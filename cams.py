"""
cams.py — an independent, satellite-informed estimate of PM2.5 at your location.

Source: Copernicus Atmosphere Monitoring Service (CAMS) global forecast/analysis, served free by
Open-Meteo's Air Quality API (no key for non-commercial use). CAMS is a computer model of the
atmosphere that is corrected with NASA MODIS satellite aerosol data; its global system does NOT
use ground monitors. So it keeps working — and can't be skewed — if government monitors go offline.

Limits (read before trusting a number):
  • grid ≈ 0.4° (~45 km): one value covers most of Delhi, so it can't see your street or a
    local fire;
  • it is a model: absolute levels in Delhi winter are often far from the monitors (the app shows
    the ratio between them). Use it for the TREND and day-to-day ups and downs, not the exact value;
  • stored values are hourly (CAMS is 3-hourly, Open-Meteo interpolates); history from Aug 2022.

Stored in data/model_YYYY-MM.csv: time_local, pm25_model, pm10_model, aod, dust.
"""
import csv
import json
import os
from datetime import datetime, timedelta

import requests

from recorder import DATA_DIR

URL = "https://air-quality-api.open-meteo.com/v1/air-quality"
FIELDS = ["time_local", "pm25_model", "pm10_model", "aod", "dust"]
SOURCE = "CAMS global (~45 km) via Open-Meteo"


def fetch(lat, lng, past_days=None, start=None, end=None, session=requests):
    """Hourly model values, local time. Returns [(datetime_local_naive, {field: value})]."""
    params = {"latitude": lat, "longitude": lng, "timezone": "auto",          # India → CAMS global (auto)
              "hourly": "pm2_5,pm10,aerosol_optical_depth,dust"}
    if start:
        params.update(start_date=start, end_date=end)
    else:
        params.update(past_days=past_days or 2, forecast_days=1)
    r = session.get(URL, params=params, timeout=30)
    r.raise_for_status()
    h = r.json().get("hourly") or {}
    out = []
    for i, t in enumerate(h.get("time") or []):
        vals = {"pm25_model": h.get("pm2_5", [None] * (i + 1))[i], "pm10_model": h.get("pm10", [None] * (i + 1))[i],
                "aod": h.get("aerosol_optical_depth", [None] * (i + 1))[i], "dust": h.get("dust", [None] * (i + 1))[i]}
        if vals["pm25_model"] is None:
            continue
        out.append((datetime.fromisoformat(t), vals))
    return out


def _path(data_dir, month):
    return os.path.join(data_dir, f"model_{month}.csv")


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
    """Upsert by hour (later fetches replace earlier forecast values). Future hours are skipped."""
    now = now or datetime.now()
    by_month = {}
    for t, v in items:
        if t > now:
            continue
        by_month.setdefault(t.strftime("%Y-%m"), []).append((t, v))
    for m, its in by_month.items():
        existing = {r["time_local"]: r for r in read(data_dir, [m])}
        for t, v in its:
            existing[t.strftime("%Y-%m-%dT%H:%M")] = {"time_local": t.strftime("%Y-%m-%dT%H:%M"),
                                                      **{k: ("" if v[k] is None else v[k]) for k in v}}
        tmp = _path(data_dir, m) + ".tmp"
        with open(tmp, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS)
            w.writeheader()
            w.writerows(sorted(existing.values(), key=lambda r: r["time_local"]))
        os.replace(tmp, _path(data_dir, m))


def update(config, data_dir=DATA_DIR, now=None, session=requests):
    """Called after each recorder sync; fetches at most once an hour (first run: last 92 days)."""
    now = now or datetime.now()
    loc = config.get("location") or {}
    if loc.get("lat") is None:
        return None
    sp = os.path.join(data_dir, "model_state.json")
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
