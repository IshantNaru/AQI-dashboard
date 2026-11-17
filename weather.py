"""
weather.py — hourly weather at your location, saved next to the air-quality data so the charts can
show WHY PM2.5 moved (calm wind and a shallow mixing layer trap pollution; wind and rain clear it).

Source: Open-Meteo forecast API (free, no key) — the same source surge.py's "likely cause" uses.
Its `past_days` returns the last days of the model's hourly analysis (up to 92 days back).

Stored in data/weather_YYYY-MM.csv (local time, one row per hour):
  time_local, temp_c, rh_pct, wind_kmh, wind_dir_deg, mixing_height_m, rain_mm

  • wind_dir_deg is where the wind blows FROM (meteorological convention): 315 = from the north-west.
  • mixing_height_m = boundary-layer height: how deep the air near the ground can mix. Below ~300 m
    (typical on calm winter nights) smoke and exhaust are trapped in a thin layer.
"""
import csv
import json
import os
from datetime import datetime, timedelta

import requests

from recorder import DATA_DIR

URL = "https://api.open-meteo.com/v1/forecast"
FIELDS = ["time_local", "temp_c", "rh_pct", "wind_kmh", "wind_dir_deg", "mixing_height_m", "rain_mm"]
API = {"temp_c": "temperature_2m", "rh_pct": "relative_humidity_2m", "wind_kmh": "wind_speed_10m",
       "wind_dir_deg": "wind_direction_10m", "mixing_height_m": "boundary_layer_height",
       "rain_mm": "precipitation"}
SOURCE = "Open-Meteo (hourly model analysis)"


def fetch(lat, lng, past_days=2, session=requests):
    """Hourly weather, local time. Returns [(datetime_local_naive, {field: value})]."""
    r = session.get(URL, timeout=30, params={
        "latitude": lat, "longitude": lng, "timezone": "auto", "past_days": past_days, "forecast_days": 1,
        "hourly": ",".join(API.values())})
    r.raise_for_status()
    h = r.json().get("hourly") or {}
    out = []
    for i, t in enumerate(h.get("time") or []):
        vals = {k: (h.get(api) or [None] * (i + 1))[i] for k, api in API.items()}
        if vals["wind_kmh"] is None and vals["mixing_height_m"] is None:
            continue
        out.append((datetime.fromisoformat(t), vals))
    return out


def _path(data_dir, month):
    return os.path.join(data_dir, f"weather_{month}.csv")


def read(data_dir, months):
    rows = []
    for m in months:
        try:
            with open(_path(data_dir, m), encoding="utf-8-sig", errors="replace", newline="") as f:
                rows += list(csv.DictReader(f))
        except OSError:
            pass
    return rows


def store(data_dir, items, now=None):
    """Upsert by hour (a later fetch replaces earlier values). Future hours are skipped."""
    now = now or datetime.now()
    by_month = {}
    for t, v in items:
        if t > now:
            continue
        by_month.setdefault(t.strftime("%Y-%m"), []).append((t, v))
    for m, its in by_month.items():
        existing = {r["time_local"]: r for r in read(data_dir, [m])}
        for t, v in its:
            key = t.strftime("%Y-%m-%dT%H:%M")
            existing[key] = {"time_local": key, **{k: ("" if v.get(k) is None else v[k]) for k in API}}
        tmp = _path(data_dir, m) + ".tmp"
        with open(tmp, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
            w.writeheader()
            w.writerows(sorted(existing.values(), key=lambda r: r["time_local"]))
        os.replace(tmp, _path(data_dir, m))


def update(config, data_dir=DATA_DIR, now=None, session=requests):
    """Called after each recorder sync; fetches at most once an hour (first run: last 92 days)."""
    now = now or datetime.now()
    loc = config.get("location") or {}
    if loc.get("lat") is None:
        return None
    sp = os.path.join(data_dir, "weather_state.json")
    try:
        with open(sp, encoding="utf-8") as f:
            st = json.load(f)
    except (OSError, ValueError):
        st = {}
    last = st.get("last_fetch")
    if last and now - datetime.fromisoformat(last) < timedelta(minutes=55):
        return None
    items = fetch(loc["lat"], loc["lng"], past_days=2 if last else 92, session=session)
    store(data_dir, items, now)
    st["last_fetch"] = now.isoformat(timespec="seconds")
    with open(sp, "w", encoding="utf-8") as f:
        json.dump(st, f)
    return len(items)


ARROWS = "↓↙←↖↑↗→↘"          # index by the direction the wind blows FROM, in 45° steps


def arrow(from_deg):
    """Arrow pointing where the wind is GOING (from 0° = north → arrow points down/south)."""
    try:
        return ARROWS[int(((float(from_deg) % 360) + 22.5) // 45) % 8]
    except (TypeError, ValueError):
        return ""


def compass(from_deg):
    try:
        return ["N", "NE", "E", "SE", "S", "SW", "W", "NW"][int(((float(from_deg) % 360) + 22.5) // 45) % 8]
    except (TypeError, ValueError):
        return ""
