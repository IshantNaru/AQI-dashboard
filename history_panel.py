"""
History window — charts of what the background recorder has logged.

Two stacked charts sharing the same time axis (no dual y-axes):
  top    : US AQI, background shaded by AQI category
  bottom : PM2.5 µg/m³, shaded by the same categories (EPA 2024 PM2.5 breakpoints)
One colour per location, solid lines everywhere. Hollow rings = WAQI live readings.

Day view:   x = time of day (each hourly average plotted at mid-hour).
Month view: one point per day — EPA daily AQI (from the 24-h mean) and mean PM2.5.
            Filled dot = valid day (≥ 18 h), hollow = incomplete day.
"""
from datetime import datetime, timedelta

from PySide6.QtCore import Qt, QDateTime, QUrl, QMargins, QPointF, QRectF
from PySide6.QtGui import QColor, QPen, QPainter, QDesktopServices, QBrush, QFont
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QCheckBox, QButtonGroup, QToolTip, QSizePolicy,
)
from PySide6.QtCharts import (
    QChart, QChartView, QLineSeries, QScatterSeries, QAreaSeries,
    QDateTimeAxis, QValueAxis, QCategoryAxis,
)

import json
import os
import statistics

from recorder import Store, DATA_DIR, parse_dt, _num, METHOD_COLS, METHOD_NAMES, _label
from aqi_math import live_row_aqi

TARGET_COLORS = {"home": "#7aa2f7", "tracked": "#ff7a93"}
INDOOR_COLORS = ["#3ecf8e", "#f7d154", "#c084fc", "#38bdf8"]
OUTDOOR_COLOR = "#ef4444"
MODEL_COLOR = "#c9b38a"          # satellite model (CAMS): sand-coloured dashed line
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
        self.header = None
        chart.plotAreaChanged.connect(self.layout)

    def add(self, series, x, y, text, color, prio, size=7, bold=True, corner=False, value=None):
        from PySide6.QtWidgets import QGraphicsSimpleTextItem
        it = QGraphicsSimpleTextItem(text, self.chart)
        f = QFont("Segoe UI", max(6, round(size * self.scale)))
        f.setBold(bold)
        it.setFont(f)
        it.setBrush(QBrush(QColor(color)))
        it.setZValue(100)
        it.hide()
        self.entries.append({"prio": prio, "series": series, "x": x, "y": y, "item": it, "corner": corner,
                             "v": y if value is None else value, "marker": False})

    def set_header(self, html, pt):
        from PySide6.QtWidgets import QGraphicsTextItem
        if self.header is None:
            self.header = QGraphicsTextItem(self.chart)
            self.header.setZValue(101)
        f = QFont("Segoe UI", max(6, round(pt * self.scale)))
        f.setBold(True)
        self.header.setFont(f)
        self.header.setHtml(html)

    def add_outlier(self, series, x, y_top, value, color, main=True):
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
        self.add(series, x, y_top, f"{value:.0f}", QColor(color).lighter(125).name(), 0 if main else 3,
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
        for e in self.entries:
            e["pt"] = self.chart.mapToPosition(QPointF(e["x"], e["y"]), e["series"])
        # every data point is an obstacle, so labels never sit on a marker
        placed = [QRectF(e["pt"].x() - 4, e["pt"].y() - (12 if e["marker"] else 4), 8, 16 if e["marker"] else 8)
                  for e in self.entries if not e["corner"]]
        if self.header is not None:                      # legend pinned to the top-left corner
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
                cands = [below, above, right, left] if lower else [above, below, right, left]
            spot = next((r for r in cands if bounds.contains(r) and
                         not any(r.intersects(q) for q in placed)), None)
            if spot is None:
                it.hide()
                continue
            placed.append(spot.adjusted(-1, -1, 1, 1))
            it.setPos(spot.topLeft())
            it.show()


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
        folder_btn = QPushButton("Open data folder"); folder_btn.setStyleSheet(BTN)
        folder_btn.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(self.store.dir)))
        bar.addWidget(folder_btn)
        root.addLayout(bar)

        # ── legend (doubles as the show/hide toggles) ───────────────────────
        leg = QHBoxLayout()
        leg.setSpacing(22)
        self.checks = {}
        for key in ("home", "tracked"):
            cb = QCheckBox(f"━━  {self.labels.get(key, key)}")
            cb.setChecked(True)
            cb.setStyleSheet(f"QCheckBox {{ color:{TARGET_COLORS[key]}; font-weight:700; font-size:13px; }}")
            cb.toggled.connect(self.reload)
            self.checks[key] = cb
            leg.addWidget(cb)
        self.model_cb = QCheckBox("┄  Satellite model (CAMS)")
        self.model_cb.setChecked(True)
        self.model_cb.setToolTip("Copernicus CAMS: a model corrected with NASA satellite aerosol data, ~45 km grid, "
                                 "independent of the ground monitors. Use it for the trend, not the exact level.")
        self.model_cb.setStyleSheet(f"QCheckBox {{ color:{MODEL_COLOR}; font-size:12px; font-weight:700; }}")
        self.model_cb.toggled.connect(self.reload)
        leg.addWidget(self.model_cb)
        self.live_cb = QCheckBox("○  WAQI live reading (second source)")
        self.live_cb.setChecked(True)
        self.live_cb.setStyleSheet(f"QCheckBox {{ color:{TEXT}; font-size:12px; }}")
        self.live_cb.toggled.connect(self.reload)
        leg.addWidget(self.live_cb)
        self.values_cb = QCheckBox("123  Show values on points")
        self.values_cb.setChecked(True)
        self.values_cb.setStyleSheet(f"QCheckBox {{ color:{TEXT}; font-size:12px; }}")
        self.values_cb.toggled.connect(self.reload)
        leg.addWidget(self.values_cb)
        self.mark_lbl = QLabel("")
        self.mark_lbl.setStyleSheet(f"color:{MUTED}; font-size:12px;")
        leg.addWidget(self.mark_lbl)
        leg.addStretch()
        bands = "  ".join(f"<span style='color:{c}'>■</span> {n}" for _, _, n, c in BANDS)
        self.method = "median"
        band_lbl = QLabel(f"<span style='color:{MUTED}'>Shading:</span> {bands}")
        band_lbl.setStyleSheet("font-size:11px;")
        root.addLayout(leg)

        mrow = QHBoxLayout()
        mrow.setSpacing(8)
        self.mlab = QLabel("Your area method:")
        self.mlab.setStyleSheet(f"color:{MUTED};")
        mrow.addWidget(self.mlab)
        self.method_btns = {}
        mgrp = QButtonGroup(self)
        for m, txt in (("median", "Median"), ("mean", "Average"), ("idw", "Distance-weighted")):
            b = QPushButton(txt); b.setCheckable(True); b.setStyleSheet(BTN)
            b.clicked.connect(lambda _=False, mm=m: self._set_method(mm))
            mgrp.addButton(b); mrow.addWidget(b); self.method_btns[m] = b
        self.method_btns["median"].setChecked(True)
        self.method_lbl = QLabel("")
        self.method_lbl.setWordWrap(True)
        self.method_lbl.setStyleSheet(f"color:{TEXT}; font-size:12px; padding-left:10px;")
        mrow.addWidget(self.method_lbl, 1)
        mrow.addWidget(band_lbl, 0, Qt.AlignTop)
        root.addLayout(mrow)

        # ── charts ──────────────────────────────────────────────────────────
        self.aqi_chart, self.pm_chart = self._new_chart(), self._new_chart()
        self.aqi_view, self.pm_view = QChartView(self.aqi_chart), QChartView(self.pm_chart)
        for v in (self.aqi_view, self.pm_view):
            v.setRenderHint(QPainter.Antialiasing)
            v.setStyleSheet(f"background:{BG}; border:none;")
            v.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
            root.addWidget(v, 1)

        self.summary = QLabel("")
        self.summary.setWordWrap(True)
        self.summary.setTextFormat(Qt.RichText)
        root.addWidget(self.summary)
        self.source = QLabel("")
        self.source.setWordWrap(True)
        self.source.setStyleSheet(f"color:{MUTED}; font-size:11px;")
        root.addWidget(self.source)
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
        keep = {self.aqi_view, self.pm_view}

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
                parts.append(f"<span style='color:{TARGET_COLORS[k]}'>━ {self.labels.get(k, k)}</span>")
        if self.mode == "day" and self.live_cb.isChecked():
            parts.append(f"<span style='color:{MUTED}'>○ WAQI live</span>")
        if self.model_cb.isChecked():
            parts.append(f"<span style='color:{MODEL_COLOR}'>┄ Satellite model</span>")
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
            self.method_lbl.setText("Your area is a single monitor, so there's nothing to combine.")
            return
        lst = ", ".join(f"{_label(c['name'])} {float(c.get('distance_km') or 0):.1f} km" for c in comps)
        how = {"median": "the middle value of the monitors that reported each hour",
               "mean": "the plain average of the monitors that reported each hour",
               "idw": "an average weighted by 1/distance², so closer monitors count more"}[self.method]
        self.method_lbl.setText(f"<b>Your area = {METHOD_NAMES[self.method]} of {len(comps)} monitors</b> "
                                f"({how}): {lst}")
        self.method_lbl.setTextFormat(Qt.RichText)

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
        for attr, view in (("aqi_chart", self.aqi_view), ("pm_chart", self.pm_view)):
            old = getattr(self, attr)
            new = self._new_chart()
            view.setChart(new)
            setattr(self, attr, new)
            old.deleteLater()

    def _x_axis(self, lo, hi, fmt, ticks, title, labels=True):
        x = QDateTimeAxis()
        x.setFormat(fmt); x.setTickCount(ticks)
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
        y.setTitleText(unit); y.setTitleBrush(QBrush(QColor(MUTED)))
        y.setLabelsColor(QColor(MUTED)); y.setGridLineColor(QColor(GRID))
        y.setLabelsFont(self._font(8)); y.setTitleFont(self._font(8, True))
        cats = QCategoryAxis()
        cats.setLabelsFont(self._font(8))
        cats.setRange(0, y_max)
        cats.setLabelsPosition(QCategoryAxis.AxisLabelsPositionCenter)
        cats.setLabelsColor(QColor(MUTED)); cats.setGridLineVisible(False)
        cats.setLineVisible(False)
        chart.addAxis(x, Qt.AlignBottom)
        chart.addAxis(y, Qt.AlignLeft)
        chart.addAxis(cats, Qt.AlignRight)

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
            fill = QColor(col); fill.setAlpha(26)
            area.setBrush(QBrush(fill)); area.setPen(QPen(Qt.NoPen))
            chart.addSeries(area); area.attachAxis(x); area.attachAxis(y)
            if top - lo_val >= y_max * 0.05:            # skip labels on slivers
                cats.append(name, top)
            else:
                cats.append(" " * (len(cats.categoriesLabels()) + 1), top)
            lo_val = hi_val
        return y

    def _scale(self, plots, kind, cap):
        """(axis top, clamp level or None). The axis top IS the bound: points above it are drawn on
        the top edge. Normally the axis just fits the data. Only when the view exceeds your normal
        range (cap × 1.2) is it bounded — at the larger of your normal range and the level that
        leaves at most ~5 % of the points above it, so one festival night shows as 1–3 outliers,
        not a row of them."""
        vals = sorted((p.y() for k, s in plots if k == kind and id(s) not in self._no_rank
                       for p in s.points()), reverse=True)
        vmax = vals[0] if vals else 0
        floor = 50 if kind == "pm" else 100
        if not cap or vmax <= cap * (1 + OUTLIER_TOLERANCE):
            return _nice(max(vmax, floor)), None
        allowed = max(1, round(OUTLIER_SHARE * len(vals)))
        must_fit = vals[allowed] if len(vals) > allowed else 0
        top = _nice_tight(max(cap, must_fit))
        while True:                       # don't flag a reading that only just clears the top (753 vs 750)
            near = [v for v in vals if top < v <= top * 1.10]
            if not near:
                break
            top = _nice_tight(max(near))
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
                layer.add_outlier(s, px, ceiling, py, col, main)
        for s, px, py, col in labels:
            prio = 1 if col == TARGET_COLORS.get("home") else 2
            layer.add(s, px, py, f"{py:.0f}", QColor(col).lighter(130).name(), prio)
        layer.layout()

    def _outlier_notes(self, out_a, out_p, ceil_a, ceil_p, fmt, kind):
        rg = self._ranges or {}
        normal = (rg.get("hour_aqi"), rg.get("hour_pm")) if kind == "hourly" else (rg.get("day_aqi"), rg.get("day_pm"))
        out = []
        for name, outs, top, norm in (("AQI", out_a, ceil_a, normal[0]), ("PM2.5", out_p, ceil_p, normal[1])):
            main = sorted({(px, py) for _, px, py, _, m in outs if m})
            if top is None or not main:
                continue
            pts = ", ".join(f"{datetime.fromtimestamp(px / 1000).astimezone():{fmt}} {py:.0f}" for px, py in main)
            out.append(f"<span style='color:#d0d0e0'>▲ {name} above the top of the axis ({top:.0f}), drawn on the top "
                       f"edge with the real value: {pts}. Your normal {kind} range reaches {norm:.0f} "
                       f"({NORMAL_PERCENTILE}th percentile of your data + {NORMAL_ALLOWANCE:.0%}, festival nights "
                       f"excluded); the axis stretches so no more than 1–3 readings a day sit above it.</span>")
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
        self._no_rank = set()                   # series not counted when choosing the axis (indoor overlay)
        self._fresh_charts()
        self._layers = [_LabelLayer(self.aqi_chart, self.ui_scale), _LabelLayer(self.pm_chart, self.ui_scale)]
        purifier = self.mode == "indoor"         # Purifier view: one big chart, outdoor-only controls hidden
        self.pm_view.setVisible(not purifier)
        if not self.phone:
            for w in [*self.checks.values(), self.live_cb, self.model_cb, self.mlab, *self.method_btns.values()]:
                w.setVisible(not purifier)
        now = datetime.now()
        if getattr(self, "_ranges_at", None) is None or (now - self._ranges_at).total_seconds() > 1800:
            try:
                self._ranges = normal_ranges(self.store)
            except Exception:
                self._ranges = {}
            self._ranges_at = now
        {"day": self._draw_day, "month": self._draw_month, "indoor": self._draw_indoor}[self.mode]()

    # ── day view ────────────────────────────────────────────────────────────
    def _draw_day(self):
        start = self.day
        end = (start + timedelta(days=1, hours=12)).replace(hour=0)
        self.period_lbl.setText(start.strftime("%a %d %b %Y"))
        self.mark_lbl.setText("")
        months = {start.strftime("%Y-%m"), (end - timedelta(seconds=1)).strftime("%Y-%m")}
        hourly = [r for m in months for r in self._rows("readings", m)]
        live   = [r for m in months for r in self._rows("live", m)]

        plots, aqi_max, pm_max, notes, src = [], 100, 50, [], {}
        hidden_rings, missing, last_ends = {}, {}, []
        now = datetime.now().astimezone()
        for key, cb in self.checks.items():
            if not cb.isChecked():
                continue
            col, label = TARGET_COLORS[key], self.labels.get(key, key)
            rows = sorted(((parse_dt(r["hour_start_local"]), r) for r in hourly if r["target"] == key),
                          key=lambda tr: tr[0] or start)
            c25, c10, ca = self._cols(key)
            rows = [(t, r) for t, r in rows if t and start <= t < end
                    and (_num(r.get(ca)) is not None or _num(r.get(c25)) is not None)]
            if key == "home" and self._area_info():
                label = f"{label} — {METHOD_NAMES[self.method]}"
            tips = {}
            a_s, p_s, prev = self._line(col, tips), self._line(col, tips, which=1), None
            for t, r in rows:
                if prev is not None and t - prev > timedelta(hours=1):     # gap → break line
                    plots += [("aqi", a_s), ("pm", p_s)]
                    a_s, p_s = self._line(col, tips), self._line(col, tips, which=1)
                xm = _ms(t + timedelta(minutes=30))
                a, p, p10 = _num(r.get(ca)), _num(r.get(c25)), _num(r.get(c10))
                if a is not None:
                    a_s.append(xm, a); aqi_max = max(aqi_max, a)
                if p is not None:
                    p_s.append(xm, p); pm_max = max(pm_max, p)
                head = (f"{label} — {_short(r.get('station'))}<br>"
                        f"{t.strftime('%H:%M')}–{(t + timedelta(hours=1)).strftime('%H:%M')} average<br>")
                aqi_s = f"US AQI {a:.0f} ({aqi_category_name(a)})" if a is not None else "US AQI —"
                pm_s = (f"PM2.5 {p} µg/m³" if p is not None else "PM2.5 —") + \
                       (f" · PM10 {p10:.0f}" if p10 is not None else "")
                used = r.get("monitors_used")
                tail = (f"<br><span style='color:#8a8aa3'>PM2.5 by monitor: {used}</span>" if used else "")
                tips[xm] = (head + f"<b>{aqi_s}</b><br>{pm_s}" + tail, head + f"<b>{pm_s}</b><br>{aqi_s}" + tail)
                prev = t
                src[key] = [r.get("station"), r.get("source_url"), set()]
            plots += [("aqi", a_s), ("pm", p_s)]

            if self.live_cb.isChecked():
                ltips = {}
                ring = self._dots(col, ltips, size=11, ring=True)
                line_stn = _short(rows[-1][1].get("station")).lower() if rows else ""
                for r in live:
                    if r["target"] != key:
                        continue
                    wname = _short(r.get("waqi_station"))
                    both_median = "nearby monitors" in line_stn and "nearby monitors" in wname.lower()
                    if line_stn and line_stn not in wname.lower() and not both_median:   # different monitor
                        hidden_rings.setdefault(label, set()).add(wname)
                        continue
                    t = _local_naive(r.get("station_time_local")) or parse_dt(r.get("polled_at_local"))
                    v = live_row_aqi(r)                  # on the 2024 scale, like the lines
                    if t and v is not None and start <= t < end:
                        ring.append(_ms(t), v); aqi_max = max(aqi_max, v)
                        parts = r.get("waqi_parts")
                        raw = r.get("waqi_aqi_raw") if r.get("aqi_scale") == "us2024" else r.get("waqi_aqi")
                        ltips[_ms(t)] = (f"WAQI live — {_short(r.get('waqi_station'))}<br>"
                                         f"reported {t.strftime('%H:%M')} · <b>US AQI {v:.0f}</b> (2024 scale)"
                                         + (f"<br><span style='color:#8a8aa3'>WAQI shows {float(raw):.0f} on its older "
                                            f"US scale</span>" if _num(raw) is not None else "")
                                         + (f"<br><span style='color:#8a8aa3'>{parts}</span>" if parts else ""))
                        src.setdefault(key, [label, "", set()])[2].add(_short(r.get("waqi_station")))
                plots.append(("aqi", ring))

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

            vals = [(_num(r.get(c25)), _num(r.get(ca)), t) for t, r in rows]
            p25 = [v[0] for v in vals if v[0] is not None]
            aq = [(v[1], v[2]) for v in vals if v[1] is not None]
            if p25 and aq:
                pk, pt = max(aq, key=lambda v: v[0])
                notes.append(f"<span style='color:{col}; font-weight:700'>{label}</span>: "
                             f"{len(p25)}/24 h so far · mean PM2.5 <b>{sum(p25)/len(p25):.0f} µg/m³</b> · "
                             f"worst hour AQI <b>{pk:.0f}</b> ({pt.strftime('%H:%M')}–{(pt + timedelta(hours=1)).strftime('%H:%M')})")
            else:
                notes.append(f"<span style='color:{col}; font-weight:700'>{label}</span>: no data for this day yet")


        if self.model_cb.isChecked():
            notes += self._model_series(plots, start, end, hourly)

        x_lo, x_hi = _ms(start), _ms(end)
        nt = 7 if self.phone else 13                        # every 4 h on a phone, 2 h on the desktop
        xa = self._x_axis(x_lo, x_hi, "HH:mm", nt, None, labels=False)
        xp = self._x_axis(x_lo, x_hi, "HH:mm", nt, "Time of day  ·  each point = that hour's average")
        self._phone_title(start.strftime("%a %d %b"))
        rg = self._ranges or {}
        top_a, ceil_a = self._scale(plots, "aqi", rg.get("hour_aqi"))
        top_p, ceil_p = self._scale(plots, "pm", rg.get("hour_pm"))
        lab_a, out_a = self._prepare(plots, "aqi", ceil_a)
        lab_p, out_p = self._prepare(plots, "pm", ceil_p)
        ya = self._setup_chart(self.aqi_chart, xa, top_a, "US AQI", 0)
        yp = self._setup_chart(self.pm_chart, xp, top_p, "PM2.5  µg/m³", 1)
        bands = sorted({(g[0], g[1]) for gs in missing.values() for g in gs})
        for g0, g1 in bands:                                  # hatched = no OpenAQ data for these hours
            for chart, xx, yy, top in ((self.aqi_chart, xa, ya, ya.max()), (self.pm_chart, xp, yp, yp.max())):
                up, lo = QLineSeries(), QLineSeries()
                up.append(_ms(g0), top); up.append(_ms(g1), top)
                lo.append(_ms(g0), 0); lo.append(_ms(g1), 0)
                band = QAreaSeries(up, lo)
                up.setParent(band); lo.setParent(band)
                band.setBrush(QBrush(QColor(255, 255, 255, 40), Qt.BDiagPattern))
                band.setPen(QPen(Qt.NoPen))
                self._add(chart, band, xx, yy)
        pending = None
        if start <= now < end:                                # today: hours OpenAQ hasn't published yet
            floor2h = (now - timedelta(hours=2)).replace(minute=0, second=0, microsecond=0)
            p0 = max(min(last_ends) if last_ends else floor2h, floor2h)
            if p0 < now:
                pending = (p0, now)
                for chart, xx, yy, top in ((self.aqi_chart, xa, ya, ya.max()), (self.pm_chart, xp, yp, yp.max())):
                    up, lo = QLineSeries(), QLineSeries()
                    up.append(_ms(p0), top); up.append(_ms(now), top)
                    lo.append(_ms(p0), 0); lo.append(_ms(now), 0)
                    band = QAreaSeries(up, lo)
                    up.setParent(band); lo.setParent(band)
                    band.setBrush(QBrush(QColor(122, 162, 247, 45), Qt.Dense6Pattern))
                    band.setPen(QPen(Qt.NoPen))
                    self._add(chart, band, xx, yy)
        for kind, s in plots:
            if s.count():
                if kind == "aqi":
                    self._add(self.aqi_chart, s, xa, ya)
                else:
                    self._add(self.pm_chart, s, xp, yp)
        self._finish_chart(self.aqi_chart, self._layers[0], xa, ya, ceil_a, lab_a, out_a, "AQI")
        self._finish_chart(self.pm_chart, self._layers[1], xp, yp, ceil_p, lab_p, out_p, "µg/m³")
        notes += self._outlier_notes(out_a, out_p, ceil_a, ceil_p, "%H:%M", "hourly")
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
        lag = "<br><span style='color:#8a8aa3'>Lines = OpenAQ hourly averages (arrive ~2 h late). " \
              "Rings = WAQI live readings, converted to the same 2024 US scale as the lines (WAQI itself uses the older table). WAQI carries only some of your monitors, so rings and lines differ when the missing monitors read higher or lower.</span>"
        self.summary.setText("<br>".join(notes) + lag if notes else "Tick at least one location above.")
        self._set_source(src)

    # ── month view ──────────────────────────────────────────────────────────
    def _draw_month(self):
        first = self.day.replace(day=1)
        month = first.strftime("%Y-%m")
        nxt = (first + timedelta(days=32)).replace(day=1)
        ndays = (nxt - first).days
        self.period_lbl.setText(first.strftime("%B %Y"))
        self.mark_lbl.setText("●  full day (≥ 18 h)    ○  incomplete day")
        daily = self._rows("daily", month)

        plots, aqi_max, pm_max, notes, src = [], 100, 50, [], {}
        for key, cb in self.checks.items():
            if not cb.isChecked():
                continue
            col, label = TARGET_COLORS[key], self.labels.get(key, key)
            rows = sorted((r for r in daily if r["target"] == key), key=lambda r: r["date"])
            if key == "home" and self._area_info():
                label = f"{label} — {METHOD_NAMES[self.method]}"
            tips = {}
            a_s, p_s = self._line(col, tips), self._line(col, tips, which=1)
            a_s.setPointsVisible(False); p_s.setPointsVisible(False)
            full_a, part_a = self._dots(col, tips), self._dots(col, tips, hollow=True)
            full_p, part_p = self._dots(col, tips, which=1), self._dots(col, tips, hollow=True, which=1)
            good, prev_d = [], None
            for r in rows:
                d = datetime.fromisoformat(r["date"]).astimezone()
                if prev_d is not None and (d - prev_d) > timedelta(days=1, hours=2):   # missing days → break line
                    plots += [("aqi", a_s), ("pm", p_s)]
                    a_s, p_s = self._line(col, tips), self._line(col, tips, which=1)
                    a_s.setPointsVisible(False); p_s.setPointsVisible(False)
                prev_d = d
                x = _ms(d.replace(hour=12))
                if key == "home" and self.method != "median" and self._area_info():
                    a = _num(r.get(f"aqi_daily_{self.method}_method"))
                    p = _num(r.get(f"pm25_daily_{self.method}_method"))
                else:
                    a, p = _num(r.get("aqi_daily_us")), _num(r.get("pm25_mean_ugm3"))
                ok = r.get("epa_valid_24h") == "yes"
                if a is not None:
                    a_s.append(x, a); (full_a if ok else part_a).append(x, a); aqi_max = max(aqi_max, a)
                if p is not None:
                    p_s.append(x, p); (full_p if ok else part_p).append(x, p); pm_max = max(pm_max, p)
                if ok and a is not None:
                    good.append((a, r["date"], p))
                head = f"{label} — {d.strftime('%a %d %b')}<br>"
                aqi_s = f"Daily US AQI {a:.0f} ({aqi_category_name(a)})" if a is not None else "Daily US AQI —"
                pm_s = f"Mean PM2.5 {p} µg/m³"
                tail = (f"<br>{r.get('pm25_hours')}/24 h of data" + ("" if ok else " (incomplete)")
                        + (f"<br>Worst hour: AQI {r.get('aqi_hourly_max')} at {r.get('aqi_hourly_max_at')}"
                           if r.get("aqi_hourly_max") else ""))
                tips[x] = (head + f"<b>{aqi_s}</b><br>{pm_s}" + tail, head + f"<b>{pm_s}</b><br>{aqi_s}" + tail)
                src[key] = [r.get("station"), r.get("source_url"), set()]
            plots += [("aqi", a_s), ("aqi", full_a), ("aqi", part_a),
                      ("pm", p_s), ("pm", full_p), ("pm", part_p)]
            if good:
                worst, best = max(good), min(good)
                pms = [g[2] for g in good if g[2] is not None]
                notes.append(f"<span style='color:{col}; font-weight:700'>{label}</span>: "
                             f"{len(good)} full day(s) · month mean PM2.5 <b>{sum(pms)/len(pms):.0f} µg/m³</b> · "
                             f"worst day AQI <b>{worst[0]:.0f}</b> ({worst[1][8:]}) · best <b>{best[0]:.0f}</b> ({best[1][8:]})")
            else:
                notes.append(f"<span style='color:{col}; font-weight:700'>{label}</span>: no full days this month yet")

        if self.model_cb.isChecked():
            notes += self._model_series(plots, first, nxt, None, daily_rows=daily)

        x_lo = _ms(first.replace(hour=12))
        x_hi = _ms((first + timedelta(days=ndays - 1, hours=2)).replace(hour=12))
        ticks = ndays                                   # one tick per day, on the data points
        if self.phone:
            ticks = 8
            self._phone_title(first.strftime("%B %Y") + " · daily")
        xa = self._x_axis(x_lo, x_hi, "dd", ticks, None, labels=False)
        xp = self._x_axis(x_lo, x_hi, "dd", ticks, "Day of month  ·  daily AQI = EPA AQI of the 24-hour mean concentration")
        rg = self._ranges or {}
        top_a, ceil_a = self._scale(plots, "aqi", rg.get("day_aqi"))
        top_p, ceil_p = self._scale(plots, "pm", rg.get("day_pm"))
        lab_a, out_a = self._prepare(plots, "aqi", ceil_a)
        lab_p, out_p = self._prepare(plots, "pm", ceil_p)
        ya = self._setup_chart(self.aqi_chart, xa, top_a, "Daily US AQI", 0)
        yp = self._setup_chart(self.pm_chart, xp, top_p, "Mean PM2.5  µg/m³", 1)
        for kind, s in plots:
            if s.count():
                if kind == "aqi":
                    self._add(self.aqi_chart, s, xa, ya)
                else:
                    self._add(self.pm_chart, s, xp, yp)
        self._finish_chart(self.aqi_chart, self._layers[0], xa, ya, ceil_a, lab_a, out_a, "AQI")
        self._finish_chart(self.pm_chart, self._layers[1], xp, yp, ceil_p, lab_p, out_p, "µg/m³")
        notes += self._outlier_notes(out_a, out_p, ceil_a, ceil_p, "%d %b", "daily")
        self.summary.setText("<br>".join(notes) if notes else "Tick at least one location above.")
        self._set_source(src)

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
        ya = self._setup_chart(self.aqi_chart, xa, top, "PM2.5  µg/m³", 1)
        for _, sr in plots:
            if sr.count():
                self._add(self.aqi_chart, sr, xa, ya)
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
        self._finish_chart(self.aqi_chart, layer, xa, ya, ceil, [], outl, "µg/m³")
        self.summary.setText("<br>".join(notes) if notes else
                             "No purifier readings for this day (indoor logging may be paused, or the purifier "
                             "isn't set up under \"indoor\" in config.json).")
        self.source.setText("Indoor: purifier's own sensor via local Wi-Fi (data/indoor_YYYY-MM.csv) · Outdoor: "
                            "OpenAQ hourly median of your area's monitors (arrives 1–2 h late).")

    def _model_series(self, plots, start, end, hourly, daily_rows=None):
        """Dashed satellite-model PM2.5 line (hourly in Day view, daily mean in Month view) and a
        one-line comparison with your area's measured PM2.5 over the same hours/days."""
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
        tips = {}
        line = QLineSeries()
        pen = QPen(QColor(MODEL_COLOR)); pen.setWidthF(2.2 * self.pen_scale); pen.setStyle(Qt.DashLine)
        line.setPen(pen)
        line.hovered.connect(lambda p, on: on and self._tip(tips, p, 1))
        self._no_rank.add(id(line))
        if daily_rows is None:                                   # Day view: hourly
            for t, v, aod in sorted(rows):
                x = _ms(t)
                line.append(x, v)
                tips[x] = (f"Satellite model (CAMS) {t:%H:%M}<br><b>PM2.5 {v:.0f} µg/m³</b>"
                           + (f" · aerosol optical depth {float(aod):.2f}" if _num(aod) is not None else "")
                           + "<br><span style='color:#8a8aa3'>~45 km grid, independent of ground monitors</span>",) * 2
            c25 = self._cols("home")[0]
            meas = {parse_dt(r["hour_start_local"]).replace(minute=0, second=0, microsecond=0): _num(r.get(c25))
                    for r in hourly if r["target"] == "home" and _num(r.get(c25)) is not None
                    and parse_dt(r["hour_start_local"])}
            pairs = [(meas[t.replace(minute=0, second=0, microsecond=0)], v) for t, v, _ in rows
                     if t.replace(minute=0, second=0, microsecond=0) in meas]
            unit = "hours"
        else:                                                    # Month view: daily means
            by = {}
            for t, v, _ in rows:
                by.setdefault(t.date(), []).append(v)
            for d, vs in sorted(by.items()):
                x = _ms(datetime.combine(d, datetime.min.time()).astimezone().replace(hour=12))
                m = statistics.mean(vs)
                line.append(x, m)
                tips[x] = (f"Satellite model (CAMS) {d:%a %d %b}<br><b>daily mean PM2.5 {m:.0f} µg/m³</b>"
                           f"<br><span style='color:#8a8aa3'>~45 km grid, independent of ground monitors</span>",) * 2
            meas = {r["date"]: _num(r.get("pm25_mean_ugm3")) for r in daily_rows
                    if r["target"] == "home" and _num(r.get("pm25_mean_ugm3")) is not None}
            pairs = [(meas[d.isoformat()], statistics.mean(vs)) for d, vs in by.items() if d.isoformat() in meas]
            unit = "days"
        plots.append(("pm", line))
        mv = statistics.mean(v for _, v, _ in rows)
        note = f"<span style='color:{MODEL_COLOR}'>┄ Satellite model (CAMS, ~45 km): mean PM2.5 {mv:.0f} µg/m³"
        if pairs:
            g = statistics.mean(a for a, _ in pairs)
            mm = statistics.mean(b for _, b in pairs)
            note += (f" · over the same {len(pairs)} {unit} your area measured {g:.0f} → monitors read "
                     f"{g / mm:.1f}× the model" if mm else "")
        return [note + ". Use it for the trend: if the monitors fall while the model rises, question the "
                "monitors.</span>"]

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
