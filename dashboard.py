"""
AQI Dashboard — PySide6 popup window.

Features:
  - Multi-source fallback chain (WAQI → IQAir → OpenAQ → OWM)
  - Consensus AQI (median of all responding sources)
  - Stale-cache warning when all live sources fail
  - Locality search: type any area name → geocode → nearest WAQI station
  - Background recorder (system tray): hourly AQI / PM2.5 log to ./data/*.csv
    with a History window (day + month charts) — see recorder.py
"""
import json
import os
import sys
from datetime import datetime, timedelta

from PySide6.QtCore import Qt, QThread, Signal, QTimer, QObject
from PySide6.QtGui import QColor, QFont, QIcon, QPixmap, QPainter, QAction
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QLabel,
    QVBoxLayout, QHBoxLayout, QFrame, QPushButton,
    QTableWidget, QTableWidgetItem, QHeaderView,
    QSizePolicy, QLineEdit, QScrollArea, QSystemTrayIcon, QMenu, QStackedWidget,
)

from aggregator import AQIAggregator
from geolocation import get_location
from waqi import WAQIClient

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")


# ─────────────────────────── AQI helpers ────────────────────────────────────

def aqi_color(aqi):
    if aqi is None:
        return "#6b6b80"
    try:
        a = int(aqi)
    except (ValueError, TypeError):
        return "#6b6b80"
    if a <= 50:   return "#3ecf8e"
    if a <= 100:  return "#f7d154"
    if a <= 150:  return "#f59e42"
    if a <= 200:  return "#ef4444"
    if a <= 300:  return "#a855f7"
    return "#e11d48"          # hazardous: bright crimson (the old dark maroon was unreadable on the dark cards)


def aqi_category(aqi):
    if aqi is None:
        return "—"
    try:
        a = int(aqi)
    except (ValueError, TypeError):
        return "—"
    if a <= 50:   return "Good"
    if a <= 100:  return "Moderate"
    if a <= 150:  return "Unhealthy for Sensitive Groups"
    if a <= 200:  return "Unhealthy"
    if a <= 300:  return "Very Unhealthy"
    return "Hazardous"


def aqi_advisory(aqi):
    if aqi is None:
        return "No data available"
    try:
        a = int(aqi)
    except (ValueError, TypeError):
        return "No data available"
    if a <= 50:   return "Air quality is satisfactory."
    if a <= 100:  return "Acceptable; sensitive individuals should monitor."
    if a <= 150:  return "Sensitive groups should reduce outdoor exertion."
    if a <= 200:  return "Everyone should reduce prolonged outdoor exertion."
    if a <= 300:  return "Health alert — significant effects for everyone."
    return "Hazardous — avoid outdoor activity. Use N95 outside."


# ─────────────────────────── Background workers ──────────────────────────────

class FetchWorker(QThread):
    done  = Signal(dict)
    error = Signal(str)

    def __init__(self, aggregator, lat, lng, city, radius, hot_radius, area_km=5.0):
        super().__init__()
        self.area_km   = area_km
        self.agg       = aggregator
        self.lat       = lat
        self.lng       = lng
        self.city      = city
        self.radius    = radius
        self.hot_radius = hot_radius

    def run(self):
        try:
            data = self.agg.fetch_all(self.lat, self.lng, self.city, self.radius, self.hot_radius)
            try:
                from area import area_reading
                data["area"] = area_reading(self.agg.waqi, self.lat, self.lng, self.area_km)
            except Exception as e:
                data.setdefault("errors", []).append(f"area: {e}")
            self.done.emit(data)
        except Exception as e:
            self.error.emit(str(e))


class GeoSearchWorker(QThread):
    """Geocode a locality query → lat/lng → nearest WAQI station AQI."""
    done  = Signal(dict)
    error = Signal(str)

    def __init__(self, waqi_client, query, area_km=5.0):
        super().__init__()
        self.client = waqi_client
        self.query  = query
        self.area_km = area_km

    def run(self):
        try:
            from geocoder import geocode
            geo = geocode(self.query)
            if not geo:
                self.error.emit(
                    f"Could not find '{self.query}' on the map. "
                    "Try adding the city name, e.g. 'Karol Bagh Delhi'."
                )
                return

            from area import area_reading, describe
            area = area_reading(self.client, geo["lat"], geo["lng"], self.area_km)
            if not area:
                self.error.emit("Location found but no AQI monitors within 8 km.")
                return
            data = {"aqi": area["median"], "station": f"Median of {area['n']} monitors within "
                    f"{area['radius_km']:g} km", "_area": area, "_area_note": describe(area),
                    "time": max((m["time"] or "" for m in area["monitors"]), default="")}

            data["_queried"]     = self.query
            data["_resolved_to"] = geo["display_name"]
            data["_geo_lat"]     = geo["lat"]
            data["_geo_lng"]     = geo["lng"]
            data["_geo_source"]  = geo.get("source", "")
            self.done.emit(data)

        except Exception as e:
            self.error.emit(str(e))


class RecorderWorker(QThread):
    done = Signal(dict)

    def __init__(self, recorder, after=None):
        super().__init__()
        self.recorder = recorder
        self.after = after                     # runs in this thread after the sync (alerts, city snapshot)

    def run(self):
        try:
            st = self.recorder.poll()
        except Exception as e:
            st = {"ok": False, "added": 0, "updated": 0, "errors": [str(e)], "targets": {}}
        if self.after:
            try:
                st["alerts"] = self.after()
            except Exception as e:
                import logging
                logging.getLogger("aqi").warning("after-sync step failed: %s", e)
        self.done.emit(st)


class FuncWorker(QThread):
    """Runs fn() off the UI thread; emits its result (or {"error": ...})."""
    done = Signal(object)

    def __init__(self, fn):
        super().__init__()
        self.fn = fn

    def run(self):
        try:
            self.done.emit(self.fn())
        except Exception as e:
            self.done.emit({"error": str(e)})


class PhoneListener(QObject):
    """Streams your ntfy topic on a daemon thread (it ends with the app, never blocks Quit); every
    message you type in the ntfy app is emitted as a command on the UI thread."""
    got = Signal(dict)
    status = Signal(str)

    def __init__(self, server, topic, since):
        super().__init__()
        self.server, self.topic, self.since = server, topic, since
        self._stop = False
        self._thread = None

    def start(self):
        import threading
        from phone import listen
        self._thread = threading.Thread(
            target=listen, daemon=True,
            args=(self.server, self.topic, self.since, self.got.emit, lambda: self._stop, self.status.emit))
        self._thread.start()

    def stop(self):
        self._stop = True


class ExplainWorker(QThread):
    done = Signal(dict)

    def __init__(self, config):
        super().__init__()
        self.config = config

    def run(self):
        try:
            import surge
            self.done.emit(surge.explain(self.config, trigger="asked from tray"))
        except Exception as e:
            self.done.emit({"summary": f"Couldn't check: {e}", "reasons": [], "unknown": []})


class IndoorWorker(QThread):
    done = Signal(list)

    def __init__(self, logger, outdoor):
        super().__init__()
        self.logger, self.outdoor = logger, outdoor

    def run(self):
        try:
            self.done.emit(self.logger.poll(self.outdoor))
        except Exception as e:
            self.done.emit([{"device": d.name, "status": f"error: {e}"} for d in self.logger.devices])


def environment_info():
    """Which Python is running the app: project venv, conda env, or global install, and where."""
    import platform
    app_dir = os.path.dirname(os.path.abspath(__file__))
    prefix, base = sys.prefix, getattr(sys, "base_prefix", sys.prefix)
    if os.path.normcase(prefix) != os.path.normcase(base):
        inside = os.path.normcase(prefix).startswith(os.path.normcase(app_dir))
        kind = f"local virtual environment ({os.path.basename(prefix)}" + (", in this app folder)" if inside else ")")
    elif os.path.isdir(os.path.join(prefix, "conda-meta")):
        kind = f"conda environment ({os.environ.get('CONDA_DEFAULT_ENV') or os.path.basename(prefix)})"
    else:
        kind = "GLOBAL Python install"
    try:
        import PySide6
        qt = f" · PySide6 {PySide6.__version__}"
    except Exception:
        qt = ""
    return f"Environment: {kind} · Python {platform.python_version()}{qt} · {sys.executable}"


def tray_icon(aqi=None):
    """Rounded square in the AQI colour with the number on it."""
    pm = QPixmap(64, 64)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(aqi_color(aqi) if aqi is not None else "#3a3a4e"))
    p.drawRoundedRect(2, 2, 60, 60, 14, 14)
    txt = str(aqi) if aqi is not None else "AQ"
    f = QFont("Segoe UI", 26 if len(txt) <= 2 else 20)
    f.setBold(True)
    p.setFont(f)
    try:
        dark_text = aqi is not None and int(aqi) <= 150
    except (TypeError, ValueError):
        dark_text = False
    p.setPen(QColor("#0e0e16" if dark_text else "#ffffff"))
    p.drawText(pm.rect(), Qt.AlignCenter, txt)
    p.end()
    return QIcon(pm)


# ─────────────────────────── AQI Card ────────────────────────────────────────

class AQICard(QFrame):
    def __init__(self, title, parent=None):
        super().__init__(parent)
        self.setFrameShape(QFrame.NoFrame)
        self.setStyleSheet(
            "QFrame { background:#1c1c28; border-radius:14px; border:1px solid #2a2a3e; }"
        )
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.setMinimumHeight(220)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(22, 20, 22, 20)
        lay.setSpacing(6)

        self.title_lbl = QLabel(title)
        self.title_lbl.setStyleSheet(
            "color:#8a8aa3; font-size:11px; font-weight:700; letter-spacing:1.5px; border:none;"
        )
        self.aqi_lbl = QLabel("—")
        self.aqi_lbl.setStyleSheet("color:#ffffff; font-size:64px; font-weight:800; border:none;")
        self.aqi_lbl.setAlignment(Qt.AlignCenter)

        self.cat_lbl = QLabel("Loading…")
        self.cat_lbl.setStyleSheet("color:#cccccc; font-size:13px; font-weight:600; border:none;")
        self.cat_lbl.setAlignment(Qt.AlignCenter)
        self.cat_lbl.setWordWrap(True)

        self.pm_lbl = QLabel("")                       # PM2.5, smaller than the AQI
        self.pm_lbl.setStyleSheet("color:#d8d8e8; font-size:17px; font-weight:700; border:none;")
        self.pm_lbl.setAlignment(Qt.AlignCenter)
        self.pm_note = QLabel("")
        self.pm_note.setStyleSheet("color:#6b6b80; font-size:10px; border:none;")
        self.pm_note.setAlignment(Qt.AlignCenter)
        self.meas_lbl = QLabel("")                     # measured hour (Your area), separate from live
        self.meas_lbl.setStyleSheet("color:#c8c8d8; font-size:12px; border:none;")
        self.meas_lbl.setAlignment(Qt.AlignCenter)
        self.meas_lbl.setWordWrap(True)
        self.meas_lbl.hide()

        self.station_lbl = QLabel("")
        self.station_lbl.setStyleSheet("color:#8a8aa3; font-size:11px; border:none;")
        self.station_lbl.setAlignment(Qt.AlignCenter)
        self.station_lbl.setWordWrap(True)

        self.advisory_lbl = QLabel("")
        self.advisory_lbl.setStyleSheet(
            "color:#b0b0c4; font-size:11px; font-style:italic; border:none;"
        )
        self.advisory_lbl.setAlignment(Qt.AlignCenter)
        self.advisory_lbl.setWordWrap(True)

        lay.addWidget(self.title_lbl)
        lay.addStretch(1)
        lay.addWidget(self.aqi_lbl)
        lay.addWidget(self.cat_lbl)
        lay.addSpacing(4)
        lay.addWidget(self.pm_lbl)
        lay.addWidget(self.pm_note)
        lay.addSpacing(4)
        lay.addWidget(self.meas_lbl)
        lay.addStretch(1)
        lay.addWidget(self.station_lbl)
        lay.addWidget(self.advisory_lbl)
        self.info_btn = QPushButton("ⓘ  How is this calculated?")
        self.info_btn.setCursor(Qt.PointingHandCursor)
        self.info_btn.setStyleSheet("QPushButton { background:transparent; color:#7aa2f7; border:none; "
                                    "font-size:11px; text-decoration:underline; } QPushButton:hover { color:#a5c0ff; }")
        self.info_btn.hide()
        lay.addWidget(self.info_btn, 0, Qt.AlignCenter)

    def set_pm(self, card):
        """card: {"pm25", "approx", "note"} from cards.card_values()."""
        from cards import pm_text
        from aqi_math import category as _cat
        if card and card.get("live") and card.get("aqi") is not None:      # "(live 23:00)" next to the category
            self.cat_lbl.setText(f"{_cat(card['aqi'])}  <span style='color:#8a8aa3; font-weight:500'>"
                                 f"(live {card['live']})</span>")
            self.cat_lbl.setTextFormat(Qt.RichText)
        m = (card or {}).get("measured")
        if m:
            col = aqi_color(m["aqi"])
            self.meas_lbl.setText(f"<span style='color:#8a8aa3'>Measured {m['span']}:</span> "
                                  f"<b>PM2.5 {m['pm25']:.0f} µg/m³</b> → <b style='color:{col}'>AQI {m['aqi']}</b>"
                                  f"<br><span style='color:#6b6b80; font-size:10px'>OpenAQ hourly, government "
                                  f"monitors · published 1–2 h after the hour</span>")
            self.meas_lbl.setTextFormat(Qt.RichText)
            self.meas_lbl.show()
        else:
            self.meas_lbl.hide()
        if not card or card.get("pm25") is None:
            self.pm_lbl.setText("PM2.5 —")
            self.pm_note.setText(card.get("note", "") if card else "")
            return
        self.pm_lbl.setText(f"{pm_text(card, unit=False)} <span style='font-size:12px; font-weight:600;"
                            f" color:#8a8aa3'>µg/m³</span>")
        self.pm_lbl.setTextFormat(Qt.RichText)
        self.pm_note.setText(card.get("note", ""))

    def update_data(self, data, station_override=None):
        if not data:
            self.aqi_lbl.setText("—")
            self.aqi_lbl.setStyleSheet("color:#6b6b80; font-size:64px; font-weight:800; border:none;")
            self.cat_lbl.setText("Unavailable")
            self.cat_lbl.setStyleSheet("color:#6b6b80; font-size:13px; font-weight:600; border:none;")
            self.station_lbl.setText("")
            self.advisory_lbl.setText("")
            return

        aqi   = data.get("aqi")
        color = aqi_color(aqi)
        self.aqi_lbl.setText(str(aqi) if aqi is not None else "—")
        self.aqi_lbl.setStyleSheet(f"color:{color}; font-size:64px; font-weight:800; border:none;")
        self.cat_lbl.setText(aqi_category(aqi))
        self.cat_lbl.setStyleSheet(f"color:{color}; font-size:13px; font-weight:600; border:none;")

        station = station_override or data.get("station") or ""
        dom     = data.get("dominant")
        self.station_lbl.setText(
            f"{station}  •  Dominant: {str(dom).upper()}" if dom else station
        )
        self.advisory_lbl.setText(aqi_advisory(aqi))


# ─────────────────────────── Search Result Card ───────────────────────────────

class SearchResultCard(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFrameShape(QFrame.NoFrame)
        self.setStyleSheet(
            "QFrame { background:#1c1c28; border-radius:14px; border:1px solid #2a2a3e; }"
        )
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setMinimumHeight(130)
        self.hide()

        lay = QHBoxLayout(self)
        lay.setContentsMargins(28, 18, 28, 18)
        lay.setSpacing(28)

        self.aqi_lbl = QLabel("—")
        self.aqi_lbl.setStyleSheet("color:#ffffff; font-size:56px; font-weight:800; border:none;")
        self.aqi_lbl.setFixedWidth(120)
        self.aqi_lbl.setAlignment(Qt.AlignCenter)
        lay.addWidget(self.aqi_lbl)

        right = QVBoxLayout()
        right.setSpacing(4)

        self.name_lbl = QLabel("")
        self.name_lbl.setStyleSheet("color:#f5f5fa; font-size:15px; font-weight:700; border:none;")
        self.name_lbl.setWordWrap(True)

        self.cat_lbl = QLabel("")
        self.cat_lbl.setStyleSheet("color:#cccccc; font-size:12px; font-weight:600; border:none;")

        self.meta_lbl = QLabel("")
        self.meta_lbl.setStyleSheet("color:#8a8aa3; font-size:11px; border:none;")
        self.meta_lbl.setWordWrap(True)

        self.advisory_lbl = QLabel("")
        self.advisory_lbl.setStyleSheet(
            "color:#b0b0c4; font-size:11px; font-style:italic; border:none;"
        )
        self.advisory_lbl.setWordWrap(True)

        right.addWidget(self.name_lbl)
        right.addWidget(self.cat_lbl)
        right.addWidget(self.meta_lbl)
        right.addWidget(self.advisory_lbl)
        right.addStretch()
        lay.addLayout(right)

    def update_data(self, data):
        if not data:
            self.hide()
            return

        aqi   = data.get("aqi")
        color = aqi_color(aqi)

        self.aqi_lbl.setText(str(aqi) if aqi is not None else "—")
        self.aqi_lbl.setStyleSheet(f"color:{color}; font-size:56px; font-weight:800; border:none;")

        resolved = data.get("_resolved_to") or data.get("station") or "Unknown"
        nearest  = data.get("station") or ""
        self.name_lbl.setText(resolved)

        self.cat_lbl.setText(aqi_category(aqi))
        self.cat_lbl.setStyleSheet(f"color:{color}; font-size:12px; font-weight:600; border:none;")

        meta_parts = []
        if data.get("_area_note"):
            meta_parts.append(data["_area_note"])
        elif nearest:
            meta_parts.append(f"Nearest station: {nearest}")
        dom = data.get("dominant")
        if dom:
            meta_parts.append(f"Dominant: {str(dom).upper()}")
        time_str = data.get("time")
        if time_str:
            meta_parts.append(f"Updated: {time_str}")
        self.meta_lbl.setText("  •  ".join(meta_parts))
        self.advisory_lbl.setText(aqi_advisory(aqi))
        self.show()


# ─────────────────────────── Main Window ────────────────────────────────────

class DashboardWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("AQI Dashboard")
        self.setMinimumSize(1120, 820)
        self.setStyleSheet("QMainWindow { background:#0e0e16; }")
        self._waqi_client = None
        self._retry_count = 0
        self._cache_retries = 0
        self._last_refresh = None
        self._outdoor_ctx = {}                  # latest measured hour for your area (from the recorder)
        self._cards = None
        self._last_fetch_data = None
        self._tray = None
        self._quitting = False
        self._tray_hint_shown = False
        self._recorder = None
        self._rec_worker = None
        self._rec_fail_streak = 0
        self._history = None
        self._snap = None
        self._last_snap = None
        self.worker = None
        self.setWindowIcon(tray_icon())

        self.config = self._load_config()
        if not self.config:
            self._show_msg("Setup needed",
                "Copy <code>config.example.json</code> to <code>config.json</code> "
                "and fill in your WAQI token.<br><br>"
                "Free token: <a style='color:#7aa2f7' href='https://aqicn.org/data-platform/token/'>"
                "aqicn.org/data-platform/token</a>")
            return

        try:
            self._validate_config()
        except ValueError as e:
            self._show_msg("Configuration error", f"<span style='color:#f87171'>{e}</span>")
            return

        self._waqi_client = WAQIClient(self.config["waqi_token"])
        self._build_ui()
        self._setup_tray()
        self._setup_recorder()
        self._setup_indoor()
        self._setup_phone()
        QTimer.singleShot(150, self._refresh)

    # ── Config ───────────────────────────────────────────────────────────────

    def _load_config(self):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            return None
        except json.JSONDecodeError as e:
            print(f"config.json parse error: {e}", file=sys.stderr)
            return None

    def _validate_config(self):
        token = self.config.get("waqi_token", "")
        if not token or token == "YOUR_WAQI_TOKEN_HERE":
            raise ValueError(
                "WAQI token missing. Get one free at "
                "https://aqicn.org/data-platform/token/"
            )

    def _show_msg(self, heading, body):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(40, 40, 40, 40)
        lbl = QLabel(f"<h2 style='color:#fff'>{heading}</h2><p style='color:#ccc'>{body}</p>")
        lbl.setOpenExternalLinks(True)
        lbl.setWordWrap(True)
        lay.addWidget(lbl)
        self.setCentralWidget(w)

    # ── UI Build ─────────────────────────────────────────────────────────────

    def _build_ui(self):
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setStyleSheet(
            "QScrollArea { background:#0e0e16; border:none; }"
            "QScrollBar:vertical { background:#1c1c28; width:8px; border-radius:4px; }"
            "QScrollBar::handle:vertical { background:#3a3a4e; border-radius:4px; }"
        )

        central = QWidget()
        outer   = QVBoxLayout(central)
        outer.setContentsMargins(28, 24, 28, 18)
        outer.setSpacing(16)

        # Header
        hdr = QHBoxLayout()
        title_box = QVBoxLayout()
        title_box.setSpacing(2)
        title = QLabel("Air Quality Dashboard")
        title.setStyleSheet("color:#f5f5fa; font-size:24px; font-weight:700;")
        self.subtitle = QLabel("Loading location…")
        self.subtitle.setStyleSheet("color:#8a8aa3; font-size:12px;")
        title_box.addWidget(title)
        title_box.addWidget(self.subtitle)
        hdr.addLayout(title_box)
        hdr.addStretch()

        self.refresh_btn = QPushButton("Refresh")
        self.refresh_btn.setCursor(Qt.PointingHandCursor)
        self.refresh_btn.setStyleSheet(
            "QPushButton { background:#7aa2f7; color:#0e0e16; border:none;"
            "  padding:9px 22px; border-radius:8px; font-weight:700; font-size:13px; }"
            "QPushButton:hover { background:#93b5ff; }"
            "QPushButton:disabled { background:#3a3a4e; color:#6b6b80; }"
        )
        self.refresh_btn.clicked.connect(self._refresh)
        self.history_btn = QPushButton("History  ▸")
        self.history_btn.setCursor(Qt.PointingHandCursor)
        self.history_btn.setStyleSheet(
            "QPushButton { background:#2a2a3e; color:#d8d8e8; border:1px solid #3a3a52;"
            "  padding:9px 22px; border-radius:8px; font-weight:700; font-size:13px; }"
            "QPushButton:hover { background:#3a3a52; }"
        )
        self.history_btn.clicked.connect(self.show_history)
        hdr.addWidget(self.history_btn)
        hdr.addSpacing(8)
        hdr.addWidget(self.refresh_btn)
        outer.addLayout(hdr)

        # Three top cards
        cards = QHBoxLayout()
        cards.setSpacing(14)
        self.locality_card  = AQICard("YOUR LOCALITY")
        self.city_card      = AQICard("CITY OVERVIEW")
        self.consensus_card = AQICard("CONSENSUS (ALL SOURCES)")
        cards.addWidget(self.locality_card)
        cards.addWidget(self.city_card)
        cards.addWidget(self.consensus_card)
        outer.addLayout(cards)

        # Indoor panel (only shown when purifiers are configured)
        self.indoor_box = QFrame()
        self.indoor_box.setStyleSheet("QFrame { background:#1c1c28; border-radius:12px; border:1px solid #2a2a3e; }")
        self.indoor_lay = QVBoxLayout(self.indoor_box)
        self.indoor_lay.setContentsMargins(18, 12, 18, 12)
        ih = QLabel("INDOOR (purifiers)")
        ih.setStyleSheet("color:#8a8aa3; font-size:11px; font-weight:700; letter-spacing:1.5px; border:none;")
        ihead = QHBoxLayout()
        ihead.addWidget(ih)
        ihead.addStretch(1)
        self.indoor_toggle = QPushButton("Indoor logging: ON")
        self.indoor_toggle.setCheckable(True)
        self.indoor_toggle.setCursor(Qt.PointingHandCursor)
        self.indoor_toggle.setToolTip("Pause when you're away from home or don't want this data. "
                                      "While paused the app doesn't contact the purifiers and writes no rows.")
        self.indoor_toggle.setStyleSheet(
            "QPushButton { background:#2a2a3e; color:#b0b0c4; border:1px solid #3a3a52; border-radius:11px;"
            " padding:3px 12px; font-size:11px; font-weight:700; }"
            "QPushButton:checked { background:#1f4d3a; color:#3ecf8e; border-color:#3ecf8e; }")
        self.indoor_graph_btn = QPushButton("Purifier graph  ▸")
        self.indoor_graph_btn.setCursor(Qt.PointingHandCursor)
        self.indoor_graph_btn.setToolTip("Live graph of your purifier's indoor PM2.5 against your area's outdoor PM2.5 "
                                         "(refreshes every minute)")
        self.indoor_graph_btn.setStyleSheet(
            "QPushButton { background:#3ecf8e; color:#0e0e16; border:none; border-radius:11px;"
            " padding:4px 16px; font-size:12px; font-weight:800; } QPushButton:hover { background:#5ee0a6; }")
        self.indoor_graph_btn.clicked.connect(self._open_purifier_graph)
        ihead.addWidget(self.indoor_graph_btn)
        ihead.addSpacing(8)
        ihead.addWidget(self.indoor_toggle)
        self.indoor_lay.addLayout(ihead)
        self.indoor_rows = {}
        self.indoor_box.hide()
        outer.addWidget(self.indoor_box)

        # Search bar
        outer.addWidget(self._section_label("Search any locality / city"))
        search_row = QHBoxLayout()
        search_row.setSpacing(10)
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText(
            "e.g.  Karol Bagh Delhi  /  Bandra Mumbai  /  Koramangala Bengaluru"
        )
        self.search_input.setStyleSheet(
            "QLineEdit { background:#1c1c28; color:#f0f0f8; border:1px solid #2a2a3e;"
            "  border-radius:8px; padding:9px 14px; font-size:13px; }"
            "QLineEdit:focus { border:1px solid #7aa2f7; }"
        )
        self.search_input.returnPressed.connect(self._do_search)

        self.search_btn = QPushButton("Search")
        self.search_btn.setCursor(Qt.PointingHandCursor)
        self.search_btn.setFixedWidth(90)
        self.search_btn.setStyleSheet(
            "QPushButton { background:#2a2a3e; color:#d8d8e8; border:1px solid #3a3a52;"
            "  padding:9px 14px; border-radius:8px; font-weight:700; font-size:13px; }"
            "QPushButton:hover { background:#3a3a52; }"
            "QPushButton:disabled { color:#6b6b80; }"
        )
        self.search_btn.clicked.connect(self._do_search)
        search_row.addWidget(self.search_input)
        search_row.addWidget(self.search_btn)
        outer.addLayout(search_row)

        self.search_status = QLabel("")
        self.search_status.setStyleSheet("color:#6b6b80; font-size:11px;")
        outer.addWidget(self.search_status)

        self.search_result_card = SearchResultCard()
        outer.addWidget(self.search_result_card)

        # Nearby table
        radius = self.config.get("radius_km", 10)
        outer.addWidget(self._section_label(f"Nearby stations  ·  within {radius} km"))
        self.nearby_table = self._make_table(["Station", "AQI", "Category", "Distance (km)"])
        self.nearby_table.setMaximumHeight(220)
        outer.addWidget(self.nearby_table)

        # Hotspots table
        hr = self.config.get("hotspot_radius_km", 30)
        outer.addWidget(self._section_label(f"Hotspots  ·  top-5 worst stations within {hr} km"))
        self.hotspots_table = self._make_table(["Station", "AQI", "Category", "Distance (km)"])
        self.hotspots_table.setMaximumHeight(200)
        outer.addWidget(self.hotspots_table)

        # Status bar
        self.status_lbl = QLabel("Initializing…")
        self.status_lbl.setStyleSheet("color:#6b6b80; font-size:11px;")
        self.status_lbl.setWordWrap(True)
        outer.addWidget(self.status_lbl)

        self.rec_lbl = QLabel("Recorder: starting…")
        self.rec_lbl.setStyleSheet("color:#6b6b80; font-size:11px;")
        self.rec_lbl.setWordWrap(True)
        outer.addWidget(self.rec_lbl)

        self.env_lbl = QLabel(environment_info())
        self.env_lbl.setStyleSheet("color:#55556a; font-size:10px;")
        self.env_lbl.setWordWrap(True)
        self.env_lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
        outer.addWidget(self.env_lbl)

        scroll.setWidget(central)
        self._stack = QStackedWidget()
        self._stack.addWidget(scroll)                 # page 0: dashboard; page 1: history (built on demand)
        # alert banner above both pages (hidden until a spike alert arrives)
        self.alert_bar = QFrame()
        self.alert_bar.setStyleSheet("QFrame { background:#3a1d24; border:1px solid #ef4444; border-radius:8px; }")
        bl = QHBoxLayout(self.alert_bar)
        bl.setContentsMargins(14, 8, 8, 8)
        self.alert_lbl = QLabel("")
        self.alert_lbl.setWordWrap(True)
        self.alert_lbl.setTextFormat(Qt.RichText)
        self.alert_lbl.setStyleSheet("color:#fecaca; font-size:13px; border:none;")
        close_btn = QPushButton("✕")
        close_btn.setFixedSize(28, 28)
        close_btn.setCursor(Qt.PointingHandCursor)
        close_btn.setStyleSheet("QPushButton { background:transparent; color:#fecaca; border:none; font-size:15px; }"
                                "QPushButton:hover { color:#ffffff; }")
        close_btn.clicked.connect(self.alert_bar.hide)
        bl.addWidget(self.alert_lbl, 1)
        bl.addWidget(close_btn, 0, Qt.AlignTop)
        self.alert_bar.hide()
        shell = QWidget()
        sl = QVBoxLayout(shell)
        sl.setContentsMargins(12, 8, 12, 0)
        sl.setSpacing(6)
        sl.addWidget(self.alert_bar)
        sl.addWidget(self._stack, 1)
        self.setCentralWidget(shell)

    def _section_label(self, text):
        lbl = QLabel(text)
        lbl.setStyleSheet("color:#d8d8e8; font-size:14px; font-weight:700; margin-top:6px;")
        return lbl

    def _make_table(self, headers):
        t = QTableWidget(0, len(headers))
        t.setHorizontalHeaderLabels(headers)
        t.setStyleSheet(
            "QTableWidget { background:#1c1c28; color:#d8d8e8; gridline-color:#262636;"
            "  border:1px solid #2a2a3e; border-radius:10px; font-size:12px; }"
            "QHeaderView::section { background:#232333; color:#8a8aa3; padding:8px 10px;"
            "  border:none; border-bottom:1px solid #2a2a3e; font-weight:700; font-size:11px; }"
            "QTableWidget::item { padding:8px 6px; }"
            "QTableWidget::item:selected { background:#2a2a4e; color:#fff; }"
        )
        t.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        for i in range(1, len(headers)):
            t.horizontalHeader().setSectionResizeMode(i, QHeaderView.ResizeToContents)
        t.verticalHeader().setVisible(False)
        t.setEditTriggers(QTableWidget.NoEditTriggers)
        t.setSelectionBehavior(QTableWidget.SelectRows)
        t.setShowGrid(False)
        return t

    # ── Main refresh ─────────────────────────────────────────────────────────

    def _refresh(self):
        if self.worker is not None and self.worker.isRunning():
            return
        self.refresh_btn.setEnabled(False)
        self.status_lbl.setStyleSheet("color:#6b6b80; font-size:11px;")
        self.status_lbl.setText("Fetching…")

        loc_cfg = self.config.get("location") or {}
        lat = lng = None
        city = ""

        if loc_cfg.get("auto"):
            ip_loc = get_location()
            if ip_loc and ip_loc.get("lat") is not None:
                lat, lng = ip_loc["lat"], ip_loc["lng"]
                city = ip_loc.get("city") or loc_cfg.get("city", "")
                self.subtitle.setText(
                    f"{ip_loc.get('city','?')}, {ip_loc.get('region','?')}  ·  "
                    f"auto-detected  ·  {lat:.4f}, {lng:.4f}"
                )
            else:
                lat, lng = loc_cfg.get("lat"), loc_cfg.get("lng")
                city = loc_cfg.get("city", "")
                self.subtitle.setText(f"{city}  ·  IP geolocation failed, using config fallback")
        else:
            lat, lng = loc_cfg.get("lat"), loc_cfg.get("lng")
            city = loc_cfg.get("city", "")
            if lat is not None and lng is not None:
                self.subtitle.setText(f"{city}  ·  {lat:.4f}, {lng:.4f}")

        if lat is None or lng is None:
            self.status_lbl.setText("⚠  No location. Set location.lat/lng in config.json.")
            self.refresh_btn.setEnabled(True)
            return

        try:
            agg = AQIAggregator(
                waqi_token = self.config["waqi_token"],
                owm_key    = self.config.get("openweather_key") or None,
                iqair_key  = self.config.get("iqair_key") or None,
                openaq_key = self.config.get("openaq_key") or None,
            )
        except ValueError as e:
            self.status_lbl.setText(f"⚠  {e}")
            self.refresh_btn.setEnabled(True)
            return

        self.worker = FetchWorker(
            agg, lat, lng, city,
            self.config.get("radius_km", 10),
            self.config.get("hotspot_radius_km", 30),
            self._area_km(),
        )
        self.worker.done.connect(self._on_fetch_done)
        self.worker.error.connect(self._on_fetch_error)
        self.worker.start()

    def _on_fetch_done(self, data):
        self._retry_count = 0

        # Locality card: median of WAQI monitors within the area radius (falls back to nearest)
        area = data.get("area")
        if area:
            self.locality_card.update_data({"aqi": area["median"], "dominant": None,
                                            "station": f"Median of {area['n']} monitors within {area['radius_km']:g} km"})
            self.locality_card.title_lbl.setText("YOUR AREA")
            self.locality_card.advisory_lbl.setText(
                f"Average {area['mean']} · distance-weighted {area['idw']}  (WAQI live)\n"
                + " · ".join(f"{m['name']} {m['km']} km: {m['aqi']:.0f}" for m in area["monitors"]))
            primary = ""
        else:
            self.locality_card.update_data(data.get("locality"))
            primary = data.get("primary_source", "")
        if primary:
            t   = self.locality_card.station_lbl.text()
            sep = "  ·  " if t else ""
            self.locality_card.station_lbl.setText(f"{t}{sep}via {primary}")

        # City card
        city_data = data.get("city")
        if city_data and city_data.get("aqi") is not None:
            self.city_card.update_data(city_data)
        else:
            loc = data.get("locality")
            self.city_card.update_data(
                loc,
                station_override=f"(no city aggregate)  {(loc or {}).get('station', '')}"
            )

        # Consensus card
        consensus_aqi   = data.get("consensus_aqi")
        source_readings = data.get("source_readings") or {}
        ok_readings     = {k: v for k, v in source_readings.items() if v.get("ok")}

        if consensus_aqi is not None:
            parts = [f"{name}: {r['aqi']}" for name, r in ok_readings.items()]
            self.consensus_card.update_data({
                "aqi":      consensus_aqi,
                "station":  "  |  ".join(parts),
                "dominant": None,
            })
            n      = len(ok_readings)
            spread = ""
            if ok_readings:
                hi = max(r["aqi"] for r in ok_readings.values())
                lo = min(r["aqi"] for r in ok_readings.values())
                if hi - lo > 50:
                    spread = "  Wide spread — sensors disagree."
            failed = [k for k, v in source_readings.items() if not v.get("ok")]
            advisory = f"Median of {n} source{'s' if n != 1 else ''}.{spread}"
            if failed:
                advisory += f"  Failed: {', '.join(failed)}"
            self.consensus_card.advisory_lbl.setText(advisory)
        else:
            self.consensus_card.update_data(None)
            self.consensus_card.station_lbl.setText("All sources unavailable")

        # "How is this calculated?" on each card, with a quick check status on Your area
        try:
            self._wire_info_buttons()
        except Exception as e:
            import logging
            logging.getLogger("aqi").warning("verify buttons: %s", e)

        # PM2.5 under each AQI
        try:
            from cards import card_values
            self._last_fetch_data = data
            self._cards = card_values(data, getattr(self, "_outdoor_ctx", {}))
            self.locality_card.set_pm(self._cards["area"])
            self.city_card.set_pm(self._cards["city"])
            self.consensus_card.set_pm(self._cards["consensus"] if consensus_aqi is not None else None)
            self._refresh_badge()
        except Exception as e:
            import logging
            logging.getLogger("aqi").warning("card PM2.5 failed: %s", e)

        # Tables
        self._fill_table(self.nearby_table,   data.get("nearby", []))
        self._fill_table(self.hotspots_table, data.get("hotspots", []))

        # Status
        if data.get("from_cache"):
            age = data.get("cache_age_minutes", "?")
            self._cache_retries += 1
            retry = ""
            if self._cache_retries <= 6:          # network often isn't up yet at login
                QTimer.singleShot(30_000, self._refresh)
                retry = f"  ·  auto-retry in 30 s ({self._cache_retries}/6)"
            self.status_lbl.setStyleSheet("color:#f59e42; font-size:11px; font-weight:600;")
            self.status_lbl.setText(
                f"⚠  ALL LIVE SOURCES FAILED — showing cached data "
                f"({age} min old){retry}  ·  Click Refresh to retry"
            )
        else:
            self._cache_retries = 0
            self._last_refresh = datetime.now()
            if data.get("consensus_aqi") is not None and self._tray:
                self._set_tray_aqi(data.get("consensus_aqi"), "live")
            ts      = datetime.now().strftime("%H:%M:%S")
            src_str = ", ".join(data.get("sources", [])) or "none"
            errs    = data.get("errors", [])
            status  = (
                f"Updated {ts}  ·  {len(ok_readings)} source(s): {src_str}  ·  "
                f"{len(data.get('nearby', []))} nearby station(s)"
            )
            if errs:
                status += f"  ·  {len(errs)} warning(s)"
            self.status_lbl.setStyleSheet("color:#6b6b80; font-size:11px;")
            self.status_lbl.setText(status)

        self.refresh_btn.setEnabled(True)

    def _on_fetch_error(self, err):
        self._retry_count += 1
        if self._retry_count <= 2:
            self.status_lbl.setStyleSheet("color:#f59e42; font-size:11px;")
            self.status_lbl.setText(
                f"⚠  Fetch error: {err}. Retrying in 10s "
                f"(attempt {self._retry_count + 1}/3)…"
            )
            QTimer.singleShot(10000, self._refresh)
        else:
            self.status_lbl.setStyleSheet("color:#ef4444; font-size:11px;")
            self.status_lbl.setText(
                f"⚠  Failed after 3 attempts: {err}. Click Refresh to retry."
            )
            self.refresh_btn.setEnabled(True)

    def _area_km(self):
        area = (self.config.get("recorder") or {}).get("home_area") or {}
        return float(area.get("radius_km", 5))

    # ── Tray / background ────────────────────────────────────────────────────

    def _setup_tray(self):
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        self._tray = QSystemTrayIcon(tray_icon(), self)
        self._tray.setToolTip("AQI Dashboard — recording")
        menu = QMenu()
        for text, slot in (("Open dashboard", self.show_dashboard),
                           ("History", self._open_history),
                           ("Record now", self._record_now),
                           ("Why is the air like this?", self._explain_now),
                           ("Send test message to phone", self._phone_test),
                           ("Open data folder", self._open_data_folder)):
            act = QAction(text, menu)
            act.triggered.connect(slot)
            menu.addAction(act)
        menu.addSeparator()
        quit_act = QAction("Quit (stop recording)", menu)
        quit_act.triggered.connect(self.quit_app)
        menu.addAction(quit_act)
        self._tray_menu = menu
        self._tray.setContextMenu(menu)
        self._tray.messageClicked.connect(self._open_last_saved)
        self._tray.activated.connect(
            lambda reason: self.show_dashboard()
            if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick) else None)
        self._tray.show()

    def _set_tray_aqi(self, aqi, origin):
        try:
            aqi = int(aqi)
        except (TypeError, ValueError):
            return
        self._tray.setIcon(tray_icon(aqi))
        self.setWindowIcon(tray_icon(aqi))
        self._tray.setToolTip(
            f"AQI {aqi} — {aqi_category(aqi)}  ({origin}, {datetime.now().strftime('%H:%M')})\n"
            "Click to open · right-click for menu")

    def show_dashboard(self):
        if getattr(self, "_stack", None) is not None:
            self._stack.setCurrentIndex(0)
        self.showNormal()
        self.raise_()
        self.activateWindow()
        if self._waqi_client and (self._last_refresh is None or
                                  (datetime.now() - self._last_refresh).total_seconds() > 1800):
            self._refresh()

    def show_history(self):
        """History opens as a page inside this window; the button toggles back to the dashboard."""
        if self._stack.currentIndex() == 1:
            self._show_dashboard_page()
            return
        try:
            from history_panel import HistoryWindow
        except ImportError as e:              # e.g. PySide6-Essentials without QtCharts
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.warning(self, "History unavailable",
                                f"Charts need the full PySide6 package (QtCharts).\n\n{e}\n\n"
                                "Fix: pip install --upgrade PySide6")
            return
        labels = {"home": "Your area", "tracked": "Tracked station"}
        if self._recorder:
            labels.update({k: v["label"] for k, v in self._recorder.targets().items()})
        if self._history is None:
            self._history = HistoryWindow(labels)
            back = QPushButton("◂  Dashboard")
            back.setCursor(Qt.PointingHandCursor)
            back.setStyleSheet("QPushButton { background:#7aa2f7; color:#0e0e16; border:none; padding:7px 16px;"
                               " border-radius:7px; font-weight:700; }")
            back.clicked.connect(self._show_dashboard_page)
            self._history.layout().itemAt(0).layout().insertWidget(0, back)
            self._stack.addWidget(self._history)
        else:
            self._history.labels = labels
            for k, cb in self._history.checks.items():
                cb.setText(f"━━  {labels.get(k, k)}")
            self._history.reload()
        self._stack.setCurrentIndex(1)
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _open_history(self):
        if getattr(self, "_stack", None) is not None and self._stack.currentIndex() == 1:
            self.showNormal(); self.raise_(); self.activateWindow()
            return
        self.show_history()

    # ── phone (ntfy) ─────────────────────────────────────────────────────────
    def _setup_phone(self):
        try:
            from phone import PhoneNotifier
            self._phone = PhoneNotifier(self.config)
        except Exception as e:
            import logging
            logging.getLogger("aqi").warning("phone notifications off: %s", e)
            self._phone = None
            return
        self._phone_worker = None
        self._phone_timer = QTimer(self)
        self._phone_timer.timeout.connect(self._phone_tick)
        self._phone_timer.start(60_000)
        self._phone_listener = None
        self._reply_workers = []
        if self._phone.enabled and (self.config.get("phone") or {}).get("commands", True):
            since = self._phone.state.get("last_id") or "10m"
            self._phone_listener = PhoneListener(self._phone.server, self._phone.topic, since)
            self._phone_listener.got.connect(self._on_phone_command)
            self._phone_listener.status.connect(
                lambda st: __import__("logging").getLogger("aqi").info("phone commands: %s", st))
            self._phone_listener.start()

    # ── replies to messages typed in the ntfy app ────────────────────────────
    def _reply_async(self, fn):
        w = FuncWorker(fn)
        self._reply_workers = [x for x in self._reply_workers if x.isRunning()] + [w]
        w.start()

    def _on_phone_command(self, ev):
        import logging
        from phone import parse_command, dot, HELP_TEXT, _category
        from aqi_math import aqi_to_pm25, pm25_to_aqi
        if not self._phone.is_command(ev):
            return
        intent, arg = parse_command(ev.get("message", ""))
        logging.getLogger("aqi").info("phone command %r → %s %s", ev.get("message"), intent, arg or "")
        ph = self._phone
        if intent == "ignore":
            return
        if intent == "help":
            self._reply_async(lambda: ph.send("↩ What you can ask", HELP_TEXT, priority=3))
        elif intent == "unknown":
            self._reply_async(lambda: ph.send("↩ Not a command", "Type help to see what you can ask.", priority=3))
        elif intent == "now":
            ctx, fetch, now = self._phone_ctx(), self._fetch_cards_job(), datetime.now()

            def job():
                c = fetch()
                if c:
                    ctx["cards"] = c
                title, msg, _ = ph.summary(ctx, now, now, ph.state.get("last"))
                return ph.send("↩ " + title, msg, priority=3)
            self._reply_async(job)
        elif intent == "place":
            client, area_km = self._waqi_client, self._area_km()

            def job():
                from geocoder import geocode
                from area import area_reading
                geo = geocode(arg)
                if not geo:
                    return ph.send(f"↩ Couldn't find \"{arg}\"", "Try adding the city, e.g. 'AQI in Rohini Delhi'.", 3)
                a = area_reading(client, geo["lat"], geo["lng"], area_km) if client else None
                if not a:
                    return ph.send(f"↩ {arg.title()}", f"Found ({geo['display_name'][:80]}) but no monitors within 8 km.", 3)
                aqi, pm = a["median"], aqi_to_pm25(a["median"])
                mons = " · ".join(f"{m['name']} {m['aqi']:.0f}" for m in a["monitors"][:5])
                return ph.send(f"↩ {dot(aqi)} {arg.title()} · AQI {aqi:.0f} · PM2.5 ≈{pm:.0f} · {_category(aqi)}",
                               f"Median of {a['n']} monitors within {a['radius_km']:g} km: {mons}\n"
                               f"Found as: {geo['display_name'][:90]}\nPM2.5 in µg/m³, estimated from the live AQI", 3)
            self._reply_async(job)
        elif intent == "why":
            cfg = self.config

            def job():
                import surge
                ex = surge.explain(cfg, trigger="asked from phone")
                body = "\n".join(f"• {t}" for _, t in ex["reasons"][:4]) or "No clear cause in the data."
                if ex.get("unknown"):
                    body += "\n(" + "; ".join(ex["unknown"]) + ")"
                return ph.send("↩ 💡 " + ex["summary"][:110], body, priority=3)
            self._reply_async(job)
        elif intent == "room":
            ind = getattr(self, "_indoor", None)
            rows = list(ind.last.values()) if ind is not None else []
            if ind is None or not rows:
                msg = ("Indoor logging is paused (switch it on in the app)." if ind is not None and not ind.enabled
                       else "No purifier reading yet.")
                self._reply_async(lambda: ph.send("↩ Room", msg, priority=3))
                return
            lines, title = [], None
            for r in rows:
                v = r.get("pm25_ugm3")
                name = r.get("room") or r.get("device")
                if r.get("status") != "ok" or v in (None, ""):
                    lines.append(f"⚪ {name}: {r.get('status')}")
                    continue
                v = float(v)
                line = f"{dot(pm25_to_aqi(v))} {name} · PM2.5 {v:.0f} · purifier {r.get('power')} ({r.get('mode')})"
                title = title or line
                ratio = r.get("indoor_outdoor_ratio")
                extra = []
                if ratio not in (None, ""):
                    extra.append(f"{100 * (1 - float(ratio)):.0f}% lower than outdoor ({r.get('outdoor_openaq_hour')} hour)")
                if r.get("humidity_pct") not in (None, ""):
                    extra.append(f"humidity {r.get('humidity_pct')}%")
                lines.append(line + ("\n   " + " · ".join(extra) if extra else ""))
            body = "\n".join(lines)
            if len(lines) == 1 and title and lines[0].startswith(title):   # one purifier: title says it
                body = lines[0][len(title):].strip() or "—"
            self._reply_async(lambda: ph.send("↩ " + (title or "Room"), body, priority=3))
        elif intent == "graph":
            today = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
            mode, day, label = {"today": ("day", today, "Your area - today"),
                                "yesterday": ("day", today - timedelta(days=1), "Your area - yesterday"),
                                "month": ("month", today, "Your area - this month"),
                                "purifier": ("indoor", today, "Purifier vs outdoor - today")}[arg]
            try:
                png, note = self._render_png(mode, day)
            except Exception as e:
                self._reply_async(lambda: ph.send("↩ Graph failed", str(e)[:150], 3))
                return

            def job():
                ok, err = ph.send_image(png, f"aqi_{arg}.png", label,
                                        (note + " | " if note else "") + "Tap the picture, then pinch to zoom.")
                if not ok:      # attachments refused (size/limits): say so instead of failing silently
                    ph.send("↩ Graph couldn't be sent", err, 3)
                return ok, err
            self._reply_async(job)

    def _fetch_cards_job(self):
        """Returns a function (safe to run in a worker thread) that fetches the same sources as the
        dashboard and returns cards.card_values(), or None on failure."""
        loc = self.config.get("location") or {}
        lat, lng, city = loc.get("lat"), loc.get("lng"), loc.get("city", "")
        cfg, oc, area_km = self.config, dict(self._outdoor_ctx), self._area_km()

        def fetch():
            if lat is None or lng is None:
                return None
            try:
                from cards import card_values
                agg = AQIAggregator(waqi_token=cfg["waqi_token"], owm_key=cfg.get("openweather_key") or None,
                                    iqair_key=cfg.get("iqair_key") or None, openaq_key=cfg.get("openaq_key") or None)
                data = agg.fetch_all(lat, lng, city, cfg.get("radius_km", 10), cfg.get("hotspot_radius_km", 30))
                try:
                    from area import area_reading
                    data["area"] = area_reading(agg.waqi, lat, lng, area_km)
                except Exception:
                    pass
                return card_values(data, oc)
            except Exception as e:
                import logging
                logging.getLogger("aqi").warning("phone: fetch for card values failed: %s", e)
                return None
        return fetch

    def _phone_ctx(self):
        ctx = dict(getattr(self, "_outdoor_ctx", {}) or {})
        ctx["city_name"] = (self.config.get("location") or {}).get("city") or "City"
        try:
            import surge
            cs = surge.city_series(surge.DATA_DIR, datetime.now().astimezone(), hours=2)
            if cs:
                ctx["city_aqi"] = cs[-1][1]
        except Exception:
            pass
        ind = getattr(self, "_indoor", None)
        if ind is not None and ind.enabled:
            ctx["indoor"] = [{"device": r.get("device"), "room": r.get("room"), "pm25": r.get("pm25_ugm3"),
                              "power": r.get("power"), "status": r.get("status")} for r in ind.last.values()]
        return ctx

    def _phone_tick(self):
        if not (self._phone and self._phone.enabled) or self._phone.due_slot() is None:
            return
        if self._phone_worker is not None and self._phone_worker.isRunning():
            return
        ctx = self._phone_ctx()
        fetch = self._fetch_cards_job()

        def job():
            c = fetch()                          # fresh card values, fetched off the UI thread
            if c:
                ctx["cards"] = c
            return self._phone.maybe_send_update(ctx)
        self._phone_worker = FuncWorker(job)
        self._phone_worker.done.connect(self._phone_done)
        self._phone_worker.start()

    def _phone_done(self, res):
        import logging
        if res and not res.get("ok", True):
            logging.getLogger("aqi").warning("phone update %s not sent: %s", res.get("slot"), res.get("error"))
        elif res:
            logging.getLogger("aqi").info("phone update %s sent", res.get("slot"))

    def _phone_test(self):
        if not self._phone or not self._phone.enabled:
            self._show_alert({"kind": "info", "title": "Phone messages are off",
                              "body": "Add a \"phone\" block with a topic to config.json (see phone.py)."})
            return
        ctx = self._phone_ctx()
        now = datetime.now()
        fetch = self._fetch_cards_job()

        def job():
            c = fetch()
            if c:
                ctx["cards"] = c
            title, msg, _ = self._phone.summary(ctx, now, now, self._phone.state.get("last"))
            return self._phone.send("Test · " + title, msg, priority=self._phone.priority)
        self._phone_test_worker = FuncWorker(job)
        self._phone_test_worker.done.connect(lambda r: self._show_alert(
            {"kind": "info", "title": "Test message sent to your phone" if r and r[0] else "Test message failed",
             "body": (f"Topic: {self._phone.topic} on {self._phone.server}. It should appear in the ntfy app "
                      "within a few seconds." if r and r[0] else f"{r[1] if r else 'unknown error'}")}))
        self._phone_test_worker.start()
        self.show_dashboard()

    def _wire_info_buttons(self):
        if not getattr(self, "_info_wired", False):
            self.locality_card.info_btn.clicked.connect(lambda: self._show_verify("area"))
            self.city_card.info_btn.clicked.connect(lambda: self._show_verify("city"))
            self.consensus_card.info_btn.clicked.connect(lambda: self._show_verify("consensus"))
            self._info_wired = True
        for c in (self.locality_card, self.city_card, self.consensus_card):
            c.info_btn.show()

    def _refresh_badge(self):
        """Run the cross-checks and put the result next to Your area's info link."""
        try:
            import verify
            from recorder import Store
            chks = verify.checks(Store(), (self._last_fetch_data or {}).get("area"),
                                 verify.latest_home_row(Store()), self._cards)
            self.locality_card.info_btn.setText(f"ⓘ  How is this calculated?   {verify.badge(chks)}")
        except Exception as e:
            import logging
            logging.getLogger("aqi").warning("checks failed: %s", e)

    def _show_verify(self, kind):
        from PySide6.QtWidgets import QDialog, QTextBrowser
        import verify
        from recorder import Store
        data, cards = self._last_fetch_data or {}, self._cards or {}
        if kind == "area":
            html, _ = verify.area_html(Store(), data, cards, self._outdoor_ctx)
        elif kind == "city":
            html = verify.city_html(data, cards)
        else:
            html = verify.consensus_html(data, cards)
        dlg = QDialog(self)
        dlg.setWindowTitle("How is this calculated?")
        dlg.resize(820, 640)
        lay = QVBoxLayout(dlg)
        tb = QTextBrowser()
        tb.setOpenExternalLinks(True)
        tb.setStyleSheet("QTextBrowser { background:#16161f; color:#d8d8e8; border:none; font-size:13px; padding:12px; }")
        tb.document().setDefaultStyleSheet("a { color:#7aa2f7; } h3 { color:#ffffff; } h4 { color:#c0c0d8; }")
        tb.setHtml(html)
        lay.addWidget(tb)
        dlg.exec()

    def _open_purifier_graph(self):
        """Indoor panel button → History page in Purifier mode, today, live."""
        if self._stack.currentIndex() != 1:
            self.show_history()
        if self._history is None:
            return
        self._history.day = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
        self._history._set_mode("indoor")

    def _show_dashboard_page(self):
        self._stack.setCurrentIndex(0)

    def _open_data_folder(self):
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices
        from recorder import DATA_DIR
        os.makedirs(DATA_DIR, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(DATA_DIR))

    def closeEvent(self, event):
        if self._tray is not None and not self._quitting:
            event.ignore()
            self.hide()
            if not self._tray_hint_shown:
                self._tray.showMessage(
                    "AQI Dashboard is still recording",
                    "It keeps logging in the background. Right-click the tray icon → Quit to stop.",
                    QSystemTrayIcon.Information, 5000)
                self._tray_hint_shown = True
            return
        event.accept()
        QApplication.quit()

    def quit_app(self):
        self._quitting = True
        if getattr(self, "_phone_listener", None) is not None:
            self._phone_listener.stop()
        if self._rec_worker is not None and self._rec_worker.isRunning():
            self._rec_worker.wait(8000)          # don't cut a CSV write in half
        if self._tray:
            self._tray.hide()
        QApplication.quit()

    # ── Recorder ─────────────────────────────────────────────────────────────

    def _setup_indoor(self):
        cfg = self.config.get("indoor") or {}
        if not cfg.get("devices"):
            return
        try:
            from indoor import IndoorLogger
            self._indoor = IndoorLogger(self.config)
        except Exception as e:
            self.rec_lbl.setText(self.rec_lbl.text() + f"\nIndoor logging off: {e}")
            return
        if not self._indoor.devices:
            return
        from PySide6.QtWidgets import QComboBox
        rooms = ["Bedroom", "Living room", "Parents' room", "Kitchen", "Other"]
        for dev in self._indoor.devices:
            row = QHBoxLayout()
            name = QLabel(dev.name)
            name.setStyleSheet("color:#d8d8e8; font-weight:700; border:none;")
            room = QComboBox()
            room.setEditable(True)
            room.addItems(rooms if self._indoor.room(dev) in rooms else [self._indoor.room(dev)] + rooms)
            room.setCurrentText(self._indoor.room(dev))
            room.setStyleSheet("QComboBox { background:#232333; color:#d8d8e8; border:1px solid #3a3a52;"
                               " border-radius:6px; padding:3px 8px; }")
            room.currentTextChanged.connect(lambda t, n=dev.name: self._indoor.set_room(n, t))
            val = QLabel("waiting for first reading…")
            val.setStyleSheet("color:#b0b0c4; border:none;")
            val.setWordWrap(True)
            row.addWidget(name); row.addWidget(QLabel("in", styleSheet="color:#6b6b80; border:none;"))
            row.addWidget(room); row.addWidget(val, 1)
            self.indoor_lay.addLayout(row)
            self.indoor_rows[dev.name] = val
        self.indoor_box.show()
        self._indoor_worker = None
        self._indoor_timer = QTimer(self)
        self._indoor_timer.setSingleShot(True)
        self._indoor_timer.timeout.connect(self._poll_indoor)
        self._indoor_tray_act = None
        if getattr(self, "_tray_menu", None) is not None:
            act = QAction("Indoor logging", self._tray_menu)
            act.setCheckable(True)
            act.toggled.connect(self._set_indoor_enabled)
            self._tray_menu.insertAction(self._tray_menu.actions()[-2], act)   # above the separator
            self._indoor_tray_act = act
        self.indoor_toggle.toggled.connect(self._set_indoor_enabled)
        self._sync_indoor_toggle()
        if self._indoor.enabled:
            QTimer.singleShot(5_000, self._poll_indoor)
        else:
            self._show_indoor_paused()

    def _sync_indoor_toggle(self):
        on = self._indoor.enabled
        for w in (self.indoor_toggle, self._indoor_tray_act):
            if w is not None and w.isChecked() != on:
                w.blockSignals(True); w.setChecked(on); w.blockSignals(False)
        self.indoor_toggle.setText("Indoor logging: ON" if on else "Indoor logging: PAUSED")

    def _show_indoor_paused(self):
        for lbl in self.indoor_rows.values():
            lbl.setText("<span style='color:#6b6b80'>paused — no readings, nothing written. "
                        "Click the switch to resume.</span>")
            lbl.setTextFormat(Qt.RichText)

    def _set_indoor_enabled(self, on):
        self._indoor.set_enabled(on)
        self._sync_indoor_toggle()
        if on:
            for lbl in self.indoor_rows.values():
                lbl.setText("resuming…")
            self._indoor_timer.stop()
            QTimer.singleShot(500, self._poll_indoor)
        else:
            self._indoor_timer.stop()
            self._show_indoor_paused()

    def _poll_indoor(self):
        if not self._indoor.enabled:
            return
        if self._indoor_worker is not None and self._indoor_worker.isRunning():
            return
        self._indoor_worker = IndoorWorker(self._indoor, dict(self._outdoor_ctx))
        self._indoor_worker.done.connect(self._on_indoor)
        self._indoor_worker.start()

    def _on_indoor(self, rows):
        if not self._indoor.enabled:                   # paused while a poll was in flight
            self._show_indoor_paused()
            return
        for r in rows:
            lbl = self.indoor_rows.get(r.get("device"))
            if lbl is None:
                continue
            if r.get("status") != "ok":
                lbl.setText(f"<span style='color:#f59e42'>{r.get('status')}</span>")
            else:
                pm = r.get("pm25_ugm3")
                col = aqi_color(int(r["standards"].split()[2]) if r.get("standards", "").startswith("US AQI") else None)
                ratio = r.get("indoor_outdoor_ratio")
                lbl.setText(f"<b style='color:{col}'>PM2.5 {pm} µg/m³</b> · {r.get('power')} · {r.get('mode')}"
                            + (f" · {float(ratio) * 100:.0f}% of outdoor" if ratio not in ("", None) else "")
                            + (f" · humidity {r.get('humidity_pct')}%" if r.get("humidity_pct") not in ("", None) else "")
                            + (f"<br><span style='color:#f59e42'>{r['event']}</span>" if r.get("event") else "")
                            + (f"<br><span style='color:#8a8aa3'>{r.get('suggestion')}</span>" if r.get("suggestion") else ""))
            lbl.setTextFormat(Qt.RichText)
        self._indoor_timer.start(self._indoor.next_interval() * 1000)

    def _setup_recorder(self):
        rc = self.config.get("recorder") or {}
        if not rc.get("enabled", True):
            self.rec_lbl.setText("Recorder: disabled in config.json (recorder.enabled = false)")
            return
        key = self.config.get("openaq_key")
        if not key:
            self.rec_lbl.setStyleSheet("color:#f59e42; font-size:11px;")
            self.rec_lbl.setText("Recorder off: add an \"openaq_key\" to config.json "
                                 "(free at explore.openaq.org → account) — OpenAQ supplies the hourly history.")
            return
        from openaq import OpenAQClient
        from recorder import Recorder
        self._recorder = Recorder(self.config, OpenAQClient(key), self._waqi_client)
        self._rec_interval_ms = max(5, int(rc.get("interval_min", 15))) * 60_000
        self._rec_timer = QTimer(self)
        self._rec_timer.setSingleShot(True)
        self._rec_timer.timeout.connect(self._record_now)
        self.rec_lbl.setText("Recorder: first sync in 20 s (back-fills up to "
                             f"{int(rc.get('backfill_days', 31))} days on first run)…")
        QTimer.singleShot(20_000, self._record_now)

    def _record_now(self):
        if not self._recorder:
            return
        if self._rec_worker is not None and self._rec_worker.isRunning():
            return
        self.rec_lbl.setText("Recorder: syncing…")
        self._rec_worker = RecorderWorker(self._recorder, self._after_sync)
        self._rec_worker.done.connect(self._on_record_done)
        self._rec_worker.start()

    def _on_record_done(self, st):
        now = datetime.now()
        got_data = any(t.get("rows") for t in st.get("targets", {}).values())
        if st.get("ok") or got_data:
            self._rec_fail_streak = 0
            delay = self._rec_interval_ms
        else:
            self._rec_fail_streak += 1             # e.g. no network yet: retry sooner
            delay = min(self._rec_interval_ms, 120_000 * self._rec_fail_streak)
        self._rec_timer.start(delay)
        nxt = (now + timedelta(milliseconds=delay)).strftime("%H:%M")

        parts = []
        for t in st.get("targets", {}).values():
            if t.get("aqi") not in (None, ""):
                hr = str(t.get("latest_hour", ""))[11:16]
                parts.append(f"{t['label']}: AQI {t['aqi']} / PM2.5 {t.get('pm25')} µg/m³ (hour {hr})")
        msg = (f"Recorder: synced {now.strftime('%H:%M')} · +{st.get('added', 0)} new hour(s)"
               f"{', ' + str(st.get('updated')) + ' revised' if st.get('updated') else ''} · next {nxt}")
        if parts:
            msg += "\n" + "   ·   ".join(parts)
        if st.get("errors"):
            msg += "\n⚠ " + " | ".join(st["errors"])[:400]
        self.rec_lbl.setStyleSheet(
            f"color:{'#f59e42' if st.get('errors') else '#6b6b80'}; font-size:11px;")
        if getattr(self, "_saved_note", None):
            msg += "\n" + self._saved_note
        self.rec_lbl.setText(msg)

        home = st.get("targets", {}).get("home", {})
        if hasattr(self, "_outdoor_ctx"):
            if home.get("pm25") not in (None, ""):
                self._outdoor_ctx.update(pm25=float(home["pm25"]), hour=str(home.get("latest_hour", ""))[11:16])
                if getattr(self, "_last_fetch_data", None):
                    from cards import card_values
                    self._cards = card_values(self._last_fetch_data, self._outdoor_ctx)
                    self.locality_card.set_pm(self._cards["area"])
                    self.consensus_card.set_pm(self._cards["consensus"])
            if home.get("waqi_aqi") not in (None, "", "-"):
                self._outdoor_ctx["waqi_aqi"] = home["waqi_aqi"]
        if self._tray and (self._last_refresh is None or
                           (now - self._last_refresh).total_seconds() > 1800):
            if home.get("waqi_aqi") not in (None, "", "-"):
                self._set_tray_aqi(home["waqi_aqi"], "WAQI live")
            elif home.get("aqi") not in (None, ""):
                self._set_tray_aqi(home["aqi"], "OpenAQ hourly")
        if self._history is not None and self._history.isVisible():
            self._history.reload()
        for a in st.get("alerts") or []:
            self._show_alert(a)
        if st.get("added") or st.get("updated") or self._last_snap is None or \
                (now - self._last_snap).total_seconds() > 3300:
            self._save_snapshots()

    def _ensure_snap(self, labels=None):
        from history_panel import HistoryWindow
        if labels is None:
            labels = {"home": "Your area", "tracked": "Tracked station"}
            if self._recorder:
                labels.update({k: v["label"] for k, v in self._recorder.targets().items()})
        if self._snap is None:
            self._snap = HistoryWindow(labels)
            self._snap.setAttribute(Qt.WA_DontShowOnScreen, True)   # laid out and drawn, never on screen
            self._snap.resize(1700, 1080)
            self._snap.show()
        return self._snap

    def _render_png(self, mode, day):
        """Phone picture: charts only, portrait, ~2× text (a separate off-screen History in phone
        mode). Returns (PNG bytes, one-line summary in plain ASCII)."""
        import re
        from PySide6.QtCore import QBuffer, QByteArray, QIODevice
        from history_panel import HistoryWindow
        labels = {"home": "Your area", "tracked": "Tracked station"}
        if self._recorder:
            labels.update({k: v["label"] for k, v in self._recorder.targets().items()})
        if getattr(self, "_phone_snap", None) is None:
            self._phone_snap = HistoryWindow(labels, phone=True)
            self._phone_snap.setAttribute(Qt.WA_DontShowOnScreen, True)
            self._phone_snap.show()
        snap = self._phone_snap
        snap.labels = labels
        if self._history is not None:
            snap.method = self._history.method
        snap.resize(1080, 1000 if mode == "indoor" else 1600)    # portrait; one chart for the purifier
        snap.day = day
        snap._set_mode(mode)
        QApplication.processEvents()
        snap._layers[0].layout(); snap._layers[1].layout()
        ba = QByteArray()
        buf = QBuffer(ba)
        buf.open(QIODevice.WriteOnly)
        snap.grab().save(buf, "PNG")
        buf.close()
        first = snap.summary.text().split("<br>")[0]
        text = re.sub(r"<[^>]+>", "", first).replace("&nbsp;", " ")
        for a, b in (("µg/m³", "ug/m3"), ("³", "3"), ("→", "->"), ("·", "-"), ("≈", "~"), ("–", "-"), ("—", "-"),
                     ("▲", "^"), ("▸", ">")):
            text = text.replace(a, b)
        return bytes(ba), text.encode("ascii", "ignore").decode()[:220]

    def _save_snapshots(self):
        """Overwrite data/snapshot_today.png and data/snapshot_month.png with the current charts."""
        try:
            from history_panel import HistoryWindow
            from recorder import DATA_DIR
            labels = {"home": "Your area", "tracked": "Tracked station"}
            if self._recorder:
                labels.update({k: v["label"] for k, v in self._recorder.targets().items()})
            snap = self._ensure_snap(labels)
            snap.labels = labels
            for k, cb in snap.checks.items():
                cb.setText(f"━━  {labels.get(k, k)}")
            if self._history is not None:                               # same method as your History view
                snap.method = self._history.method
                snap.method_btns[snap.method].setChecked(True)
            os.makedirs(DATA_DIR, exist_ok=True)
            for mode, name in (("day", "snapshot_today.png"), ("month", "snapshot_month.png")):
                snap.mode = mode
                (snap.day_btn if mode == "day" else snap.month_btn).setChecked(True)
                snap._today()
                QApplication.processEvents()
                path = os.path.join(DATA_DIR, name)
                tmp = path + ".tmp.png"
                if snap.grab().save(tmp, "PNG"):
                    os.replace(tmp, path)
            self._last_snap = datetime.now()
            self._save_daily_images()
            today = datetime.now().date()
            if getattr(self, "_last_eval", None) != today:          # refresh evaluation once a day
                from evaluate import evaluate
                evaluate()
                self._last_eval = today
        except Exception as e:
            import logging
            logging.getLogger("aqi").warning("snapshot failed: %s", e)

    # ── Spike alerts ─────────────────────────────────────────────────────────

    def _after_sync(self):
        """Runs in the recorder thread: city-wide snapshot, then alerts (+ likely cause)."""
        import logging
        import surge
        try:
            surge.ensure_events_template()
            if self._waqi_client is not None:
                surge.record_city(self._waqi_client, config=self.config)
        except Exception as e:
            logging.getLogger("aqi").warning("city-wide snapshot failed: %s", e)
        try:                                     # satellite-model PM2.5 (CAMS), at most hourly
            import cams
            cams.update(self.config)
        except Exception as e:
            logging.getLogger("aqi").warning("satellite model fetch failed: %s", e)
        if getattr(self, "_alerts", None) is None:
            from alerts import AlertManager
            self._alerts = AlertManager(self.config)
        alerts = self._alerts.check(explainer=lambda trig: surge.explain(self.config, trigger=trig))
        phone = getattr(self, "_phone", None)
        if phone is not None:
            for a in alerts:
                res = phone.send_alert(a)
                if res and not res[0]:
                    logging.getLogger("aqi").warning("phone alert not sent: %s", res[1])
        return alerts

    def _explain_now(self):
        if getattr(self, "_explain_worker", None) is not None and self._explain_worker.isRunning():
            return
        self.alert_bar.setStyleSheet("QFrame { background:#1b2433; border:1px solid #38bdf8; border-radius:8px; }")
        self.alert_lbl.setText("<b>Checking why the air is like this…</b> (calendar, city-wide data, weather)")
        self.alert_bar.show()
        self.show_dashboard()
        self._explain_worker = ExplainWorker(self.config)
        self._explain_worker.done.connect(
            lambda ex: self._show_alert({"kind": "info", "title": "Why the air is like this", "body": ex["summary"],
                                         "reasons": ex.get("reasons", []), "unknown": ex.get("unknown", [])}))
        self._explain_worker.start()

    def _show_alert(self, a):
        spike = a["kind"] == "spike"
        info = a["kind"] == "info"
        when = datetime.now().strftime("%H:%M")
        bg, border, fg = (("#3a1d24", "#ef4444", "#fecaca") if spike else
                          ("#1b2433", "#38bdf8", "#dbeafe") if info else ("#16302a", "#3ecf8e", "#bbf7d0"))
        self.alert_bar.setStyleSheet("QFrame { background:%s; border:1px solid %s; border-radius:8px; }" % (bg, border))
        self.alert_lbl.setStyleSheet(f"color:{fg}; font-size:13px; border:none;")
        detail = ""
        if a.get("reasons"):
            detail = "<br>" + "<br>".join(f"&nbsp;• {t}" for _, t in a["reasons"][:6])
        if a.get("unknown"):
            detail += "<br><span style='color:#9ca3af'>" + "; ".join(a["unknown"]) + "</span>"
        body = a["body"].split(" Likely: ")[0] + (" Likely causes:" if a.get("reasons") and not info else "") \
            if a.get("reasons") else a["body"]
        self.alert_lbl.setText(f"<b>{a['title']}</b> &nbsp;·&nbsp; {body} "
                               f"<span style='color:#9ca3af'>({when})</span>"
                               + (f"<span style='font-size:12px'>{detail}</span>" if detail else ""))
        self.alert_lbl.setToolTip("<br>".join(t for _, t in a.get("reasons", [])) or "")
        self.alert_bar.show()
        if info:
            return
        away = not self.isVisible() or self.isMinimized() or not self.isActiveWindow()
        if self._tray and away:                       # toast near the taskbar when the app isn't in front
            self._tray.showMessage(a["title"], a["body"],
                                   QSystemTrayIcon.Warning if spike else QSystemTrayIcon.Information, 15000)
        if spike and getattr(getattr(self, "_alerts", None), "sound", True):
            try:
                import winsound
                winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
            except Exception:
                QApplication.beep()

    # ── One image per day: data/daily_images/YYYY-MM-DD.png ─────────────────

    def _day_hours(self, day_iso):
        """How many hourly readings exist for a date (both lines) — used to see if a day grew."""
        from recorder import Store
        st = Store()
        n = 0
        for r in st.read(st.path("readings", day_iso[:7])):
            if r["target"] in ("home", "tracked") and r["hour_start_local"][:10] == day_iso \
                    and (r.get("pm25_ugm3") or r.get("pm10_ugm3")):
                n += 1
        return n

    def _save_daily_images(self):
        """
        Saves each completed day's chart as data/daily_images/YYYY-MM-DD.png the NEXT MORNING
        (first sync after 06:00), when all of that day's hours have had time to arrive from
        OpenAQ (~1–2 h delay). A tray notification says it was saved; clicking it opens the image.
        Days missed while the laptop was off are caught up on the next run (up to 7 days back).
        If an outage is filled in later, the image is quietly updated with the extra hours.
        """
        import json as _json
        from recorder import DATA_DIR, Store
        folder = os.path.join(DATA_DIR, "daily_images")
        os.makedirs(folder, exist_ok=True)
        log_path = os.path.join(folder, "rendered.json")          # {date: hours present when drawn}
        try:
            with open(log_path, encoding="utf-8") as f:
                done = _json.load(f)
        except (OSError, ValueError):
            done = {}
        now = datetime.now()
        first = 1 if now.hour >= 6 else 2                          # yesterday only after 06:00
        days = [(now - timedelta(days=i)).date().isoformat() for i in range(7, first - 1, -1)]
        snap, new, changed = self._snap, [], False
        for d in days:
            n = self._day_hours(d)
            path = os.path.join(folder, f"{d}.png")
            if n == 0 or (os.path.exists(path) and done.get(d, -1) >= n):
                continue                                         # nothing recorded, or image up to date
            snap.mode = "day"
            snap.day_btn.setChecked(True)
            snap.day = datetime.fromisoformat(d).astimezone()
            snap.reload()
            QApplication.processEvents()
            tmp = path + ".tmp.png"
            if snap.grab().save(tmp, "PNG"):
                is_new = not os.path.exists(path)
                os.replace(tmp, path)
                if is_new:
                    new.append((d, path))
                done[d] = n
                changed = True
        if changed:
            with open(log_path, "w", encoding="utf-8") as f:
                _json.dump(done, f, indent=1)
        if new:
            self._notify_saved(new, Store())

    def _notify_saved(self, new, store):
        d, path = new[-1]
        self._last_saved_image = path
        line = ""
        for r in store.read(store.path("daily", d[:7])):          # that day's numbers for your area
            if r["date"] == d and r["target"] == "home" and r.get("pm25_mean_ugm3"):
                line = (f"Your area: average PM2.5 {float(r['pm25_mean_ugm3']):.0f} µg/m³, "
                        f"AQI {r['aqi_daily_us']} ({r['aqi_category']}), {r['pm25_hours']}/24 h.")
        day_txt = datetime.fromisoformat(d).strftime("%a %d %b")
        title = f"Chart saved for {day_txt}" if len(new) == 1 else f"{len(new)} daily charts saved (latest {day_txt})"
        body = f"{line}\nClick to open · data\\daily_images\\{os.path.basename(path)}".strip()
        if self._tray:
            self._tray.showMessage(title, body, QSystemTrayIcon.Information, 12000)
        self._saved_note = f"📷 {title} — {datetime.now().strftime('%H:%M')}"
        if hasattr(self, "rec_lbl"):
            self.rec_lbl.setText(self.rec_lbl.text() + "\n" + self._saved_note)

    def _open_last_saved(self):
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices
        path = getattr(self, "_last_saved_image", None)
        if path and os.path.exists(path):
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    # ── Search ───────────────────────────────────────────────────────────────

    def _do_search(self):
        query = self.search_input.text().strip()
        if not query:
            return
        if not self._waqi_client:
            self.search_status.setText("⚠  WAQI client not initialized.")
            return

        self.search_btn.setEnabled(False)
        self.search_result_card.hide()
        self.search_status.setStyleSheet("color:#6b6b80; font-size:11px;")
        self.search_status.setText(f"Locating '{query}'…")

        self.geo_worker = GeoSearchWorker(self._waqi_client, query, self._area_km())
        self.geo_worker.done.connect(self._on_geo_search_done)
        self.geo_worker.error.connect(self._on_geo_search_error)
        self.geo_worker.start()

    def _on_geo_search_done(self, data):
        self.search_btn.setEnabled(True)
        resolved = data.get("_resolved_to", "")
        geo_src  = data.get("_geo_source", "")
        aqi      = data.get("aqi")
        station  = data.get("station", "")
        self.search_status.setStyleSheet("color:#6b6b80; font-size:11px;")
        self.search_status.setText(
            f"Resolved via {geo_src}: {resolved}  →  "
            f"{station}  —  AQI {aqi} ({aqi_category(aqi)}), WAQI live"
        )
        self.search_result_card.update_data(data)

    def _on_geo_search_error(self, err):
        self.search_btn.setEnabled(True)
        self.search_status.setStyleSheet("color:#f59e42; font-size:11px;")
        self.search_status.setText(f"⚠  {err}")

    # ── Table fill ───────────────────────────────────────────────────────────

    def _fill_table(self, table, stations):
        table.setRowCount(0)
        if not stations:
            table.setRowCount(1)
            empty = QTableWidgetItem("No data available")
            empty.setForeground(QColor("#6b6b80"))
            table.setItem(0, 0, empty)
            for c in range(1, table.columnCount()):
                table.setItem(0, c, QTableWidgetItem(""))
            return

        for s in stations:
            row = table.rowCount()
            table.insertRow(row)

            station_obj = s.get("station")
            name = (
                station_obj.get("name", "—")
                if isinstance(station_obj, dict)
                else str(station_obj or "—")
            )
            name_item = QTableWidgetItem(name)
            name_item.setForeground(QColor("#e0e0f0"))
            table.setItem(row, 0, name_item)

            aqi = s.get("aqi_int") or s.get("aqi")
            if isinstance(aqi, str):
                try:
                    aqi = int(aqi)
                except (ValueError, TypeError):
                    aqi = None
            color    = aqi_color(aqi)
            aqi_item = QTableWidgetItem(str(aqi) if aqi is not None else "—")
            aqi_item.setForeground(QColor(color))
            f = aqi_item.font()
            f.setBold(True)
            f.setPointSize(13)
            aqi_item.setFont(f)
            aqi_item.setTextAlignment(Qt.AlignCenter)
            table.setItem(row, 1, aqi_item)

            cat_item = QTableWidgetItem(aqi_category(aqi))
            cat_item.setForeground(QColor(color))
            table.setItem(row, 2, cat_item)

            d      = s.get("distance_km")
            d_item = QTableWidgetItem(f"{d:.2f}" if isinstance(d, (int, float)) else "—")
            d_item.setForeground(QColor("#b0b0c4"))
            d_item.setTextAlignment(Qt.AlignCenter)
            table.setItem(row, 3, d_item)


# ─────────────────────────── Entry point ────────────────────────────────────

def _setup_logging():
    import logging
    from logging.handlers import RotatingFileHandler
    log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard.log")
    h = RotatingFileHandler(log_path, maxBytes=200_000, backupCount=1, encoding="utf-8")
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log = logging.getLogger("aqi")
    log.setLevel(logging.INFO)
    log.addHandler(h)
    return log


def main():
    log = _setup_logging()
    sys.excepthook = lambda t, v, tb: log.error("uncaught exception", exc_info=(t, v, tb))
    from autostart import ensure_autostart
    log.info("start: %s | autostart %s | %s", sys.executable, ensure_autostart(), environment_info())

    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setFont(QFont("Segoe UI", 9))
    app.setQuitOnLastWindowClosed(False)       # keep recording in the tray

    # Single instance: a second launch just brings the running one forward,
    # so two recorders never write the same CSV.
    from PySide6.QtNetwork import QLocalServer, QLocalSocket
    name = "AQIDashboard-" + (os.environ.get("USERNAME") or os.environ.get("USER") or "user")
    probe = QLocalSocket()
    probe.connectToServer(name)
    if probe.waitForConnected(400):
        probe.write(b"show")
        probe.flush()
        probe.waitForBytesWritten(400)
        log.info("already running — asked it to show itself")
        return 0

    win = DashboardWindow()
    server = QLocalServer()
    QLocalServer.removeServer(name)
    server.listen(name)
    server.newConnection.connect(lambda: (server.nextPendingConnection(), win.show_dashboard()))
    win._server = server
    win.show()
    if win._tray is None:                    # no tray (or setup error) → closing quits
        app.setQuitOnLastWindowClosed(True)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
