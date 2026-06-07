"""
AQI Dashboard — PySide6 popup window.

Features:
  - Multi-source fallback chain (WAQI → IQAir → OpenAQ → OWM)
  - Consensus AQI (median of all responding sources)
  - Stale-cache warning when all live sources fail
  - Locality search: type any area name → geocode → nearest WAQI station
"""
import json
import os
import sys
from datetime import datetime

from PySide6.QtCore import Qt, QThread, Signal, QTimer
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QLabel,
    QVBoxLayout, QHBoxLayout, QFrame, QPushButton,
    QTableWidget, QTableWidgetItem, QHeaderView,
    QSizePolicy, QLineEdit, QScrollArea,
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
    return "#7f1d1d"


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

    def __init__(self, aggregator, lat, lng, city, radius, hot_radius):
        super().__init__()
        self.agg       = aggregator
        self.lat       = lat
        self.lng       = lng
        self.city      = city
        self.radius    = radius
        self.hot_radius = hot_radius

    def run(self):
        try:
            self.done.emit(self.agg.fetch_all(
                self.lat, self.lng, self.city, self.radius, self.hot_radius
            ))
        except Exception as e:
            self.error.emit(str(e))


class GeoSearchWorker(QThread):
    """Geocode a locality query → lat/lng → nearest WAQI station AQI."""
    done  = Signal(dict)
    error = Signal(str)

    def __init__(self, waqi_client, query):
        super().__init__()
        self.client = waqi_client
        self.query  = query

    def run(self):
        try:
            from geocoder import geocode
            geo = geocode(self.query)
            if not geo:
                self.error.emit(
                    f"Could not find '{self.query}' on the map. "
                    "Try adding the city name, e.g. 'West Patel Nagar Delhi'."
                )
                return

            data = self.client.by_geo(geo["lat"], geo["lng"])
            if not data:
                self.error.emit("Location found but no nearby AQI station.")
                return

            data["_queried"]     = self.query
            data["_resolved_to"] = geo["display_name"]
            data["_geo_lat"]     = geo["lat"]
            data["_geo_lng"]     = geo["lng"]
            data["_geo_source"]  = geo.get("source", "")
            self.done.emit(data)

        except Exception as e:
            self.error.emit(str(e))


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
        lay.addStretch(1)
        lay.addWidget(self.station_lbl)
        lay.addWidget(self.advisory_lbl)

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
        if nearest:
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

        # Search bar
        outer.addWidget(self._section_label("Search any locality / city"))
        search_row = QHBoxLayout()
        search_row.setSpacing(10)
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText(
            "e.g.  West Patel Nagar Delhi  /  Bandra Mumbai  /  Koramangala Bengaluru"
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

        scroll.setWidget(central)
        self.setCentralWidget(scroll)

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
        )
        self.worker.done.connect(self._on_fetch_done)
        self.worker.error.connect(self._on_fetch_error)
        self.worker.start()

    def _on_fetch_done(self, data):
        self._retry_count = 0

        # Locality card
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

        # Tables
        self._fill_table(self.nearby_table,   data.get("nearby", []))
        self._fill_table(self.hotspots_table, data.get("hotspots", []))

        # Status
        if data.get("from_cache"):
            age = data.get("cache_age_minutes", "?")
            self.status_lbl.setStyleSheet("color:#f59e42; font-size:11px; font-weight:600;")
            self.status_lbl.setText(
                f"⚠  ALL LIVE SOURCES FAILED — showing cached data "
                f"({age} min old)  ·  Click Refresh to retry"
            )
        else:
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

        self.geo_worker = GeoSearchWorker(self._waqi_client, query)
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
            f"Nearest station: {station}  —  AQI {aqi} ({aqi_category(aqi)})"
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

def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setFont(QFont("Segoe UI", 9))
    win = DashboardWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
