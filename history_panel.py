"""
History window — charts of what the background recorder has logged.

Day and Month views: one PM2.5 chart (shaded by the US EPA 2024 PM2.5 categories). Each point's label
is "PM2.5 (AQI …)", with ", PM10" when PM10 sets the AQI. Below it, a thin weather strip on the same
time axis (wind speed + direction arrows, mixing-layer height). One colour per location. Hollow rings =
WAQI live readings (turned back into PM2.5). Shaded band = lowest–highest of your area's monitors.
The y-axis is your normal range (same height every day); readings above it are ▲ markers.

Day view:   x = time of day (each hourly average plotted at mid-hour).
Month view: one point per day — EPA daily AQI (from the 24-h mean) and mean PM2.5.
            Filled dot = valid day (≥ 18 h), hollow = incomplete day.
"""
from datetime import datetime, timedelta

from PySide6.QtCore import Qt, QDateTime, QUrl, QMargins, QPointF, QRectF
from PySide6.QtGui import QColor, QPen, QPainter, QDesktopServices, QBrush, QFont
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QCheckBox, QButtonGroup, QToolTip, QSizePolicy, QToolButton, QMenu, QWidgetAction,
)
from PySide6.QtCharts import (
    QChart, QChartView, QLineSeries, QScatterSeries, QAreaSeries,
    QDateTimeAxis, QValueAxis, QCategoryAxis,
)

import json
import os
import statistics

from recorder import Store, DATA_DIR, parse_dt, _num, METHOD_COLS, METHOD_NAMES, _label
from aqi_math import live_row_aqi, pm25_to_aqi, aqi_to_pm25, waqi_to_pm25

TARGET_COLORS = {"home": "#7aa2f7", "tracked": "#ff7a93"}
INDOOR_COLORS = ["#3ecf8e", "#f7d154", "#c084fc", "#38bdf8"]
OUTDOOR_COLOR = "#ef4444"
MODEL_COLOR = "#c9b38a"          # satellite model (CAMS): sand-coloured dashed line
WIND_COLOR = "#67e8f9"           # weather strip: wind speed line + direction arrows
BLH_COLOR = "#94a3b8"            # weather strip: mixing-layer height (shaded)
BG, CARD, GRID, TEXT, MUTED = "#0e0e16", "#1c1c28", "#2a2a3e", "#d8d8e8", "#8a8aa3"

# (upper AQI, upper PM2.5 µg/m³, label, colour)
BANDS = [
    (50,  9.0,   "Good",          "#3ecf8e"),
    (100, 35.4,  "Moderate",      "#f7d154"),
    (150, 55.4,  "USG",           "#f59e42"),
    (200, 125.4, "Unhealthy",     "#ef4444"),
    (300, 225.4, "Very unhealthy", "#a855f7"),
    (500, 325.4, "Hazardous",     "#9f1239"),
]

BTN = ("QPushButton { background:#2a2a3e; color:#d8d8e8; border:1px solid #3a3a52;"
       " padding:6px 14px; border-radius:7px; font-weight:600; }"
       "QPushButton:hover { background:#3a3a52; }"
       "QPushButton:checked { background:#7aa2f7; color:#0e0e16; border-color:#7aa2f7; }")


def _ms(dt):
    return int(dt.timestamp() * 1000)


def _local_naive(s):
    """WAQI station times are local wall-clock strings without an offset."""
    try:
        return datetime.fromisoformat(str(s)).astimezone()
    except (TypeError, ValueError):
        return None


def _excel_dt(d, t):
    """Fallback for rows whose timestamp Excel reformatted: rebuild from the date + time columns."""
    for fmt in ("%Y-%m-%d %H:%M:%S", "%d-%m-%Y %H:%M:%S", "%d/%m/%Y %H:%M:%S", "%m/%d/%Y %H:%M:%S",
                "%Y-%m-%d %H:%M", "%d-%m-%Y %H:%M", "%d/%m/%Y %H:%M", "%m/%d/%Y %H:%M"):
        try:
            return datetime.strptime(f"{str(d).strip()} {str(t).strip()}", fmt).astimezone()
        except ValueError:
            continue
    return None


# ── "normal range" for the y-axis ───────────────────────────────────────────
# The axis is confined to what is normal for YOUR data: the 98th percentile of the last 90 days
# (festival nights excluded) plus a 15 % allowance. Anything above that ceiling is drawn ON the
# ceiling line as a ▲ marker labelled with its real value, so one Diwali night can't squash the
# rest of the chart. Floors keep ordinary bad days from ever being treated as outliers.
NORMAL_WINDOW_DAYS = 90
NORMAL_PERCENTILE = 98
NORMAL_ALLOWANCE = 0.15
MIN_HOURS, MIN_DAYS = 72, 10
OUTLIER_TOLERANCE = 0.20          # values up to ceiling × 1.2 just get a taller axis
OUTLIER_SHARE = 0.05              # at most ~5 % of a chart's points may sit above the axis (1–3 a day)
FLOORS = {"hour_aqi": 300, "hour_pm": 150, "day_aqi": 250, "day_pm": 100}


def _pct(vals, q):
    v = sorted(vals)
    if not v:
        return None
    k = (len(v) - 1) * q / 100
    lo = int(k)
    hi = min(lo + 1, len(v) - 1)
    return v[lo] + (v[hi] - v[lo]) * (k - lo)


def _round_up(v, step):
    return int(-(-v // step) * step)


def normal_ranges(store, now=None):
    """{"hour_aqi": ceiling or None, "hour_pm", "day_aqi", "day_pm", "n_hours", "n_days", "excluded"}."""
    now = now or datetime.now().astimezone()
    since = now - timedelta(days=NORMAL_WINDOW_DAYS)
    try:
        from surge import festival_windows
        wins = festival_windows({since.year, now.year}, store.dir)
    except Exception:
        wins = []

    def festive(t):
        return any(a <= t < b for a, b, _ in wins)

    hv = {"aqi": [], "pm": []}
    dv = {"aqi": [], "pm": []}
    excluded, hours, days = set(), set(), set()
    for m in store.months("readings"):
        if m < since.strftime("%Y-%m"):
            continue
        for r in store.read(store.path("readings", m)):
            if r.get("target") not in ("home", "tracked"):
                continue
            t = parse_dt(r.get("hour_start_local"))
            if not t or not (since <= t <= now):
                continue
            if festive(t):
                excluded.add(t.date())
                continue
            hours.add(t)
            for col, k in (("aqi_us", "aqi"), ("aqi_mean", "aqi"), ("aqi_idw", "aqi"),
                           ("pm25_ugm3", "pm"), ("pm25_mean", "pm"), ("pm25_idw", "pm")):
                v = _num(r.get(col))
                if v is not None:
                    hv[k].append(v)
    for m in store.months("daily"):
        if m < since.strftime("%Y-%m"):
            continue
        for r in store.read(store.path("daily", m)):
            if r.get("target") not in ("home", "tracked"):
                continue
            try:
                d = datetime.fromisoformat(r["date"]).astimezone() + timedelta(hours=12)
            except (KeyError, ValueError):
                continue
            if not (since <= d <= now) or festive(d) or festive(d - timedelta(hours=10)):
                continue
            days.add(r["date"])
            for col, k in (("aqi_daily_us", "aqi"), ("pm25_mean_ugm3", "pm")):
                v = _num(r.get(col))
                if v is not None:
                    dv[k].append(v)

    def ceiling(vals, n_min, floor, step):
        if len(vals) < n_min:
            return None
        return max(floor, _round_up(_pct(vals, NORMAL_PERCENTILE) * (1 + NORMAL_ALLOWANCE), step))
    return {"hour_aqi": ceiling(hv["aqi"], MIN_HOURS, FLOORS["hour_aqi"], 10),
            "hour_pm": ceiling(hv["pm"], MIN_HOURS, FLOORS["hour_pm"], 10),
            "day_aqi": ceiling(dv["aqi"], MIN_DAYS, FLOORS["day_aqi"], 10),
            "day_pm": ceiling(dv["pm"], MIN_DAYS, FLOORS["day_pm"], 10),
            "n_hours": len(hours), "n_days": len(days), "excluded": sorted(excluded)}


from PySide6.QtWidgets import QGraphicsSimpleTextItem as _QGSTI


class _HaloText(_QGSTI):
    """Value label with a dark outline (halo) around every letter, so it stays readable where it
    crosses a line of the same colour, the spread band or the shaded categories."""
    HALO = QColor("#05050a")                       # near-black, darker than the chart background

    def __init__(self, text, parent, halo_px=3.2):
        super().__init__(text, parent)
        self.halo_px = halo_px

    def boundingRect(self):
        m = self.halo_px
        return super().boundingRect().adjusted(-m, -m, m, m)

    def paint(self, painter, option, widget=None):
        from PySide6.QtGui import QPainterPath, QFontMetricsF
        fm = QFontMetricsF(self.font())
        path = QPainterPath()
        for i, line in enumerate(self.text().split("\n")):
            path.addText(QPointF(0, fm.ascent() + i * fm.lineSpacing()), self.font(), line)
        painter.setRenderHint(QPainter.Antialiasing)
        pen = QPen(self.HALO, self.halo_px * 2)        # stroke is centred on the outline: half lies outside
        pen.setJoinStyle(Qt.RoundJoin)
        painter.strokePath(path, pen)
        painter.fillPath(path, self.brush())


class _LabelLayer:
    """Value labels drawn by us instead of Qt's point labels, so they can avoid each other and the
    data points. Where two lines share an x, the upper line's label goes above its point and the
    lower line's below, so it's always clear which value belongs to which line. A label that
    can't find a free spot (above / below / right / left) is dropped. Outlier labels go first,
    then your area, then the rest. Re-laid out whenever the plot area changes size."""

    def __init__(self, chart, scale=1.0):
        self.chart = chart
        self.scale = scale
        self.entries = []
        self.markers = []
        self.obstacles = []                       # (series, x, y) points labels must not cover (WAQI rings)
        self.band_names = []                      # (series, x, y, item): category names inside the bands
        self.header = None
        chart.plotAreaChanged.connect(self.layout)

    def add(self, series, x, y, text, color, prio, size=7, bold=True, corner=False, value=None):
        from PySide6.QtWidgets import QGraphicsSimpleTextItem
        it = _HaloText(text, self.chart, halo_px=1.2 * max(1.0, self.scale ** 0.7))
        f = QFont("Segoe UI")
        f.setPointSizeF(max(6.0, size * 0.94 * self.scale))
        f.setBold(bold)
        it.setFont(f)
        it.setBrush(QBrush(QColor(color)))
        it.setZValue(100)
        it.hide()
        self.entries.append({"prio": prio, "series": series, "x": x, "y": y, "item": it, "corner": corner,
                             "v": y if value is None else value, "marker": False})

    def add_band_name(self, series, x, y, text, color):
        it = _QGSTI(text, self.chart)
        f = QFont("Segoe UI"); f.setPointSizeF(max(6.0, 7.5 * self.scale)); f.setBold(True)
        it.setFont(f)
        c = QColor(color if color in ("#3ecf8e", "#f7d154", "#f59e42") else TEXT); c.setAlpha(150)
        it.setBrush(QBrush(c))
        it.setZValue(2)
        self.band_names.append((series, x, y, it))

    def set_header(self, html, pt):
        from PySide6.QtWidgets import QGraphicsTextItem
        if self.header is None:
            self.header = QGraphicsTextItem(self.chart)
            self.header.setZValue(101)
        f = QFont("Segoe UI", max(6, round(pt * self.scale)))
        f.setBold(True)
        self.header.setFont(f)
        self.header.setHtml(html)

    def add_outlier(self, series, x, y_top, value, color, main=True, text=None):
        """▲ on the top edge of the plot + the real value just above it (in the chart margin)."""
        from PySide6.QtWidgets import QGraphicsPolygonItem
        from PySide6.QtGui import QPolygonF
        k = self.scale ** 0.7
        tri = QGraphicsPolygonItem(QPolygonF([QPointF(0, -11 * k), QPointF(-6.5 * k, 0), QPointF(6.5 * k, 0)]),
                                   self.chart)
        tri.setBrush(QBrush(QColor(color if main else CARD)))
        pen = QPen(QColor(color)); pen.setWidthF(1.6); tri.setPen(pen)
        tri.setZValue(99)
        self.markers.append((series, x, y_top, tri))
        self.add(series, x, y_top, text or f"{value:.0f}", QColor(color).lighter(115).name(), 0 if main else 3,
                 size=8, value=value)
        self.entries[-1]["marker"] = True

    def layout(self, *_):
        pa = self.chart.plotArea()
        if pa.width() < 50:
            return
        top_room = 28 * self.scale + (30 if self.header is not None else 0)
        bounds = QRectF(pa.left() - 4, pa.top() - top_room, pa.width() + 8, pa.height() + top_room)
        for series, x, y, tri in self.markers:
            tri.setPos(self.chart.mapToPosition(QPointF(x, y), series))
        for series, x, y, it in self.band_names:
            q = self.chart.mapToPosition(QPointF(x, y), series)
            br = it.boundingRect()                  # right edge, next to the AQI numbers it names
            it.setPos(pa.right() - br.width() - 8, q.y() - br.height() / 2)
        for e in self.entries:
            e["pt"] = self.chart.mapToPosition(QPointF(e["x"], e["y"]), e["series"])
        # every data point is an obstacle, so labels never sit on a marker
        placed = [QRectF(e["pt"].x() - 4, e["pt"].y() - (12 if e["marker"] else 4), 8, 16 if e["marker"] else 8)
                  for e in self.entries if not e["corner"]]
        for series, x, y in self.obstacles:
            q = self.chart.mapToPosition(QPointF(x, y), series)
            placed.append(QRectF(q.x() - 6, q.y() - 6, 12, 12))
        if self.header is not None:                      # legend pinned to the top-left corner
            self.header.setTextWidth(max(200.0, self.chart.size().width() - pa.left()))
            self.header.setPos(pa.left() - 8, 2)
            placed.append(self.header.mapRectToParent(self.header.boundingRect()))
        by_x = {}
        for e in self.entries:
            if not e["corner"]:
                by_x.setdefault(round(e["x"]), []).append((round(e["pt"].y()), -e["v"]))
        for e in sorted(self.entries, key=lambda e: e["prio"]):
            it, pt = e["item"], e["pt"]
            br = it.boundingRect()
            w, h = br.width(), br.height()
            above = QRectF(pt.x() - w / 2, pt.y() - h - ((13 * self.scale ** 0.7) if e["marker"] else 5), w, h)
            below = QRectF(pt.x() - w / 2, pt.y() + 5, w, h)
            right = QRectF(pt.x() + 7, pt.y() - h / 2, w, h)
            left = QRectF(pt.x() - w - 7, pt.y() - h / 2, w, h)
            if e["corner"]:                                  # caption at the right end of a line
                xs = [pa.right() - w - 6 - k * 30 for k in range(int(max(0, pa.width() - w - 12) // 30) + 1)]
                cands = [QRectF(x0, pt.y() + dy, w, h) for x0 in xs for dy in (-h - 6, 6)]   # slide right → left
            else:
                ys = by_x.get(round(e["x"]), [])
                # lowest on screen at this x (ties on the ceiling: the smaller real value is lower)
                lower = len(ys) > 1 and (round(pt.y()), -e["v"]) == max(ys)
                gap = h * 0.8                            # one step further away, still straight above/below

                def keep_in(r):                          # at the chart's left/right edge, slide the label
                    dx = max(bounds.left() - r.left(), 0) - max(r.right() - bounds.right(), 0)
                    return r.translated(dx, 0)           # inwards: still above/below its point, never cut
                above, below = keep_in(above), keep_in(below)
                above2, below2 = above.translated(0, -gap), below.translated(0, gap)
                if e["marker"]:                          # ▲ value: above it, or just under it if the
                    cands = [above, above2, below]       # margin is too short for the label
                elif len(ys) > 1:                        # lines share this x: upper line's label above its
                    cands = [below, below2] if lower else [above, above2]   # point, lower line's below
                else:
                    cands = [above, below, above2, below2]
            spot = next((r for r in cands if bounds.contains(r) and
                         not any(r.intersects(q) for q in placed)), None)
            if spot is None and not e["corner"] and not e["marker"] and e["prio"] < 2:
                # a peak / low / latest hour whose usual side is blocked (e.g. a low next to the x-axis):
                # use the other side rather than leave the day's key number blank
                spot = next((r for r in (above, below, above2, below2) if r not in cands and bounds.contains(r)
                             and not any(r.intersects(q) for q in placed)), None)
            if spot is None:
                it.hide()
                continue
            placed.append(spot.adjusted(-1, -1, 1, 1))
            it.setPos(spot.topLeft())
            it.show()


def _brief_name(label):
    """'Your area (5 monitors) — median' → 'Your area'; 'Worst in Delhi: Jahangirpuri' → 'Worst in Delhi'."""
    return str(label).split(" — ")[0].split(" (")[0].split(":")[0]


def _short(name):
    return str(name or "").split(",")[0].strip()


class HistoryWindow(QWidget):
    def __init__(self, target_labels, data_dir=DATA_DIR, parent=None, phone=False):
        super().__init__(parent)
        # phone=True: an off-screen copy that draws only the charts, with ~2× text and thicker lines,
        # for pictures sent to the phone (readable on a 6-inch screen)
        self.phone = phone
        self.ui_scale = 2.1 if phone else 1.0
        self.pen_scale = 1.7 if phone else 1.0
        self.setWindowTitle("AQI History")
        self.resize(1200, 820)
        self.setStyleSheet(f"QWidget {{ background:{BG}; color:{TEXT}; font-size:12px; }}"
                           f"QCheckBox {{ spacing:6px; }}")
        self.store  = Store(data_dir)
        self.labels = target_labels
        self.mode   = "day"
        self.day    = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 14, 20, 12)
        root.setSpacing(8)

        # ── controls ────────────────────────────────────────────────────────
        bar = QHBoxLayout()
        self.day_btn, self.month_btn = QPushButton("Day"), QPushButton("Month")
        self.indoor_btn = QPushButton("Purifier")
        grp = QButtonGroup(self)
        for b in (self.day_btn, self.month_btn, self.indoor_btn):
            b.setCheckable(True); b.setStyleSheet(BTN); grp.addButton(b); bar.addWidget(b)
        self.day_btn.setChecked(True)
        self.day_btn.clicked.connect(lambda: self._set_mode("day"))
        self.month_btn.clicked.connect(lambda: self._set_mode("month"))
        self.indoor_btn.clicked.connect(lambda: self._set_mode("indoor"))
        bar.addSpacing(16)
        self.prev_btn, self.next_btn = QPushButton("◀"), QPushButton("▶")
        for b in (self.prev_btn, self.next_btn):
            b.setStyleSheet(BTN); b.setFixedWidth(40)
        self.period_lbl = QLabel("")
        self.period_lbl.setStyleSheet("font-size:15px; font-weight:700; color:#f5f5fa;")
        self.period_lbl.setMinimumWidth(190)
        self.period_lbl.setAlignment(Qt.AlignCenter)
        self.prev_btn.clicked.connect(lambda: self._step(-1))
        self.next_btn.clicked.connect(lambda: self._step(+1))
        today_btn = QPushButton("Today"); today_btn.setStyleSheet(BTN); today_btn.clicked.connect(self._today)
        for w in (self.prev_btn, self.period_lbl, self.next_btn, today_btn):
            bar.addWidget(w)
        bar.addStretch()
        self.info_btn = QPushButton("ⓘ How to read"); self.info_btn.setStyleSheet(BTN)
        self.info_btn.setToolTip("What every line, band and number means, the full notes, and the sources")
        self.info_btn.clicked.connect(self._show_details)
        bar.addWidget(self.info_btn)
        self.save_btn = QPushButton("⤓ Save graph"); self.save_btn.setStyleSheet(BTN)
        self.save_btn.setToolTip("Save the charts on screen (with legend and notes) as a PNG picture or a PDF")
        self.save_btn.clicked.connect(self._save_graph)
        bar.addWidget(self.save_btn)
        folder_btn = QPushButton("Open data folder"); folder_btn.setStyleSheet(BTN)
        folder_btn.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(self.store.dir)))
        bar.addWidget(folder_btn)
        root.addLayout(bar)

        # ── one row: locations · Layers ▾ · area method ─────────────────────
        leg = QHBoxLayout()
        leg.setSpacing(18)
        self.checks = {}
        for key in ("home", "tracked"):
            cb = QCheckBox(f"━━  {self.labels.get(key, key)}")
            cb.setChecked(True)
            cb.setStyleSheet(f"QCheckBox {{ color:{TARGET_COLORS[key]}; font-weight:700; font-size:13px; }}")
            cb.toggled.connect(self.reload)
            self.checks[key] = cb
            leg.addWidget(cb)

        def toggle(text, color, on, tip, bold=False):
            cb = QCheckBox(text)
            cb.setChecked(on)
            cb.setToolTip(tip)
            cb.setStyleSheet(f"QCheckBox {{ color:{color}; font-size:12px; padding:5px 10px; "
                             f"{'font-weight:700;' if bold else ''} }}")
            cb.toggled.connect(self.reload)
            return cb
        self.model_cb = toggle("┄  Satellite model (CAMS)", MODEL_COLOR, False,
                               "Copernicus CAMS: a model corrected with NASA satellite aerosol data, ~45 km grid, "
                               "independent of the ground monitors. Use it for the trend, not the exact level.")
        self.scaled_cb = toggle("┈  Satellite model × usual gap", MODEL_COLOR, True,
                                "The model scaled by how much higher your monitors usually read than it. If your line "
                                "follows this one, weather / regional air explains the change.")
        self.live_cb = toggle("○  Live readings (hours not yet published)", TEXT, True,
                              "WAQI's live readings for the latest hours, before the government feed publishes "
                              "them. Replaced by the measured point once the hour is published.")
        self.spread_cb = toggle("▒  Spread of your monitors", TARGET_COLORS["home"], False,
                                "Band behind 'Your area': lowest to highest of its monitors each hour. Narrow = they "
                                "agree; wide = they disagree (or one is faulty).")
        self.avg_cb = toggle("╌  24-h average (as CPCB / news report)", TEXT, False,
                             "Rolling mean of the last 24 hours (where ≥ 18 of them exist). Day view only.")
        self.weather_cb = toggle("🌬  Weather strip", WIND_COLOR, True,
                                 "Wind speed and direction and mixing-layer height under the chart, on the same time "
                                 "axis (temperature and humidity in the tooltip).")
        self.values_cb = toggle("123  Values on points", TEXT, True, "PM2.5 (AQI) printed above each point.")
        self.layers_btn = QToolButton()
        self.layers_btn.setText("Layers  ▾")
        self.layers_btn.setPopupMode(QToolButton.InstantPopup)
        self.layers_btn.setStyleSheet("QToolButton { background:#2a2a3e; color:#d8d8e8; border:1px solid #3a3a52;"
                                      " padding:5px 14px; border-radius:7px; font-weight:600; }"
                                      "QToolButton::menu-indicator { image:none; }")
        menu = QMenu(self.layers_btn)
        menu.setStyleSheet("QMenu { background:#1c1c28; border:1px solid #3a3a52; padding:6px; }")
        for cb in (self.model_cb, self.scaled_cb, self.live_cb, self.spread_cb, self.avg_cb, self.weather_cb,
                   self.values_cb):
            wa = QWidgetAction(menu)
            wa.setDefaultWidget(cb)               # ticking keeps the menu open
            menu.addAction(wa)
        self.layers_btn.setMenu(menu)
        leg.addWidget(self.layers_btn)
        self.mark_lbl = QLabel("")
        self.mark_lbl.setStyleSheet(f"color:{MUTED}; font-size:12px;")
        leg.addWidget(self.mark_lbl)
        leg.addStretch()
        self.method = "median"
        self.mlab = QLabel("Your area:")
        self.mlab.setStyleSheet(f"color:{MUTED};")
        leg.addWidget(self.mlab)
        self.method_btns = {}
        mgrp = QButtonGroup(self)
        for m, txt in (("median", "Median"), ("mean", "Average"), ("idw", "Distance-weighted")):
            b = QPushButton(txt); b.setCheckable(True); b.setStyleSheet(BTN)
            b.clicked.connect(lambda _=False, mm=m: self._set_method(mm))
            mgrp.addButton(b); leg.addWidget(b); self.method_btns[m] = b
        self.method_btns["median"].setChecked(True)
        root.addLayout(leg)
        self.method_lbl = QLabel("")               # Purifier view's legend line (hidden in Day/Month)
        self.method_lbl.setWordWrap(True)
        self.method_lbl.setStyleSheet(f"color:{TEXT}; font-size:12px;")
        root.addWidget(self.method_lbl)
        self._details, self._model_brief, self._wx_brief = [], "", ""

        # ── charts ──────────────────────────────────────────────────────────
        self.main_chart, self.wx_chart = self._new_chart(), self._new_chart()
        self.main_view, self.wx_view = QChartView(self.main_chart), QChartView(self.wx_chart)
        for v in (self.main_view, self.wx_view):
            v.setRenderHint(QPainter.Antialiasing)
            v.setStyleSheet(f"background:{BG}; border:none;")
            v.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
            root.addWidget(v, 4 if v is self.main_view else 1)
        if not phone:
            self.main_view.setMinimumHeight(300)
            self.wx_view.setMinimumHeight(135)

        self.summary = QLabel("")
        self.summary.setWordWrap(True)
        self.summary.setTextFormat(Qt.RichText)
        self.summary.setStyleSheet("font-size:12px;")
        root.addWidget(self.summary)
        self.source = QLabel("")                   # kept for the "How to read" dialog, not shown
        self.source.hide()
        # live: the Purifier view for today redraws itself every minute while it's on screen
        from PySide6.QtCore import QTimer
        self._live_timer = QTimer(self)
        self._live_timer.timeout.connect(self._live_tick)
        self._live_timer.start(60_000)
        if phone:
            self._phone_layout(root)
        self.reload()

    def _phone_layout(self, root):
        """Hide every control and text block: the picture is just the charts."""
        root.setContentsMargins(8, 8, 8, 8)
        keep = {self.main_view, self.wx_view}

        def hide(item):
            w = item.widget()
            if w is not None and w not in keep:
                w.hide()
            lay = item.layout()
            if lay is not None:
                for i in range(lay.count()):
                    hide(lay.itemAt(i))
        for i in range(root.count()):
            hide(root.itemAt(i))

    # ── save / print the charts ─────────────────────────────────────────────
    def _legend_html(self):
        """One coloured line naming what is drawn (the on-screen legend is made of checkboxes)."""
        parts = []
        if self.mode == "indoor":
            parts.append(f"<span style='color:{INDOOR_COLORS[0]}'>━ Indoor PM2.5 (purifier sensor)</span>")
            parts.append(f"<span style='color:{OUTDOOR_COLOR}'>━ Outdoor PM2.5 (your area, hourly)</span>")
        else:
            for k, cb in self.checks.items():
                if cb.isChecked():
                    parts.append(f"<span style='color:{TARGET_COLORS[k]}'>━ {self.labels.get(k, k)}</span>")
            if self.mode == "day" and self.live_cb.isChecked() and getattr(self, "_rings_drawn", False):
                parts.append(f"<span style='color:{TEXT}'>○ Live, not yet published</span>")
            if self.spread_cb.isChecked():
                parts.append(f"<span style='color:{TARGET_COLORS['home']}'>▒ Spread of your monitors</span>")
            if self.mode == "day" and self.avg_cb.isChecked():
                parts.append(f"<span style='color:{TEXT}'>╌ 24-h average</span>")
            if self.model_cb.isChecked():
                parts.append(f"<span style='color:{MODEL_COLOR}'>┄ Satellite model (CAMS)</span>")
            if self.scaled_cb.isChecked():
                parts.append(f"<span style='color:{MODEL_COLOR}'>┈ Model × usual gap</span>")
            parts.append(f"<span style='color:{MUTED}'>left axis PM2.5 µg/m³ · right axis US AQI · area method: "
                         f"{METHOD_NAMES.get(self.method, self.method)}</span>")
        return "&nbsp;&nbsp;&nbsp;".join(parts)

    def export_image(self, scale=2.0):
        """The charts as one picture: title + legend, the chart(s) on screen, then the notes under them.
        Drawn at `scale` × screen size so text stays sharp when printed."""
        from PySide6.QtGui import QImage, QTextDocument
        views = [v for v in (self.main_view, self.wx_view) if not v.isHidden() and v.height() > 20]
        width = max([v.viewport().width() for v in views] + [900])
        pad = 16

        def doc(html, pt=10):
            d = QTextDocument()
            f = QFont("Segoe UI"); f.setPointSizeF(pt)
            d.setDefaultFont(f)
            d.setHtml(f"<div style='color:{TEXT}'>{html}</div>")
            d.setTextWidth(width - 2 * pad)
            return d
        title = {"day": "Day", "month": "Month", "indoor": "Purifier"}[self.mode]
        head = doc(f"<span style='font-size:14pt; font-weight:700; color:#f5f5fa'>AQI History · {title} · "
                   f"{self.period_lbl.text()}</span><br>{self._legend_html()}", 10)
        notes = [doc(self.summary.text(), 9)] if self.summary.text().strip() else []
        notes.append(doc("<span style='color:#8a8aa3'>Source: OpenAQ hourly averages from government monitors · "
                         "AQI: US EPA 2024 breakpoints · satellite model: Copernicus CAMS · weather: Open-Meteo</span>",
                         8))
        height = int(pad + head.size().height() + 8 + sum(v.viewport().height() + 6 for v in views)
                     + sum(d.size().height() + 4 for d in notes) + pad)
        img = QImage(int(width * scale), int(height * scale), QImage.Format_ARGB32)
        img.fill(QColor(BG))
        p = QPainter(img)
        p.setRenderHints(QPainter.Antialiasing | QPainter.TextAntialiasing | QPainter.SmoothPixmapTransform)
        p.scale(scale, scale)
        y = pad
        p.save(); p.translate(pad, y); head.drawContents(p); p.restore()
        y += head.size().height() + 8
        for v in views:
            vp = v.viewport().rect()                    # vector redraw of the chart scene, so it stays sharp
            v.render(p, QRectF((width - vp.width()) / 2, y, vp.width(), vp.height()), vp)
            y += vp.height() + 6
        for d in notes:
            p.save(); p.translate(pad, y); d.drawContents(p); p.restore()
            y += d.size().height() + 4
        p.end()
        return img

    @staticmethod
    def _write_pdf(img, path):
        """One A4 page (landscape for wide pictures), the picture fitted inside 8 mm margins."""
        from PySide6.QtGui import QPdfWriter, QPageSize, QPageLayout
        from PySide6.QtCore import QMarginsF, QRect
        w = QPdfWriter(path)
        w.setResolution(300)
        w.setPageSize(QPageSize(QPageSize.A4))
        w.setPageOrientation(QPageLayout.Landscape if img.width() >= img.height() else QPageLayout.Portrait)
        w.setPageMargins(QMarginsF(8, 8, 8, 8), QPageLayout.Millimeter)
        p = QPainter(w)
        if not p.isActive():
            return False
        r = p.viewport()
        s = min(r.width() / img.width(), r.height() / img.height())
        tw, th = int(img.width() * s), int(img.height() * s)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        p.drawImage(QRect((r.width() - tw) // 2, 0, tw, th), img)
        p.end()
        return True

    def _save_graph(self):
        from PySide6.QtWidgets import QFileDialog, QMessageBox
        kind = {"day": "day", "month": "month", "indoor": "purifier"}[self.mode]
        stamp = self.day.strftime("%Y-%m" if self.mode == "month" else "%Y-%m-%d")
        folder = os.path.join(self.store.dir, "saved_graphs")
        os.makedirs(folder, exist_ok=True)
        path, flt = QFileDialog.getSaveFileName(self, "Save graph", os.path.join(folder, f"AQI_{kind}_{stamp}.png"),
                                                "PNG picture (*.png);;PDF for printing (*.pdf)")
        if not path:
            return
        pdf = path.lower().endswith(".pdf") or (flt.startswith("PDF") and not path.lower().endswith(".png"))
        if pdf and not path.lower().endswith(".pdf"):
            path += ".pdf"
        if not pdf and not path.lower().endswith(".png"):
            path += ".png"
        try:
            img = self.export_image()
            ok = self._write_pdf(img, path) if pdf else img.save(path, "PNG")
        except Exception as e:
            ok, err = False, str(e)
        else:
            err = "" if ok else "the file couldn't be written (is it open in another program?)"
        if not ok:
            QMessageBox.warning(self, "Graph not saved", f"Couldn't save {os.path.basename(path)}: {err}")
            return
        box = QMessageBox(self)
        box.setWindowTitle("Graph saved")
        box.setText(f"Saved {os.path.basename(path)}\nin {os.path.dirname(path)}")
        open_btn = box.addButton("Open it", QMessageBox.AcceptRole)
        box.addButton("Close", QMessageBox.RejectRole)
        box.exec()
        if box.clickedButton() is open_btn:
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    def _set_summary(self, stats, status, details):
        """At most three short lines under the chart; everything else is in "ⓘ How to read"."""
        lines = []
        if stats:
            lines.append("&nbsp;&nbsp;&nbsp;│&nbsp;&nbsp;&nbsp;".join(stats))
        second = [x for x in (self._model_brief, self._wx_brief) if x]
        if second:
            lines.append(f"<span style='color:{MUTED}'>" + "&nbsp;&nbsp;&nbsp;·&nbsp;&nbsp;&nbsp;".join(second) + "</span>")
        if status:
            lines.append("<span style='color:#e0a96d'>" + "&nbsp;&nbsp;&nbsp;·&nbsp;&nbsp;&nbsp;".join(status) + "</span>")
        self.summary.setText("<br>".join(lines) or "Tick at least one location above.")
        self._details = details

    def _show_details(self):
        from PySide6.QtWidgets import QDialog, QTextBrowser
        dlg = QDialog(self)
        dlg.setWindowTitle("How to read this chart")
        dlg.resize(900, 640)
        lay = QVBoxLayout(dlg)
        tb = QTextBrowser()
        tb.setOpenExternalLinks(True)
        tb.setStyleSheet(f"QTextBrowser {{ background:{CARD}; color:{TEXT}; border:none; padding:12px; font-size:13px; }}")
        bands = " ".join(f"<span style='color:{c}'>■</span> {n}" for _, _, n, c in BANDS)
        legend = [
            f"<span style='color:{TARGET_COLORS['home']}'>━ Your area</span> and "
            f"<span style='color:{TARGET_COLORS['tracked']}'>━ the tracked station</span>: hourly PM2.5 "
            "(Month view: daily mean). <b>Left axis = PM2.5 µg/m³</b> (evenly spaced). <b>Right axis = the US AQI at the "
            "same height</b>: its numbers are unevenly spaced because AQI rises at a different rate in each category. "
            "Labels show PM2.5; each line's peak, lowest point and latest hour also show the AQI. When PM10, not PM2.5, "
            "sets the AQI, the label always shows it (“PM10”), because the right axis only reflects PM2.5.",
            f"<span style='color:{TARGET_COLORS['home']}'>▒</span> Spread: lowest to highest of your area's monitors.",
            "○ Rings: live readings (WAQI) for the latest hours, before the government feed publishes them "
            "(it runs 1–2 h behind). Each ring sits at the middle of its hour, where the measured point will appear; "
            "once that hour is published, the ring is replaced by the point.",
            f"<span style='color:{MODEL_COLOR}'>┄ Satellite model</span> (its own level) and "
            f"<span style='color:{MODEL_COLOR}'>┈ model × usual gap</span> (where your monitors would be on a normal day "
            "for this weather).",
            f"<span style='color:{WIND_COLOR}'>Weather strip</span>: wind speed (line; arrows show where it blows to) "
            "and mixing-layer height (grey area; the red zone below 300 m traps pollution).",
            f"Background shading: {bands}. The y-axis is your normal range, the same every day; ▲ = a reading above it, "
            "with its real value.",
        ]
        html = ("<h3>What's on the chart</h3><ul>" + "".join(f"<li>{x}</li>" for x in legend) + "</ul>"
                + f"<h3>Your area</h3><p>{getattr(self, '_method_html', '')}</p>"
                + "<h3>This view</h3><p>" + "<br><br>".join(self._details or ["—"]) + "</p>"
                + "<h3>Sources</h3><p style='color:#8a8aa3'>" + self.source.text().replace("\n", "<br>") + "</p>")
        tb.setHtml(html)
        lay.addWidget(tb)
        dlg.setStyleSheet(f"QDialog {{ background:{BG}; }}")
        dlg.exec()

    def _font(self, pt, bold=False):
        f = QFont("Segoe UI", max(6, round(pt * self.ui_scale)))
        f.setBold(bold)
        return f

    def _phone_title(self, period):
        """Phone pictures have no controls, so the top chart carries the legend."""
        if not self.phone:
            return
        parts = [f"<span style='color:{TEXT}'>{period}</span>"]
        for k, cb in self.checks.items():
            if cb.isChecked():
                short = str(self.labels.get(k, k)).split(" (")[0].split(":")[0]
                parts.append(f"<span style='color:{TARGET_COLORS[k]}'>━ {short}</span>")
        if self.mode == "day" and self.live_cb.isChecked():
            parts.append(f"<span style='color:{MUTED}'>○ live</span>")
        if self.spread_cb.isChecked():
            parts.append(f"<span style='color:{TARGET_COLORS['home']}'>▒ spread</span>")
        if self.mode == "day" and self.avg_cb.isChecked():
            parts.append(f"<span style='color:{TEXT}'>╌ 24-h avg</span>")
        if self.model_cb.isChecked() or self.scaled_cb.isChecked():
            parts.append(f"<span style='color:{MODEL_COLOR}'>┄ satellite model</span>")
        self._layers[0].set_header("&nbsp;&nbsp;".join(parts), 9)

    def _live_tick(self):
        if self.mode == "indoor" and self.isVisible() and \
                self.day.date() == datetime.now().astimezone().date():
            self.reload()

    # ── navigation ──────────────────────────────────────────────────────────
    def _set_method(self, m):
        self.method = m
        self.method_btns[m].setChecked(True)
        self.reload()

    def _cols(self, key):
        return METHOD_COLS[self.method] if key == "home" else METHOD_COLS["median"]

    def _area_info(self):
        """Which monitors make up 'your area', from the recorder's state file."""
        try:
            with open(os.path.join(self.store.dir, "state.json"), encoding="utf-8") as f:
                st = (json.load(f).get("stations") or {}).get("home") or {}
        except (OSError, ValueError):
            return None
        comps = st.get("components") or []
        return comps if st.get("mode") == "median" and comps else None

    def _update_method_label(self):
        comps = self._area_info()
        on = bool(comps)
        for b in self.method_btns.values():
            b.setEnabled(on)
        if not on:
            self._method_html = "Your area is a single monitor, so there's nothing to combine."
            return
        lst = ", ".join(f"{_label(c['name'])} {float(c.get('distance_km') or 0):.1f} km" for c in comps)
        how = {"median": "the middle value of the monitors that reported each hour",
               "mean": "the plain average of the monitors that reported each hour",
               "idw": "an average weighted by 1/distance², so closer monitors count more"}[self.method]
        self._method_html = (f"<b>Your area = {METHOD_NAMES[self.method]} of {len(comps)} monitors</b> "
                             f"({how}): {lst}")
        for b in self.method_btns.values():
            b.setToolTip(self._method_html)

    def _set_mode(self, m):
        self.mode = m
        {"day": self.day_btn, "month": self.month_btn, "indoor": self.indoor_btn}[m].setChecked(True)
        self.reload()

    def _step(self, n):
        if self.mode in ("day", "indoor"):
            self.day = (self.day + timedelta(days=n, hours=12)).replace(hour=0)
        else:
            y, mo = self.day.year, self.day.month + n
            y, mo = y + (mo - 1) // 12, (mo - 1) % 12 + 1
            self.day = self.day.replace(year=y, month=mo, day=1)
        self.reload()

    def _today(self):
        self.day = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
        self.reload()

    # ── chart plumbing ──────────────────────────────────────────────────────
    def _new_chart(self):
        c = QChart()
        c.setBackgroundBrush(QBrush(QColor(CARD)))
        c.setBackgroundRoundness(10)
        k = getattr(self, "ui_scale", 1.0)
        # top margin: room for outlier values above the plot (+ the legend title on phone pictures)
        c.setMargins(QMargins(6, round(30 * k) + (44 if getattr(self, "phone", False) else 0), 6, 6))
        c.legend().hide()
        return c

    def _fresh_charts(self):
        self._keep_old, self._keep = getattr(self, "_keep", []), []
        # A fresh QChart per redraw: removing axes from a live chart leaves ghosts.
        for attr, view in (("main_chart", self.main_view), ("wx_chart", self.wx_view)):
            old = getattr(self, attr)
            new = self._new_chart()
            view.setChart(new)
            setattr(self, attr, new)
            old.deleteLater()

    def _x_axis(self, lo, hi, fmt, ticks, title, labels=True):
        x = QDateTimeAxis()
        x.setFormat(fmt); x.setTickCount(ticks)
        x.setTruncateLabels(False)
        x.setRange(QDateTime.fromMSecsSinceEpoch(lo), QDateTime.fromMSecsSinceEpoch(hi))
        x.setLabelsColor(QColor(MUTED)); x.setGridLineColor(QColor(GRID))
        x.setLabelsVisible(labels)
        x.setLabelsFont(self._font(8))
        if title:
            x.setTitleText(title); x.setTitleBrush(QBrush(QColor(MUTED)))
            x.setTitleFont(self._font(8, True))
        return x

    def _setup_chart(self, chart, x, y_max, unit, band_index):
        """band_index 0 = AQI bounds, 1 = PM2.5 bounds."""
        y = QValueAxis()
        y.setRange(0, y_max); y.setTickCount(7); y.setLabelFormat("%d")
        y.setTruncateLabels(False)                 # numbers, never "…", however narrow the chart
        y.setTitleText(unit); y.setTitleBrush(QBrush(QColor(MUTED)))
        y.setLabelsColor(QColor(MUTED)); y.setGridLineColor(QColor(GRID))
        y.setLabelsFont(self._font(8)); y.setTitleFont(self._font(8, True))
        cats = QCategoryAxis()
        cats.setLabelsFont(self._font(8))
        cats.setTruncateLabels(False)
        cats.setRange(0, y_max)
        cats.setLabelsPosition(QCategoryAxis.AxisLabelsPositionCenter)
        cats.setLabelsColor(QColor(MUTED)); cats.setGridLineVisible(False)
        cats.setLineVisible(False)
        if band_index == 1:                        # PM2.5 chart: right axis = the AQI at the same height
            cats.setLabelsPosition(QCategoryAxis.AxisLabelsPositionOnValue)
            cats.setLabelsColor(QColor(TEXT))
            cats.setTitleText("US AQI"); cats.setTitleBrush(QBrush(QColor(TEXT)))
            cats.setTitleFont(self._font(8, True))
            last = -1e9
            for a in (50, 100, 150, 200, 250, 300, 400, 500, 600, 700, 800, 1000, 1200, 1500, 2000, 3000):
                pm = aqi_to_pm25(a)
                if pm is None or pm > y_max:
                    break
                if pm - last >= y_max * 0.045:      # keep the numbers apart where categories are thin
                    cats.append(f"{a}", pm)
                    last = pm
        chart.addAxis(x, Qt.AlignBottom)
        chart.addAxis(y, Qt.AlignLeft)
        chart.addAxis(cats, Qt.AlignRight)
        layer = next((l for l in getattr(self, "_layers", []) if l.chart is chart), None)

        lo_val = 0.0
        x_lo, x_hi = x.min().toMSecsSinceEpoch(), x.max().toMSecsSinceEpoch()
        for band in BANDS:
            hi_val, name, col = band[band_index], band[2], band[3]
            if lo_val >= y_max:
                break
            top = min(hi_val, y_max)
            upper, lower = QLineSeries(), QLineSeries()
            upper.append(x_lo, top); upper.append(x_hi, top)
            lower.append(x_lo, lo_val); lower.append(x_hi, lo_val)
            area = QAreaSeries(upper, lower)
            upper.setParent(area); lower.setParent(area)   # Qt owns them → freed with the chart, never early
            fill = QColor(col); fill.setAlpha(36)
            area.setBrush(QBrush(fill)); area.setPen(QPen(Qt.NoPen))
            chart.addSeries(area); area.attachAxis(x); area.attachAxis(y)
            if band_index == 1:
                if layer is not None and top - lo_val >= y_max * 0.06:     # name the band inside the chart
                    layer.add_band_name(area, x_lo, (lo_val + top) / 2, name, col)
            elif top - lo_val >= y_max * 0.05:          # skip labels on slivers
                cats.append(name, top)
            else:
                cats.append(" " * (len(cats.categoriesLabels()) + 1), top)
            lo_val = hi_val
        return y

    def _scale(self, plots, kind, cap, fixed=False):
        """(axis top, clamp level or None). The axis top IS the bound: points above it are drawn on
        the top edge as ▲ markers with their real values.
        fixed=True (Day/Month views): the axis is your normal range on every day, so the same value is
        the same height on any day. It is raised only when more than ~5 % of the points would sit
        above it, so a very bad day shows 1–3 markers, not a row of them.
        fixed=False (Purifier view): the axis fits the data unless it exceeds your normal range × 1.2."""
        vals = sorted((p.y() for k, s in plots if k == kind and id(s) not in self._no_rank
                       for p in s.points()), reverse=True)
        vmax = vals[0] if vals else 0
        floor = 50 if kind == "pm" else 100
        if not cap:
            return _nice(max(vmax, floor)), None
        base = _nice_tight(cap)
        if fixed:
            if vmax <= base:
                return base, None
        elif vmax <= cap * (1 + OUTLIER_TOLERANCE):
            return _nice(max(vmax, floor)), None
        allowed = max(1, round((0.10 if fixed else OUTLIER_SHARE) * len(vals)))
        must_fit = vals[allowed] if len(vals) > allowed else 0
        top = _nice_tight(max(cap, must_fit))
        near = [v for v in vals if top < v <= top * 1.10]   # don't flag a reading that only just clears
        if near:                                             # the top (753 vs 750) — one step only, so it
            top = _nice_tight(max(near))                     # can't chain upwards to fit every outlier
        return top, (top if vmax > top else None)

    def _prepare(self, plots, kind, ceiling):
        """Collect value labels (real values), then clamp anything above the ceiling onto it."""
        show_vals = getattr(self, "values_cb", None) is None or self.values_cb.isChecked()
        labels, outliers = [], []
        for k, s in plots:
            if k != kind:
                continue
            col = self._labelled.get(id(s))
            pts = s.points()
            for i, p in enumerate(pts):
                if ceiling is not None and p.y() > ceiling:
                    outliers.append((s, p.x(), p.y(), s.pen().color().name(), col is not None))
                    s.replace(i, QPointF(p.x(), ceiling))
                elif col and show_vals:
                    labels.append((s, p.x(), p.y(), col))
        return labels, outliers

    def _finish_chart(self, chart, layer, x, y, ceiling, labels, outliers, unit_word):
        """▲ outlier markers on the top edge with their real values, then the value labels."""
        if ceiling is not None:
            seen = {(px, round(py)) for _, px, py, _, main in outliers if main}
            for s, px, py, col, main in outliers:
                if not main and (px, round(py)) in seen:
                    continue                                  # same value as a line point (month dots)
                layer.add_outlier(s, px, ceiling, py, col, main, text=getattr(self, "_texts", {}).get((id(s), px)))
        rank, by_col = {}, {}
        for s, px, py, col in labels:                  # one colour = one location, even across line breaks
            by_col.setdefault(col, []).append((px, py, s))
        for col, pts in by_col.items():
            pts.sort(key=lambda t: t[0])
            hi, lo = max(pts, key=lambda t: t[1]), min(pts, key=lambda t: t[1])
            for i, (px, py, s) in enumerate(pts):
                prev = pts[i - 1][1] if i > 0 else None
                nxt = pts[i + 1][1] if i < len(pts) - 1 else None
                if (px, py) in ((hi[0], hi[1]), (lo[0], lo[1])) or i == len(pts) - 1:
                    r = 1                                # the day's peak, its lowest point, the latest hour
                elif prev is not None and nxt is not None and (py - prev) * (py - nxt) > 0:
                    r = 2                                # the line turns here (local peak or dip)
                else:
                    r = 3
                rank[(id(s), px)] = r + (0 if col == TARGET_COLORS.get("home") else 0.5)
        for s, px, py, col in labels:
            text = getattr(self, "_texts", {}).get((id(s), px)) or f"{py:.0f}"
            r = rank.get((id(s), px), 3)
            if r % 1 >= 0 and int(r) > 1 and "PM10" not in text:
                text = text.split(" (")[0]              # the right axis gives its AQI; hover shows it too
            layer.add(s, px, py, text, QColor(col).lighter(108).name(), r)
        layer.layout()

    def _outlier_notes(self, outs, top, axis_top, fmt, kind):
        rg = self._ranges or {}
        norm = rg.get("hour_pm") if kind == "hourly" else rg.get("day_pm")
        out = []
        if norm and axis_top > _nice_tight(norm):
            out.append(f"<span style='color:#d0d0e0'>Axis raised to {axis_top:.0f} today (usually "
                       f"{_nice_tight(norm):.0f}): more than a few readings were above your normal range.</span>")
        main = sorted({(px, py) for _, px, py, _, m in outs if m})
        if top is None or not main:
            return out
        pts = ", ".join(f"{datetime.fromtimestamp(px / 1000).astimezone():{fmt}} {py:.0f}" for px, py in main)
        out.append(f"<span style='color:#d0d0e0'>▲ PM2.5 above the top of the axis ({top:.0f}), drawn on the top "
                   f"edge with the real value: {pts}. The axis is your normal {kind} range "
                   f"({NORMAL_PERCENTILE}th percentile of your data + {NORMAL_ALLOWANCE:.0%}, festival nights "
                   f"excluded), the same height every day so days compare by eye.</span>")
        return out

    def _add(self, chart, s, x, y):
        chart.addSeries(s); s.attachAxis(x); s.attachAxis(y)

    def _line(self, color, tips, width=2.6, which=0):
        s = QLineSeries()
        pen = QPen(QColor(color)); pen.setWidthF(width * self.pen_scale); pen.setCapStyle(Qt.RoundCap)
        s.setPen(pen)
        s.setPointsVisible(True)
        self._labelled[id(s)] = color          # value labels added later by _finish_chart()
        s.hovered.connect(lambda p, on: on and self._tip(tips, p, which))
        return s

    def _dots(self, color, tips, size=9, hollow=False, ring=False, which=0):
        s = QScatterSeries()
        s.setMarkerSize(size * self.pen_scale)
        s.setColor(QColor(CARD if (hollow or ring) else color))
        pen = QPen(QColor(color)); pen.setWidthF((2.2 if ring else 1.5) * self.pen_scale)
        s.setPen(pen)
        s.hovered.connect(lambda p, on: on and self._tip(tips, p, which))
        return s

    def _tip(self, tips, p, which=0):
        """which = 0 on the AQI chart, 1 on the PM2.5 chart: lead with that chart's own value."""
        if not tips:
            return
        k = min(tips, key=lambda t: abs(t - p.x()))
        v = tips[k]
        QToolTip.showText(self.cursor().pos(), v[which] if isinstance(v, tuple) else v)

    # ── data ────────────────────────────────────────────────────────────────
    def _rows(self, kind, month):
        return self.store.read(self.store.path(kind, month))

    def reload(self, *_):
        self._update_method_label()
        self._labelled = {}
        self._texts = {}                        # (series id, x) → label text
        self._model_brief, self._wx_brief = "", ""
        self._rings_drawn = False
        self._no_rank = set()                   # series not counted when choosing the axis (indoor overlay)
        self._fresh_charts()
        self._layers = [_LabelLayer(self.main_chart, self.ui_scale), _LabelLayer(self.wx_chart, self.ui_scale)]
        purifier = self.mode == "indoor"         # Purifier view: one big chart, outdoor-only controls hidden
        self.wx_view.setVisible(not purifier and self.weather_cb.isChecked())
        if not self.phone:
            for w in [*self.checks.values(), self.live_cb, self.model_cb, self.mlab, *self.method_btns.values(),
                      self.spread_cb, self.avg_cb, self.scaled_cb, self.weather_cb, self.info_btn]:
                w.setVisible(not purifier)
            self.method_lbl.setVisible(purifier)
        now = datetime.now()
        if getattr(self, "_ranges_at", None) is None or (now - self._ranges_at).total_seconds() > 1800:
            try:
                self._ranges = normal_ranges(self.store)
            except Exception:
                self._ranges = {}
            self._ranges_at = now
        {"day": self._draw_day, "month": self._draw_month, "indoor": self._draw_indoor}[self.mode]()

    # ── shared pieces for the Day / Month views ─────────────────────────────
    @staticmethod
    def _point_label(p, a):
        """'245 (AQI 312)' — and ', PM10' when PM10, not PM2.5, sets the AQI."""
        if p is None:
            return None
        if a is None:
            return f"{p:.0f}"
        a25 = pm25_to_aqi(p)
        pm10 = a25 is not None and a > a25 + 0.5
        return f"{p:.0f} (AQI {a:.0f}{', PM10' if pm10 else ''})"

    def _hour_monitors(self, rows, start, end):
        """{hour bucket (local): [each monitor's PM2.5]} from the per-monitor rows ('home|<id>').
        Monitors label hours at :00 or :30; +15 min and rounding down puts both in one bucket."""
        out = {}
        for r in rows:
            if not str(r.get("target", "")).startswith("home|") or r.get("status") not in ("ok", None, ""):
                continue
            v = _num(r.get("pm25_ugm3"))
            t = parse_dt(r.get("hour_start_local"))
            if v is None or not t or not (start - timedelta(hours=1) <= t < end):
                continue
            b = (t + timedelta(minutes=15)).replace(minute=0, second=0, microsecond=0)
            out.setdefault(b, []).append(v)
        return out

    def _spread_series(self, runs, ceiling, tips):
        """Faint band from the lowest to the highest monitor (one QAreaSeries per unbroken run)."""
        out = []
        for run in runs:
            if len(run) < 2:
                continue
            up, lo = QLineSeries(), QLineSeries()
            for x, a, b in run:
                lo.append(x, a if ceiling is None else min(a, ceiling))
                up.append(x, b if ceiling is None else min(b, ceiling))
            area = QAreaSeries(up, lo)
            up.setParent(area); lo.setParent(area)
            fill = QColor(TARGET_COLORS["home"]); fill.setAlpha(24)
            area.setBrush(QBrush(fill))
            edge = QColor(TARGET_COLORS["home"]); edge.setAlpha(55)
            pen = QPen(edge); pen.setWidthF(0.8)
            area.setPen(pen)
            area.hovered.connect(lambda p, on, t=tips: on and self._tip(t, p))
            out.append(area)
        return out

    def _usual_ratio(self, end, days, col):
        """Median of (your area ÷ satellite model) over the `days` before `end`, hour by hour."""
        import cams
        frm = end - timedelta(days=days)
        months = sorted({(frm + timedelta(days=i)).strftime("%Y-%m") for i in range(days + 1)})
        model = {}
        for r in cams.read(self.store.dir, months):
            try:
                model[r["time_local"][:13]] = float(r["pm25_model"])
            except (KeyError, ValueError):
                pass
        ratios = []
        for m in months:
            for r in self._rows("readings", m):
                if r["target"] != "home":
                    continue
                t = parse_dt(r.get("hour_start_local"))
                v = _num(r.get(col))
                if not t or v is None or not (frm <= t < end):
                    continue
                mv = model.get(t.strftime("%Y-%m-%dT%H"))
                if mv and mv > 0:
                    ratios.append(v / mv)
        return (statistics.median(ratios), len(ratios)) if len(ratios) >= 24 else (None, len(ratios))

    def _avg24_series(self, key, rows_all, start, end, col_name, color):
        """Rolling 24-hour mean of PM2.5 (what CPCB and the news report), only where ≥ 18 of the
        24 hours exist (EPA's 75 % rule)."""
        pts = sorted((t, v) for t, v in rows_all if t is not None and v is not None)
        tips, s = {}, QLineSeries()
        pen = QPen(QColor(color).lighter(115)); pen.setWidthF(1.6 * self.pen_scale); pen.setStyle(Qt.DashDotLine)
        s.setPen(pen)
        s.hovered.connect(lambda p, on: on and self._tip(tips, p))
        j = 0
        for i, (t, _) in enumerate(pts):
            if not (start <= t < end):
                continue
            while pts[j][0] <= t - timedelta(hours=24):
                j += 1
            win = [v for _, v in pts[j:i + 1]]
            if len(win) >= 18:
                m = statistics.fmean(win)
                x = _ms(t + timedelta(minutes=30))
                s.append(x, m)
                a = pm25_to_aqi(m)
                tips[x] = (f"{col_name} — 24-hour average ending {(t + timedelta(hours=1)):%H:%M}<br>"
                           f"<b>PM2.5 {m:.0f} µg/m³ (AQI {a})</b> from {len(win)} hours<br>"
                           f"<span style='color:#8a8aa3'>This is the number CPCB and the news report: it moves "
                           f"slowly, so it lags behind the hourly line.</span>")
        self._no_rank.add(id(s))
        return s

    def _weather_rows(self, start, end):
        import weather
        months = {start.strftime("%Y-%m"), (end - timedelta(seconds=1)).strftime("%Y-%m")}
        out = []
        for r in weather.read(self.store.dir, sorted(months)):
            try:
                t = datetime.fromisoformat(r["time_local"]).astimezone()
            except (KeyError, ValueError):
                continue
            if start <= t < end:
                out.append((t, {k: _num(r.get(k)) for k in ("temp_c", "rh_pct", "wind_kmh", "wind_dir_deg",
                                                             "mixing_height_m", "rain_mm")}))
        return sorted(out, key=lambda x: x[0])

    def _draw_weather(self, start, end, x_axis, daily=False):
        """Thin strip under the PM2.5 chart: wind speed (line, arrows = where it blows) and
        mixing-layer height (shaded, right axis; the red zone below 300 m is where pollution gets
        trapped). Returns one note line for the summary."""
        import math
        import weather
        rows = self._weather_rows(start, end)
        chart, layer = self.wx_chart, self._layers[1]
        chart.setMargins(QMargins(6, 6, 6, 2))          # no outlier room needed: more height for the strip
        if daily:                                         # Month view: one point per day
            by = {}
            for t, v in rows:
                by.setdefault(t.date(), []).append(v)
            rows = []
            for d, vs in sorted(by.items()):
                def mean(k):
                    xs = [v[k] for v in vs if v[k] is not None]
                    return statistics.fmean(xs) if xs else None
                dirs = [(v["wind_dir_deg"], v["wind_kmh"] or 0) for v in vs if v["wind_dir_deg"] is not None]
                sx = sum(math.sin(math.radians(a)) * w for a, w in dirs)
                sy = sum(math.cos(math.radians(a)) * w for a, w in dirs)
                rain = [v["rain_mm"] for v in vs if v["rain_mm"] is not None]
                rows.append((datetime.combine(d, datetime.min.time()).astimezone().replace(hour=12),
                             {"temp_c": mean("temp_c"), "rh_pct": mean("rh_pct"), "wind_kmh": mean("wind_kmh"),
                              "wind_dir_deg": (math.degrees(math.atan2(sx, sy)) % 360) if dirs else None,
                              "mixing_height_m": mean("mixing_height_m"), "rain_mm": sum(rain) if rain else None,
                              "blh_min": min((v["mixing_height_m"] for v in vs if v["mixing_height_m"] is not None),
                                             default=None)}))
        if not rows:
            self.wx_view.hide()
            self._wx_brief = (f"<span style='color:{WIND_COLOR}'>Weather:</span> none saved for this period yet "
                              "(the first fetch fills the last 3 months)")
            return (f"<span style='color:{MUTED}'>🌬 Weather strip: no weather saved for this period yet "
                    "(fetched hourly while the app runs; the first fetch fills the last 3 months).</span>")
        w_max = max([v["wind_kmh"] or 0 for _, v in rows] + [10])
        b_max = max([v["mixing_height_m"] or 0 for _, v in rows] + [600])
        yw = QValueAxis(); yw.setRange(0, _nice_tight(w_max * 1.15)); yw.setTickCount(4); yw.setLabelFormat("%d")
        yb = QValueAxis(); yb.setRange(0, _nice_tight(b_max * 1.05)); yb.setTickCount(4); yb.setLabelFormat("%d")
        for ax, title, colr in ((yw, "Wind km/h", WIND_COLOR), (yb, "Mixing m", BLH_COLOR)):
            ax.setTitleText(title); ax.setTitleBrush(QBrush(QColor(colr)))
            ax.setLabelsColor(QColor(MUTED)); ax.setGridLineColor(QColor(GRID))
            ax.setLabelsFont(self._font(7)); ax.setTitleFont(self._font(7, True))
            ax.setTruncateLabels(False)
        yb.setGridLineVisible(False)
        chart.addAxis(x_axis, Qt.AlignBottom); chart.addAxis(yw, Qt.AlignLeft); chart.addAxis(yb, Qt.AlignRight)
        x_lo, x_hi = x_axis.min().toMSecsSinceEpoch(), x_axis.max().toMSecsSinceEpoch()
        # the "trapping" zone: mixing layer below 300 m
        up, lo = QLineSeries(), QLineSeries()
        up.append(x_lo, min(300, yb.max())); up.append(x_hi, min(300, yb.max()))
        lo.append(x_lo, 0); lo.append(x_hi, 0)
        trap = QAreaSeries(up, lo); up.setParent(trap); lo.setParent(trap)
        tz = QColor("#ef4444"); tz.setAlpha(28); trap.setBrush(QBrush(tz)); trap.setPen(QPen(Qt.NoPen))
        chart.addSeries(trap); trap.attachAxis(x_axis); trap.attachAxis(yb)
        tips = {}
        bu, bl = QLineSeries(), QLineSeries()
        wind = QLineSeries()
        pen = QPen(QColor(WIND_COLOR)); pen.setWidthF(2.0 * self.pen_scale); wind.setPen(pen)
        for t, v in rows:
            x = _ms(t if daily else t + timedelta(minutes=30))
            if v["mixing_height_m"] is not None:
                bu.append(x, v["mixing_height_m"]); bl.append(x, 0)
            if v["wind_kmh"] is not None:
                wind.append(x, v["wind_kmh"])
            when = t.strftime("%a %d %b (daily mean)") if daily else t.strftime("%H:%M")
            bits = [f"<b>{when}</b>"]
            if v["wind_kmh"] is not None:
                bits.append(f"Wind {v['wind_kmh']:.0f} km/h" + (f" from {weather.compass(v['wind_dir_deg'])} "
                            f"{weather.arrow(v['wind_dir_deg'])}" if v["wind_dir_deg"] is not None else ""))
            if v["mixing_height_m"] is not None:
                bits.append(f"Mixing layer {v['mixing_height_m']:.0f} m"
                            + (f" (lowest {v['blh_min']:.0f} m)" if daily and v.get("blh_min") is not None else "")
                            + (" — shallow: pollution trapped near the ground" if v["mixing_height_m"] < 300 else ""))
            if v["temp_c"] is not None:
                bits.append(f"{v['temp_c']:.0f} °C · humidity {v['rh_pct']:.0f}%" if v["rh_pct"] is not None
                            else f"{v['temp_c']:.0f} °C")
            if v["rain_mm"]:
                bits.append(f"Rain {v['rain_mm']:.1f} mm")
            tips[x] = "<br>".join(bits)
        blh = QAreaSeries(bu, bl); bu.setParent(blh); bl.setParent(blh)
        bc = QColor(BLH_COLOR); bc.setAlpha(70); blh.setBrush(QBrush(bc))
        bp = QColor(BLH_COLOR); bp.setAlpha(160); blh.setPen(QPen(bp, 1.2))
        blh.hovered.connect(lambda p, on: on and self._tip(tips, p))
        wind.hovered.connect(lambda p, on: on and self._tip(tips, p))
        chart.addSeries(blh); blh.attachAxis(x_axis); blh.attachAxis(yb)
        chart.addSeries(wind); wind.attachAxis(x_axis); wind.attachAxis(yw)
        step = 1 if daily else (3 if self.phone else 2)
        for i, (t, v) in enumerate(rows):
            if i % step or v["wind_kmh"] is None or v["wind_dir_deg"] is None:
                continue
            x = _ms(t if daily else t + timedelta(minutes=30))
            layer.add(wind, x, v["wind_kmh"], weather.arrow(v["wind_dir_deg"]), WIND_COLOR, 5, size=9, bold=True)
        layer.layout()
        # one plain-language line for the summary
        hb = [(v["mixing_height_m"], t) for t, v in rows if v["mixing_height_m"] is not None]
        hw = [(v["wind_kmh"], t) for t, v in rows if v["wind_kmh"] is not None]
        bits = []
        if hb:
            lo_b, lo_t = min(hb)
            trapped = sum(1 for b, _ in hb if b < 300)
            bits.append(f"shallowest mixing layer {lo_b:.0f} m ({lo_t.strftime('%d %b' if daily else '%H:%M')})"
                        + (f", below 300 m for {trapped} {'day(s)' if daily else 'hour(s)'}" if trapped else ""))
        if hw:
            lo_w, lo_wt = min(hw)
            bits.append(f"calmest {lo_w:.0f} km/h ({lo_wt.strftime('%d %b' if daily else '%H:%M')})")
        wb = []
        if hb:
            if trapped:
                wb.append(f"mixing layer under 300 m for {trapped} {'day(s)' if daily else 'h'} (lowest {lo_b:.0f} m)")
            else:
                wb.append(f"lowest mixing layer {lo_b:.0f} m")
        if hw:
            wb.append(f"calmest {lo_w:.0f} km/h at {lo_wt.strftime('%d %b' if daily else '%H:%M')}")
        self._wx_brief = f"<span style='color:{WIND_COLOR}'>Weather:</span> " + " · ".join(wb)
        return (f"<span style='color:{WIND_COLOR}'>🌬 Weather: " + "; ".join(bits) +
                ". Calm wind + a shallow mixing layer (red zone) trap pollution; wind and a deep layer clear it. "
                f"Arrows show where the wind blows to. Source: {weather.SOURCE}.</span>")

    def _main_axis_title(self, daily):
        return "Mean PM2.5 µg/m³" if daily else "PM2.5 µg/m³"

    # ── day view ────────────────────────────────────────────────────────────
    def _draw_day(self):
        start = self.day
        end = (start + timedelta(days=1, hours=12)).replace(hour=0)
        self.period_lbl.setText(start.strftime("%a %d %b %Y"))
        self.mark_lbl.setText("")
        months = {start.strftime("%Y-%m"), (end - timedelta(seconds=1)).strftime("%Y-%m"),
                  (start - timedelta(days=1)).strftime("%Y-%m")}          # the day before: 24-h average
        hourly = [r for m in months for r in self._rows("readings", m)]
        live   = [r for m in months for r in self._rows("live", m)]

        plots, notes, src, stats = [], [], {}, []
        self._texts = {}
        hidden_rings, missing, last_ends = {}, {}, []
        spread_runs, spread_tips = [], {}
        now = datetime.now().astimezone()
        for key, cb in self.checks.items():
            if not cb.isChecked():
                continue
            col, label = TARGET_COLORS[key], self.labels.get(key, key)
            all_rows = sorted(((parse_dt(r["hour_start_local"]), r) for r in hourly if r["target"] == key),
                              key=lambda tr: tr[0] or start)
            c25, c10, ca = self._cols(key)
            rows = [(t, r) for t, r in all_rows if t and start <= t < end and _num(r.get(c25)) is not None]
            if key == "home" and self._area_info():
                label = f"{label} — {METHOD_NAMES[self.method]}"
            tips = {}
            p_s, prev = self._line(col, tips), None
            for t, r in rows:
                if prev is not None and t - prev > timedelta(hours=1):     # gap → break line
                    plots.append(("pm", p_s))
                    p_s = self._line(col, tips)
                xm = _ms(t + timedelta(minutes=30))
                a, p, p10 = _num(r.get(ca)), _num(r.get(c25)), _num(r.get(c10))
                p_s.append(xm, p)
                self._texts[(id(p_s), xm)] = self._point_label(p, a)
                head = (f"{label} — {_short(r.get('station'))}<br>"
                        f"{t.strftime('%H:%M')}–{(t + timedelta(hours=1)).strftime('%H:%M')} average<br>")
                a25 = pm25_to_aqi(p)
                drv = (" — set by PM10" if a is not None and a25 is not None and a > a25 + 0.5 else "")
                aqi_s = (f"US AQI {a:.0f} ({aqi_category_name(a)}){drv}" if a is not None else "US AQI —")
                pm_s = f"PM2.5 {p} µg/m³" + (f" · PM10 {p10:.0f}" if p10 is not None else "")
                used = r.get("monitors_used")
                tail = (f"<br><span style='color:#8a8aa3'>PM2.5 by monitor: {used}</span>" if used else "")
                tips[xm] = head + f"<b>{pm_s}</b><br>{aqi_s}" + tail
                prev = t
                src[key] = [r.get("station"), r.get("source_url"), set()]
            plots.append(("pm", p_s))

            if key == "home" and self.spread_cb.isChecked() and self._area_info():
                mons = self._hour_monitors(hourly, start, end)
                run, prev_t = [], None
                for t, r in rows:
                    vals = mons.get((t + timedelta(minutes=15)).replace(minute=0, second=0, microsecond=0), [])
                    if len(vals) < 2:
                        continue
                    if prev_t is not None and t - prev_t > timedelta(hours=1) and run:
                        spread_runs.append(run); run = []
                    xm = _ms(t + timedelta(minutes=30))
                    run.append((xm, min(vals), max(vals)))
                    spread_tips[xm] = (f"Your {len(vals)} monitors, {t:%H:%M}–{(t + timedelta(hours=1)):%H:%M}:<br>"
                                       f"lowest <b>{min(vals):.0f}</b> · highest <b>{max(vals):.0f}</b> µg/m³ "
                                       f"(gap {max(vals) - min(vals):.0f})<br><span style='color:#8a8aa3'>A wide band "
                                       f"means the monitors disagree — the median ignores one odd monitor.</span>")
                    prev_t = t
                if run:
                    spread_runs.append(run)

            if self.avg_cb.isChecked():
                series = [(t, _num(r.get(c25))) for t, r in all_rows if t and t < end]
                s24 = self._avg24_series(key, series, start, end, label, col)
                if s24.count():
                    plots.append(("pm", s24))

            if self.live_cb.isChecked():
                ltips = {}
                ring = self._dots(col, ltips, size=11, ring=True)
                line_stn = _short(rows[-1][1].get("station")).lower() if rows else ""
                # hours the measured line already covers (monitors label hours at :00 or :30)
                covered = {(t + timedelta(minutes=15)).replace(minute=0, second=0, microsecond=0) for t, _ in rows}
                seen_hours = set()
                for r in live:
                    if r["target"] != key:
                        continue
                    wname = _short(r.get("waqi_station"))
                    both_median = "nearby monitors" in line_stn and "nearby monitors" in wname.lower()
                    if line_stn and line_stn not in wname.lower() and not both_median:   # different monitor
                        hidden_rings.setdefault(label, set()).add(wname)
                        continue
                    t = _local_naive(r.get("station_time_local")) or parse_dt(r.get("polled_at_local"))
                    raw = r.get("waqi_aqi_raw") if r.get("aqi_scale") == "us2024" else r.get("waqi_aqi")
                    a24 = live_row_aqi(r)
                    pm = waqi_to_pm25(raw) if _num(raw) is not None else (aqi_to_pm25(a24) if a24 is not None else None)
                    # WAQI stamps each reading with the END of its hour (checked on 350 hours of your data:
                    # aligned that way, the same monitor agrees with OpenAQ to a median 5 µg/m³; aligned to the
                    # hour's start, 26). So the ring belongs to the hour before t, drawn at that hour's middle,
                    # exactly where the line's point for the same hour sits. Hours the line already covers get no
                    # ring: one number per hour, the measured one. Rings are the live preview of later hours.
                    if not t:
                        continue
                    hour = (t - timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
                    if hour in covered or hour in seen_hours:
                        continue
                    if pm is not None and start <= hour < end:
                        seen_hours.add(hour)
                        xr = _ms(hour + timedelta(minutes=30))
                        ring.append(xr, pm)
                        parts = r.get("waqi_parts")
                        ltips[xr] = (f"Live (WAQI) — {_short(r.get('waqi_station'))}<br>"
                                         f"{hour:%H:%M}–{t:%H:%M} average, not yet published by the government feed<br>"
                                         f"<b>PM2.5 ≈ {pm:.0f} µg/m³</b>"
                                         + (f" (AQI {a24:.0f})" if a24 is not None else "")
                                         + (f"<br><span style='color:#8a8aa3'>estimated from WAQI's index "
                                            f"{float(raw):.0f} (older US table)</span>" if _num(raw) is not None else "")
                                         + (f"<br><span style='color:#8a8aa3'>{parts}</span>" if parts else "")
                                         + ("<br><span style='color:#8a8aa3'>WAQI carries 3 of your 5 monitors, so this "
                                            "can differ a little from the measured line</span>" if key == "home" else ""))
                        src.setdefault(key, [label, "", set()])[2].add(_short(r.get("waqi_station")))
                plots.append(("pm", ring))
                self._rings_drawn = self._rings_drawn or ring.count() > 0

            have = {t.replace(minute=0) for t, _ in rows}
            if have:
                last_ends.append(max(have) + timedelta(hours=1))
            limit = min(end, now - timedelta(hours=2))            # OpenAQ normally lags ~2 h
            h, gaps, run = start, [], None
            while h + timedelta(hours=1) <= limit:
                if h not in have:
                    run = [run[0] if run else h, h + timedelta(hours=1)]
                elif run:
                    gaps.append(run); run = None
                h += timedelta(hours=1)
            if run:
                gaps.append(run)
            missing[label] = gaps

            p25 = [_num(r.get(c25)) for _, r in rows]
            aq = [(_num(r.get(ca)), t) for t, r in rows if _num(r.get(ca)) is not None]
            if p25 and aq:
                pk, pt = max(aq, key=lambda v: v[0])
                notes.append(f"<span style='color:{col}; font-weight:700'>{label}</span>: "
                             f"{len(p25)}/24 h so far · mean PM2.5 <b>{sum(p25)/len(p25):.0f} µg/m³</b> · "
                             f"worst hour AQI <b>{pk:.0f}</b> ({pt.strftime('%H:%M')}–{(pt + timedelta(hours=1)).strftime('%H:%M')})")
                stats.append(f"<span style='color:{col}'><b>{_brief_name(label)}</b></span> mean "
                             f"<b>{sum(p25)/len(p25):.0f}</b> µg/m³ · worst AQI <b>{pk:.0f}</b> at {pt:%H:%M}"
                             + (f" · {len(p25)}/24 h" if len(p25) < 24 else ""))
            else:
                notes.append(f"<span style='color:{col}; font-weight:700'>{label}</span>: no data for this day yet")
                stats.append(f"<span style='color:{col}'><b>{_brief_name(label)}</b></span> no data yet")

        if self.model_cb.isChecked() or self.scaled_cb.isChecked():
            notes += self._model_series(plots, start, end, hourly)

        x_lo, x_hi = _ms(start), _ms(end)
        nt = 7 if self.phone else 13                        # every 4 h on a phone, 2 h on the desktop
        xp = self._x_axis(x_lo, x_hi, "HH:mm", nt, None)
        self._phone_title(start.strftime("%a %d %b"))
        rg = self._ranges or {}
        top_p, ceil_p = self._scale(plots, "pm", rg.get("hour_pm"), fixed=True)
        lab_p, out_p = self._prepare(plots, "pm", ceil_p)
        yp = self._setup_chart(self.main_chart, xp, top_p, self._main_axis_title(False), 1)
        for area in self._spread_series(spread_runs, ceil_p, spread_tips):
            self._add(self.main_chart, area, xp, yp)
        bands = sorted({(g[0], g[1]) for gs in missing.values() for g in gs})
        for g0, g1 in bands:                                  # hatched = no OpenAQ data for these hours
            up, lo = QLineSeries(), QLineSeries()
            up.append(_ms(g0), yp.max()); up.append(_ms(g1), yp.max())
            lo.append(_ms(g0), 0); lo.append(_ms(g1), 0)
            band = QAreaSeries(up, lo)
            up.setParent(band); lo.setParent(band)
            band.setBrush(QBrush(QColor(255, 255, 255, 40), Qt.BDiagPattern))
            band.setPen(QPen(Qt.NoPen))
            self._add(self.main_chart, band, xp, yp)
        pending = None
        if start <= now < end:                                # today: hours OpenAQ hasn't published yet
            floor2h = (now - timedelta(hours=2)).replace(minute=0, second=0, microsecond=0)
            p0 = max(min(last_ends) if last_ends else floor2h, floor2h)
            if p0 < now:
                pending = (p0, now)
                up, lo = QLineSeries(), QLineSeries()
                up.append(_ms(p0), yp.max()); up.append(_ms(now), yp.max())
                lo.append(_ms(p0), 0); lo.append(_ms(now), 0)
                band = QAreaSeries(up, lo)
                up.setParent(band); lo.setParent(band)
                band.setBrush(QBrush(QColor(122, 162, 247, 45), Qt.Dense6Pattern))
                band.setPen(QPen(Qt.NoPen))
                self._add(self.main_chart, band, xp, yp)
        for kind, s in plots:
            if s.count():
                self._add(self.main_chart, s, xp, yp)
        for kind, sr in plots:
            if isinstance(sr, QScatterSeries) and id(sr) not in self._labelled:
                self._layers[0].obstacles += [(sr, p.x(), p.y()) for p in sr.points()]
        self._finish_chart(self.main_chart, self._layers[0], xp, yp, ceil_p, lab_p, out_p, "µg/m³")
        if self.weather_cb.isChecked():
            notes.append(self._draw_weather(start, end, self._x_axis(x_lo, x_hi, "HH:mm", nt, None)))
        notes += self._outlier_notes(out_p, ceil_p, top_p, "%H:%M", "hourly")
        if spread_runs:
            notes.append(f"<span style='color:{TARGET_COLORS['home']}'>▒ Shaded band: lowest to highest of your "
                         f"monitors each hour. Narrow = they agree; wide = one area is much worse, or a monitor "
                         f"is off — the median line isn't pulled by a single odd monitor.</span>")
        if pending:
            notes.append(f"<span style='color:#7aa2f7'>⏳ {pending[0].strftime('%H:%M')}–now (dotted): not missing — "
                         f"OpenAQ publishes each hour about 1–2 h after it ends, and the app fetches it then.</span>")
        for label, gs in missing.items():
            if gs:
                spans = ", ".join(f"{a.strftime('%H:%M')}–{b.strftime('%H:%M')}" for a, b in gs)
                notes.append(f"<span style='color:#f59e42'>⚠ {label}: no hourly data from OpenAQ for {spans} "
                             f"(hatched). The app re-checks every 15 min and fills these in if OpenAQ publishes them.</span>")
        for label, names in hidden_rings.items():
            notes.append(f"<span style='color:#8a8aa3'>WAQI rings hidden for {label}: WAQI's nearest monitor is "
                         f"{', '.join(sorted(names))}, a different monitor, so comparing them would mislead.</span>")
        lag = ("<br><span style='color:#8a8aa3'>Lines = OpenAQ hourly averages (arrive ~2 h late). Each label: PM2.5 "
               "µg/m³ (peak, low and latest also show the AQI; the right axis gives the AQI at any height); “PM10” "
               "marks hours when PM10, not PM2.5, sets the AQI. "
               "Rings = live readings (WAQI) for the hours the government feed hasn't published yet, turned back "
               "into PM2.5 and drawn at the middle of their hour, like the line's points. Once an hour is published "
               "its ring disappears and the measured point takes its place.</span>")
        self._set_source(src)
        status = []
        if pending:
            status.append(f"⏳ from {pending[0]:%H:%M} not published yet (OpenAQ is 1–2 h behind)")
        for label, gs in missing.items():
            if gs:
                status.append(f"⚠ {_brief_name(label)}: no data " + ", ".join(f"{a:%H:%M}–{b:%H:%M}" for a, b in gs))
        main_out = sorted({(px, py) for _, px, py, _, m in out_p if m})
        if main_out:
            status.append("▲ above the axis: " + ", ".join(
                f"{datetime.fromtimestamp(px / 1000).astimezone():%H:%M} {py:.0f}" for px, py in main_out))
        self._set_summary(stats, status, notes + [lag])

    # ── month view ──────────────────────────────────────────────────────────
    def _draw_month(self):
        first = self.day.replace(day=1)
        month = first.strftime("%Y-%m")
        nxt = (first + timedelta(days=32)).replace(day=1)
        ndays = (nxt - first).days
        self.period_lbl.setText(first.strftime("%B %Y"))
        self.mark_lbl.setText("●  full day (≥ 18 h)    ○  incomplete day")
        daily = self._rows("daily", month)

        plots, notes, src, stats = [], [], {}, []
        self._texts = {}
        spread_runs, spread_tips = [], {}
        for key, cb in self.checks.items():
            if not cb.isChecked():
                continue
            col, label = TARGET_COLORS[key], self.labels.get(key, key)
            rows = sorted((r for r in daily if r["target"] == key), key=lambda r: r["date"])
            if key == "home" and self._area_info():
                label = f"{label} — {METHOD_NAMES[self.method]}"
            tips = {}
            p_s = self._line(col, tips)
            p_s.setPointsVisible(False)
            full_p, part_p = self._dots(col, tips), self._dots(col, tips, hollow=True)
            good, prev_d = [], None
            for r in rows:
                d = datetime.fromisoformat(r["date"]).astimezone()
                if prev_d is not None and (d - prev_d) > timedelta(days=1, hours=2):   # missing days → break line
                    plots.append(("pm", p_s))
                    p_s = self._line(col, tips)
                    p_s.setPointsVisible(False)
                prev_d = d
                x = _ms(d.replace(hour=12))
                if key == "home" and self.method != "median" and self._area_info():
                    a = _num(r.get(f"aqi_daily_{self.method}_method"))
                    p = _num(r.get(f"pm25_daily_{self.method}_method"))
                else:
                    a, p = _num(r.get("aqi_daily_us")), _num(r.get("pm25_mean_ugm3"))
                ok = r.get("epa_valid_24h") == "yes"
                if p is not None:
                    p_s.append(x, p); (full_p if ok else part_p).append(x, p)
                    self._texts[(id(p_s), x)] = self._point_label(p, a)
                if ok and a is not None:
                    good.append((a, r["date"], p))
                head = f"{label} — {d.strftime('%a %d %b')}<br>"
                a25 = pm25_to_aqi(p) if p is not None else None
                drv = (" — set by PM10" if a is not None and a25 is not None and a > a25 + 0.5 else "")
                aqi_s = f"Daily US AQI {a:.0f} ({aqi_category_name(a)}){drv}" if a is not None else "Daily US AQI —"
                pm_s = f"Mean PM2.5 {p} µg/m³"
                tail = (f"<br>{r.get('pm25_hours')}/24 h of data" + ("" if ok else " (incomplete)")
                        + (f"<br>Worst hour: AQI {r.get('aqi_hourly_max')} at {r.get('aqi_hourly_max_at')}"
                           if r.get("aqi_hourly_max") else ""))
                tips[x] = head + f"<b>{pm_s}</b><br>{aqi_s}" + tail
                src[key] = [r.get("station"), r.get("source_url"), set()]
            plots += [("pm", p_s), ("pm", full_p), ("pm", part_p)]
            if good:
                worst, best = max(good), min(good)
                pms = [g[2] for g in good if g[2] is not None]
                notes.append(f"<span style='color:{col}; font-weight:700'>{label}</span>: "
                             f"{len(good)} full day(s) · month mean PM2.5 <b>{sum(pms)/len(pms):.0f} µg/m³</b> · "
                             f"worst day AQI <b>{worst[0]:.0f}</b> ({worst[1][8:]}) · best <b>{best[0]:.0f}</b> ({best[1][8:]})")
                stats.append(f"<span style='color:{col}'><b>{_brief_name(label)}</b></span> month mean "
                             f"<b>{sum(pms)/len(pms):.0f}</b> µg/m³ · worst day AQI <b>{worst[0]:.0f}</b> "
                             f"(day {int(worst[1][8:])}) · best {best[0]:.0f}")
            else:
                notes.append(f"<span style='color:{col}; font-weight:700'>{label}</span>: no full days this month yet")
                stats.append(f"<span style='color:{col}'><b>{_brief_name(label)}</b></span> no full days yet")

            if key == "home" and self.spread_cb.isChecked() and self._area_info():
                per = {}                                   # (monitor, date) → hourly values
                for r in self._rows("readings", month):
                    tg = str(r.get("target", ""))
                    v = _num(r.get("pm25_ugm3"))
                    if tg.startswith("home|") and v is not None and r.get("status") in ("ok", None, ""):
                        per.setdefault((tg, r["hour_start_local"][:10]), []).append(v)
                by_day = {}
                for (tg, dstr), vs in per.items():
                    if len(vs) >= 12:                      # a monitor's day counts with ≥ half its hours
                        by_day.setdefault(dstr, []).append(statistics.fmean(vs))
                run, prev_dd = [], None
                for dstr in sorted(by_day):
                    vs = by_day[dstr]
                    if len(vs) < 2:
                        continue
                    dd = datetime.fromisoformat(dstr).astimezone()
                    if prev_dd is not None and dd - prev_dd > timedelta(days=1, hours=2) and run:
                        spread_runs.append(run); run = []
                    x = _ms(dd.replace(hour=12))
                    run.append((x, min(vs), max(vs)))
                    spread_tips[x] = (f"Your {len(vs)} monitors' daily means, {dd:%a %d %b}:<br>lowest "
                                      f"<b>{min(vs):.0f}</b> · highest <b>{max(vs):.0f}</b> µg/m³")
                    prev_dd = dd
                if run:
                    spread_runs.append(run)

        if self.model_cb.isChecked() or self.scaled_cb.isChecked():
            notes += self._model_series(plots, first, nxt, None, daily_rows=daily)

        x_lo = _ms(first.replace(hour=12))
        x_hi = _ms((first + timedelta(days=ndays - 1, hours=2)).replace(hour=12))
        ticks = ndays                                   # one tick per day, on the data points
        if self.phone:
            ticks = 8
            self._phone_title(first.strftime("%B %Y") + " · daily")
        xp = self._x_axis(x_lo, x_hi, "dd", ticks, None)
        rg = self._ranges or {}
        top_p, ceil_p = self._scale(plots, "pm", rg.get("day_pm"), fixed=True)
        lab_p, out_p = self._prepare(plots, "pm", ceil_p)
        yp = self._setup_chart(self.main_chart, xp, top_p, self._main_axis_title(True), 1)
        for area in self._spread_series(spread_runs, ceil_p, spread_tips):
            self._add(self.main_chart, area, xp, yp)
        for kind, s in plots:
            if s.count():
                self._add(self.main_chart, s, xp, yp)
        self._finish_chart(self.main_chart, self._layers[0], xp, yp, ceil_p, lab_p, out_p, "µg/m³")
        if self.weather_cb.isChecked():
            notes.append(self._draw_weather(first, nxt, self._x_axis(x_lo, x_hi, "dd", ticks, None), daily=True))
        notes += self._outlier_notes(out_p, ceil_p, top_p, "%d %b", "daily")
        if spread_runs:
            notes.append(f"<span style='color:{TARGET_COLORS['home']}'>▒ Shaded band: lowest to highest of your "
                         f"monitors' daily means.</span>")
        notes.append("<span style='color:#8a8aa3'>Each label: mean PM2.5 (the day's AQI, from the 24-hour mean); "
                     "“PM10” marks days when PM10, not PM2.5, sets the AQI.</span>")
        self._set_source(src)
        main_out = sorted({(px, py) for _, px, py, _, m in out_p if m})
        status = (["▲ above the axis: " + ", ".join(
            f"{datetime.fromtimestamp(px / 1000).astimezone():%d %b} {py:.0f}" for px, py in main_out)]
                  if main_out else [])
        self._set_summary(stats, status, notes)

    # ── indoor view ─────────────────────────────────────────────────────────
    def _draw_indoor(self):
        """Purifier view — one full-size chart: each purifier's indoor PM2.5 (green: faint = every
        30-s reading, bold = 2.5-min rolling median) against your area's outdoor PM2.5 (red, hourly),
        zoomed to the hours the purifier was logging."""
        start = self.day
        end = (start + timedelta(days=1, hours=12)).replace(hour=0)
        self.period_lbl.setText(start.strftime("Purifier · %a %d %b %Y"))
        live = start.date() == datetime.now().astimezone().date()
        self.mark_lbl.setText(f"<span style='color:{INDOOR_COLORS[0]}'>●</span> Live · updated "
                              f"{datetime.now():%H:%M:%S} · refreshes every minute (purifier read every 30 s)"
                              if live else "")
        self.mark_lbl.setTextFormat(Qt.RichText)
        self.method_lbl.setText(
            f"<span style='color:{INDOOR_COLORS[0]}'><b>━━ Indoor</b></span> = purifier's own PM2.5 sensor "
            "(faint = every 30-s reading, bold = 2.5-min smoothed; grey dots = purifier off; orange ▲ = logged event) "
            f"&nbsp;·&nbsp; <span style='color:{OUTDOOR_COLOR}'><b>━━ Outdoor</b></span> = your area's PM2.5, hourly")
        self.method_lbl.setTextFormat(Qt.RichText)
        data = self._indoor_rows(start, end)
        layer = self._layers[0]
        outdoor = {}
        for m in {start.strftime("%Y-%m"), (end - timedelta(seconds=1)).strftime("%Y-%m")}:
            for r in self._rows("readings", m):
                t = parse_dt(r["hour_start_local"])
                v = _num(r.get("pm25_ugm3"))
                if r["target"] == "home" and v is not None and t and start <= t < end:
                    outdoor[t.replace(minute=0, second=0, microsecond=0)] = v
        plots, notes, ev_series, half_labels = [], [], [], []
        for i, name in enumerate(sorted({r["device"] for _, _, r in data})):
            col = INDOOR_COLORS[i % len(INDOOR_COLORS)]
            tips, ev_tips = {}, {}
            faint = QColor(col); faint.setAlpha(80)
            new_pair = lambda: (self._plain_line(faint, tips, width=1.0), self._plain_line(col, tips, width=2.8))
            line, smooth = new_pair()
            window, runs, per_hour, room = [], [[]], {}, ""
            off = self._dots("#55556a", tips, size=4)
            events = self._dots("#f59e42", ev_tips, size=11)
            events.setMarkerShape(QScatterSeries.MarkerShapeTriangle)
            ev_series.append(events)
            prev, vals, n_ev, last_half = None, [], 0, None
            for t, pm, r in data:
                if r["device"] != name:
                    continue
                room = r.get("room") or room
                x = _ms(t)
                o = outdoor.get(t.replace(minute=0, second=0, microsecond=0))
                tips[x] = (f"{name} — {r.get('room')}<br>{t:%H:%M:%S} · <b>indoor PM2.5 {pm:.0f} µg/m³</b> · "
                           f"{r.get('power')} · {r.get('mode')}"
                           + (f"<br>outdoor that hour {o:.0f} → <b>{100 * (1 - pm / o):.0f}% lower</b> indoors" if o else "")
                           + (f"<br><span style='color:#f59e42'>{r['event']}</span>" if r.get("event") else ""))
                ev_txt = "; ".join(e for e in (r.get("event") or "").split("; ")
                                   if not e.startswith("sudden rise"))   # old detector: mostly sensor jitter
                if ev_txt:
                    events.append(x, pm); ev_tips[x] = tips[x]; n_ev += 1
                if r.get("power") != "on":
                    off.append(x, pm)
                    if runs[-1]:
                        runs.append([])
                    if line.count():                   # purifier off → end this stretch of line
                        plots += [("pm", line), ("pm", smooth)]
                        line, smooth = new_pair()
                    prev, window = None, []
                    continue
                if prev is not None and (t - prev) > timedelta(minutes=5):
                    if runs[-1]:
                        runs.append([])
                    plots += [("pm", line), ("pm", smooth)]
                    line, smooth = new_pair()
                    window = []
                window = (window + [pm])[-5:]
                sm = statistics.median(window)
                line.append(x, pm); smooth.append(x, sm)
                runs[-1].append((t, sm, r.get("event") or ""))
                per_hour.setdefault(t.replace(minute=0, second=0, microsecond=0), []).append(pm)
                half = t.replace(minute=0 if t.minute < 30 else 30, second=0, microsecond=0)
                if half != last_half:                  # one indoor value label per half hour
                    half_labels.append((smooth, x, sm, col))
                    last_half = half
                vals.append(pm)
                prev = t
            plots += [("pm", line), ("pm", smooth), ("pm", off), ("pm", events)]
            if vals:
                both = [(statistics.mean(v), outdoor[h]) for h, v in per_hour.items() if outdoor.get(h)]
                line_txt = (f"<span style='color:{col}; font-weight:700'>{name}</span> ({room}): "
                            f"{len(vals)} readings while on · lowest {min(vals):.0f} · median "
                            f"{statistics.median(vals):.0f} µg/m³")
                if both:
                    ind = statistics.mean(a for a, _ in both)
                    out = statistics.mean(b for _, b in both)
                    line_txt += (f" · in the {len(both)} hour(s) with an outdoor value: indoor {ind:.0f} vs outdoor "
                                 f"{out:.0f} µg/m³ → <b>{100 * (1 - ind / out):.0f}% lower indoors</b>")
                waiting = len(per_hour) - len(both)
                if waiting:
                    line_txt += f" · {waiting} hour(s) still waiting for OpenAQ's outdoor value (1–2 h lag)"
                notes.append(line_txt)
                for run in runs:
                    notes += self._cleandown(run, col)
        # time axis zoomed to the hours with purifier readings (± 30 min, at least 3 h)
        ts = [t for t, _, _ in data]
        lo, hi = start, end
        if ts:
            lo = max(start, (min(ts) - timedelta(minutes=30)).replace(minute=0, second=0, microsecond=0))
            hi = min(end, (max(ts) + timedelta(minutes=89)).replace(minute=0, second=0, microsecond=0))
            if hi - lo < timedelta(hours=3):
                hi = min(end, lo + timedelta(hours=3))
                lo = max(start, hi - timedelta(hours=3))
        out_s, out_tips = None, {}
        if outdoor:
            out_s = self._dots(OUTDOOR_COLOR, out_tips, size=8)       # points; line added below
            out_line = self._plain_line(OUTDOOR_COLOR, out_tips, width=2.8)
            for h, v in sorted(outdoor.items()):
                if lo - timedelta(hours=1) <= h <= hi:                  # enters/leaves the window
                    x = _ms(h + timedelta(minutes=30))
                    out_line.append(x, v); out_s.append(x, v)
                    out_tips[x] = (f"Outdoor — your area<br>{h:%H:%M}–{h + timedelta(hours=1):%H:%M} average<br>"
                                   f"<b>PM2.5 {v:.0f} µg/m³</b>")
            plots += [("pm", out_line), ("pm", out_s)]
        hours = max(1, round((hi - lo).total_seconds() / 3600))
        if hours > 12 and hours % 2 and hi < end:            # make the span splittable into whole hours
            hi += timedelta(hours=1); hours += 1
        step = next(k for k in range(min(hours, 7 if self.phone else 12), 0, -1) if hours % k == 0)
        if self.phone:
            self._layers[0].set_header(
                f"<span style='color:{TEXT}'>{start:%a %d %b}</span>&nbsp;&nbsp;"
                f"<span style='color:{INDOOR_COLORS[0]}'>━ Indoor (purifier)</span>&nbsp;&nbsp;"
                f"<span style='color:{OUTDOOR_COLOR}'>━ Outdoor (your area)</span>", 9)
        xa = self._x_axis(_ms(lo), _ms(hi), "HH:mm", step + 1,
                          "Time of day" + ("" if (lo, hi) == (start, end) else "  ·  zoomed to the hours the purifier was logging"))
        top, ceil = self._scale(plots, "pm", (self._ranges or {}).get("hour_pm"))
        lab, outl = self._prepare(plots, "pm", ceil)
        ya = self._setup_chart(self.main_chart, xa, top, "PM2.5  µg/m³", 1)
        for _, sr in plots:
            if sr.count():
                self._add(self.main_chart, sr, xa, ya)
        if self.values_cb.isChecked():
            if out_s is not None:
                for p in out_s.points():
                    layer.add(out_s, p.x(), p.y(), f"{p.y():.0f}", QColor(OUTDOOR_COLOR).lighter(130).name(), 1)
            ev_x = [p.x() for ev in ev_series for p in ev.points()]
            for sr, x, v, col in half_labels:
                if not any(abs(x - e) < 120_000 for e in ev_x):      # event label already shows it
                    layer.add(sr, x, v, f"{v:.0f}", QColor(col).lighter(120).name(), 2)
        for ev in ev_series:
            for p in ev.points():
                layer.add(ev, p.x(), p.y(), f"{p.y():.0f}", "#f59e42", 1)
        self._finish_chart(self.main_chart, layer, xa, ya, ceil, [], outl, "µg/m³")
        self.summary.setText("<br>".join(notes) if notes else
                             "No purifier readings for this day (indoor logging may be paused, or the purifier "
                             "isn't set up under \"indoor\" in config.json).")
        self.source.setText("Indoor: purifier's own sensor via local Wi-Fi (data/indoor_YYYY-MM.csv) · Outdoor: "
                            "OpenAQ hourly median of your area's monitors (arrives 1–2 h late).")

    def _model_series(self, plots, start, end, hourly, daily_rows=None):
        """Satellite model (CAMS) lines and a plain-language reading of how the monitors compare.
        ┄ raw model PM2.5 (its own level — usually far below Delhi's monitors in winter)
        ┈ model × its usual gap to your monitors (median ratio over the 7 days before this day, or over
          this month in Month view): if your line follows this one, the change is weather/regional;
          if it breaks away, the cause is local — or a monitor is off."""
        import cams
        months = sorted({start.strftime("%Y-%m"), (end - timedelta(seconds=1)).strftime("%Y-%m")})
        rows = []
        for r in cams.read(self.store.dir, months):
            try:
                t = datetime.fromisoformat(r["time_local"]).astimezone()
                v = float(r["pm25_model"])
            except (KeyError, ValueError):
                continue
            if start <= t < end:
                rows.append((t, v, r.get("aod")))
        if not rows:
            return [f"<span style='color:{MODEL_COLOR}'>┄ Satellite model: no data for this period yet "
                    f"(fetched hourly once the app is running; history from Aug 2022).</span>"]
        c25 = self._cols("home")[0]
        if daily_rows is None:                                   # Day view: hourly
            pts = [(_ms(t), v, t, aod) for t, v, aod in sorted(rows)]
            meas = {parse_dt(r["hour_start_local"]).replace(minute=0, second=0, microsecond=0): _num(r.get(c25))
                    for r in hourly if r["target"] == "home" and _num(r.get(c25)) is not None
                    and parse_dt(r["hour_start_local"])}
            pairs = [(meas[t.replace(minute=0, second=0, microsecond=0)], v) for t, v, _ in rows
                     if t.replace(minute=0, second=0, microsecond=0) in meas]
            unit = "hours"
            usual, n_usual = self._usual_ratio(start, 7, c25)
            usual_txt = "the 7 days before"
        else:                                                    # Month view: daily means
            by = {}
            for t, v, _ in rows:
                by.setdefault(t.date(), []).append(v)
            pts = [(_ms(datetime.combine(d, datetime.min.time()).astimezone().replace(hour=12)), statistics.mean(vs),
                    d, None) for d, vs in sorted(by.items())]
            meas = {r["date"]: _num(r.get("pm25_mean_ugm3")) for r in daily_rows
                    if r["target"] == "home" and _num(r.get("pm25_mean_ugm3")) is not None}
            pairs = [(meas[d.isoformat()], statistics.mean(vs)) for d, vs in by.items() if d.isoformat() in meas]
            unit = "days"
            usual, n_usual = self._usual_ratio(min(end, datetime.now().astimezone()), 30, c25)
            usual_txt = "the last 30 days"
        tips = {}
        if self.model_cb.isChecked():
            line = QLineSeries()
            pen = QPen(QColor(MODEL_COLOR)); pen.setWidthF(2.0 * self.pen_scale); pen.setStyle(Qt.DashLine)
            line.setPen(pen)
            line.hovered.connect(lambda p, on: on and self._tip(tips, p))
            self._no_rank.add(id(line))
            for x, v, t, aod in pts:
                line.append(x, v)
            plots.append(("pm", line))
        if self.scaled_cb.isChecked() and usual:
            sl = QLineSeries()
            pen = QPen(QColor(MODEL_COLOR).lighter(135)); pen.setWidthF(1.8 * self.pen_scale); pen.setStyle(Qt.DotLine)
            sl.setPen(pen)
            sl.hovered.connect(lambda p, on: on and self._tip(tips, p))
            self._no_rank.add(id(sl))
            for x, v, t, aod in pts:
                sl.append(x, v * usual)
            plots.append(("pm", sl))
        for x, v, t, aod in pts:
            when = t.strftime("%H:%M") if daily_rows is None else t.strftime("%a %d %b") + " (daily mean)"
            tips[x] = (f"Satellite model (CAMS) {when}<br><b>PM2.5 {v:.0f} µg/m³</b>"
                       + (f" · aerosol optical depth {float(aod):.2f}" if _num(aod) is not None else "")
                       + (f"<br>× usual gap {usual:.1f} = <b>{v * usual:.0f}</b> — where your monitors would be "
                          f"if today were a normal day for this weather" if usual else "")
                       + "<br><span style='color:#8a8aa3'>~45 km grid, computed from weather and satellite aerosol "
                         "data, independent of the ground monitors</span>")
        mv = statistics.mean(v for _, v, _, _ in pts)
        out = [f"<span style='color:{MODEL_COLOR}'>┄ Satellite model (CAMS, ~45 km grid): mean PM2.5 {mv:.0f} µg/m³"]
        if pairs and usual:
            today = statistics.median(a / b for a, b in pairs if b > 0)
            dev = today / usual
            if 0.6 <= dev <= 1.6:
                verdict = ("<b>your monitors follow the model</b> — the ups and downs are weather or regional "
                           "(smoke arriving, wind dropping), not something on your street")
            elif dev > 1.6:
                verdict = ("<b>your monitors are well above their usual gap</b> — a local source the 45 km model "
                           "can't see (burning, traffic, a fire), or a monitor reading high")
            else:
                verdict = ("<b>your monitors are well below their usual gap</b> — local air cleaner than the region, "
                           "or a monitor reading low (e.g. water sprinkled around it)")
            short = ("they follow it — weather / regional" if 0.6 <= dev <= 1.6 else
                     "above usual — local source, or a monitor reading high" if dev > 1.6 else
                     "below usual — cleaner locally, or a monitor reading low")
            self._model_brief = (f"<span style='color:{MODEL_COLOR}'>Satellite check:</span> monitors "
                                 f"{today:.1f}× the model (usual {usual:.1f}×) → {short}")
            out[0] += (f" · over these {len(pairs)} {unit} your monitors read <b>{today:.1f}×</b> the model; "
                       f"their usual gap ({usual_txt}) is <b>{usual:.1f}×</b>, so today's gap is {dev:.1f}× the usual "
                       f"(within 0.6–1.6× counts as normal) → {verdict}.")
            out.append(f"<span style='color:#8a8aa3'>┈ Dotted = model × {usual:.1f}. The model averages a ~45 km "
                       "square and misses street-level sources and the shallow winter inversion, so it reads lower "
                       "than city monitors; its SHAPE is the useful part. If your line tracks the dotted one, the "
                       "model explains the change.</span>")
        elif pairs:
            g = statistics.mean(a for a, _ in pairs)
            mm = statistics.mean(b for _, b in pairs)
            if mm:
                self._model_brief = (f"<span style='color:{MODEL_COLOR}'>Satellite check:</span> monitors "
                                     f"{g / mm:.1f}× the model")
            out[0] += (f" · over the same {len(pairs)} {unit} your area measured {g:.0f} → monitors read "
                       f"{g / mm:.1f}× the model (not enough history yet to know their usual gap)" if mm else "")
        out[0] += "</span>" if out[0].endswith(".") else ".</span>"
        return out

    def _indoor_rows(self, start, end):
        import csv as _csv
        rows = []
        for m in {start.strftime("%Y-%m"), (end - timedelta(seconds=1)).strftime("%Y-%m")}:
            try:
                with open(os.path.join(self.store.dir, f"indoor_{m}.csv"), encoding="utf-8-sig", errors="replace",
                          newline="") as f:
                    rows += list(_csv.DictReader(f))
            except OSError:
                pass
        out = []
        for r in rows:
            if r.get("status") != "ok":
                continue
            t = _local_naive(r.get("timestamp")) or _excel_dt(r.get("date"), r.get("time"))
            pm = _num(r.get("pm25_ugm3"))
            if t and pm is not None and start <= t < end:
                out.append((t, pm, r))
        return sorted(out, key=lambda x: x[0])

    @staticmethod
    def _cleandown(run, col):
        """How fast one switched-on stretch cleaned the room (from the smoothed readings)."""
        if len(run) < 10 or run[-1][0] - run[0][0] < timedelta(minutes=15):
            return []
        t0, v0 = run[0][0], max(v for t, v, _ in run if t - run[0][0] <= timedelta(minutes=2))
        lo_t, lo_v = min(((t, v) for t, v, _ in run), key=lambda x: x[1])
        half = next((t for t, v, _ in run if v <= v0 / 2), None)
        at30 = next((v for t, v, _ in run if t - t0 >= timedelta(minutes=30)), None)
        bits = [f"{t0:%H:%M}–{run[-1][0]:%H:%M}: started at {v0:.0f}"]
        if half:
            bits.append(f"halved in {(half - t0).total_seconds() / 60:.0f} min")
        if at30 is not None:
            ch = 100 * (at30 / v0 - 1)
            bits.append(f"{at30:.0f} after 30 min ({'−' if ch < 0 else '+'}{abs(ch):.0f}%"
                        + (", it rose — something added particles" if ch > 10 else "") + ")")
        bits.append(f"lowest {lo_v:.0f} at {lo_t:%H:%M}")
        if v0 - lo_v < 10:
            bits.append("(already clean when switched on — nothing to measure)")
        return [f"&nbsp;&nbsp;<span style='color:{col}'>▸</span> Clean-down {', '.join(bits)}"]

    def _plain_line(self, color, tips, width=2.0):
        s = QLineSeries()
        pen = QPen(QColor(color)); pen.setWidthF(width * self.pen_scale)
        s.setPen(pen)
        s.hovered.connect(lambda p, on: on and self._tip(tips, p))
        return s

    def _set_source(self, src):
        lines = ["Source: OpenAQ hourly averages from government monitors · AQI computed here from "
                 "PM2.5/PM10 with US EPA 2024 breakpoints."]
        for key, (station, url, waqi) in src.items():
            line = f"{self.labels.get(key, key)} → {station}  {url or ''}"
            if waqi:
                w = ", ".join(sorted(waqi))
                same = _short(station).lower() in w.lower() or \
                    ("nearby monitors" in str(station).lower() and "nearby monitors" in w.lower())
                line += f"   ·   WAQI rings: {w}" + ("" if same else "  (nearest WAQI monitor — no WAQI feed found under this station's name)")
            lines.append(line)
        self.source.setText("\n".join(lines))


def aqi_category_name(a):
    from aqi_math import category
    return category(a)


def _nice_tight(v):
    """Smallest round axis top ≥ v (6 equal ticks), no extra headroom."""
    for tick in (5, 10, 20, 25, 50, 75, 100, 125, 150, 200, 250, 300, 400, 500, 750, 1000):
        if v <= tick * 6:
            return tick * 6
    return int(-(-v // 6000) * 6000)


def _nice(v):
    """Axis max that splits into 6 round ticks (tickCount = 7)."""
    v = max(float(v), 10.0) * 1.08
    for tick in (5, 10, 20, 25, 50, 75, 100, 125, 150, 200, 250, 300, 400, 500):
        if v <= tick * 6:
            return tick * 6
    return int(-(-v // 3000) * 3000)
