"""
surge.py — "why did the air just get worse?"  Rule-based, no LLM.

When a spike alert fires (or you ask from the tray menu), this gathers evidence and lists the
likely contributors, strongest first. Every reason states the number it rests on, so you can
judge it yourself. Evidence used:

  1. Calendar   — festival nights (Diwali incl. the night before and two residual days, Dussehra,
                  Gurpurab, Holika Dahan, Lohri, New Year's Eve) + anything you add to
                  data/events.csv (date,name — e.g. a cricket win, a big wedding next door).
  2. Local vs city-wide — your area's live AQI change vs the median of all monitors in your city over
                  the same ~3 h. City-wide rise → weather / festival / regional smoke.
                  Only your area → a nearby source (fire, garbage burning, traffic, construction).
                  City-wide medians are recorded every sync into data/city_live_YYYY-MM.csv. The city
                  is a box around config.json → location (±0.25° lat, ±0.28° lng, about ±28 km), or
                  your own "city_box": [lat1, lng1, lat2, lng2].
  3. Weather (Open-Meteo, free, no key) — wind speed and its drop, mixing-layer height
                  (low = pollution trapped near the ground), humidity/fog, rain, wind direction.
  4. Particle mix — PM2.5 as % of PM10 in your area: high = combustion (smoke, firecrackers,
                  stubble, vehicles); low = dust (construction, road dust, dust storm).
  5. Stubble season (Oct–Nov) + north-westerly wind; with a free NASA FIRMS key
                  (config.json "firms_map_key") also the fire count in Punjab + Haryana.

What it cannot know: one-off local events (a factory/landfill fire, a wedding's fireworks) unless
they show up as "local only" — or you add them to data/events.csv. That is the only thing an LLM
with web search could add, and it would be guessing from news headlines.

Each explanation is also written to data/surge_reasons.csv with the raw evidence.
"""
import csv
import json
import os
import statistics
from datetime import date, datetime, timedelta

import requests

from recorder import DATA_DIR

STUBBLE_BOX = "73.8,28.4,77.6,32.5"                # west,south,east,north: Punjab + Haryana
# Crop-residue smoke from Punjab/Haryana matters for the north Indian plains (Punjab, Haryana,
# Delhi-NCR, western/central UP, north Rajasthan). Elsewhere the check is skipped.
NORTH_PLAINS = (25.0, 73.5, 32.5, 84.5)            # lat1, lng1, lat2, lng2


def city_name(config):
    return ((config or {}).get("location") or {}).get("city") or "your city"


def city_box(config):
    """[lat1, lng1, lat2, lng2] of the user's city: config "city_box", else a box around location."""
    cfg = config or {}
    if cfg.get("city_box") and len(cfg["city_box"]) == 4:
        return [float(v) for v in cfg["city_box"]]
    loc = cfg.get("location") or {}
    if loc.get("lat") is None or loc.get("lng") is None:
        return None
    lat, lng = float(loc["lat"]), float(loc["lng"])
    return [lat - 0.25, lng - 0.28, lat + 0.25, lng + 0.28]


def in_north_plains(config):
    cfg = config or {}
    if "stubble_check" in cfg:                      # explicit override in config.json
        return bool(cfg["stubble_check"])
    loc = cfg.get("location") or {}
    try:
        lat, lng = float(loc["lat"]), float(loc["lng"])
    except (KeyError, TypeError, ValueError):
        return False
    a, b, c, d = NORTH_PLAINS
    return a <= lat <= c and b <= lng <= d

# Built-in festival table (verified dates). Years beyond it come from the `holidays` package if
# installed, and from data/events.csv.
FESTIVALS = {
    "2025-10-02": "Dussehra", "2025-10-20": "Diwali", "2025-11-05": "Gurpurab (Guru Nanak Jayanti)",
    "2026-03-03": "Holika Dahan", "2026-10-20": "Dussehra", "2026-11-08": "Diwali",
    "2026-11-24": "Gurpurab (Guru Nanak Jayanti)",
    "2027-03-21": "Holika Dahan", "2027-10-09": "Dussehra", "2027-10-29": "Diwali",
    "2027-11-14": "Gurpurab (Guru Nanak Jayanti)",
}
CITY_FIELDS = ["polled_at_local", "stations", "median_aqi", "p90_aqi", "max_aqi", "max_station", "aqi_scale"]
REASON_FIELDS = ["time", "trigger", "summary", "festival", "home_rise_aqi", "city_rise_aqi", "city_now_aqi",
                 "wind_now_kmh", "wind_before_kmh", "wind_dir_deg", "mixing_height_m", "humidity_pct",
                 "rain_mm_3h", "pm25_share_of_pm10_pct", "stubble_fires_24h"]


# ── calendar ────────────────────────────────────────────────────────────────
def _festival_days(year):
    days = {d: n for d, n in FESTIVALS.items() if d.startswith(str(year))}
    try:
        import holidays                                # optional, extends to future years
        for d, n in holidays.India(years=year).items():
            k = d.isoformat()
            if k in days:
                continue
            if "Diwali" in n or "Deepavali" in n:
                days[k] = "Diwali"
            elif "Dussehra" in n or "Vijaya" in n:
                days[k] = "Dussehra"
            elif "Guru Nanak" in n:
                days[k] = "Gurpurab (Guru Nanak Jayanti)"
            elif n.startswith("Holi"):
                days[(d - timedelta(days=1)).isoformat()] = "Holika Dahan"
    except Exception:
        pass
    days.setdefault(f"{year}-01-13", "Lohri (bonfires)")
    days.setdefault(f"{year}-12-31", "New Year's Eve")
    return days


def _user_events(data_dir):
    out = {}
    try:
        with open(os.path.join(data_dir, "events.csv"), encoding="utf-8-sig", errors="replace") as f:
            for r in csv.DictReader(f):
                d, n = (r.get("date") or "").strip(), (r.get("name") or "").strip()
                if d and n and not d.startswith("#"):
                    out[d] = n
    except OSError:
        pass
    return out


def festival_windows(years, data_dir=DATA_DIR):
    """[(start, end, name)] — hours whose air a festival can dominate. Diwali: 17:00 the evening
    before (Choti Diwali) to the end of the 2nd day after. Others and your own events.csv entries:
    17:00 that day to 12:00 the next."""
    out = []
    user = _user_events(data_dir)
    for y in years:
        cal = {**_festival_days(y), **{k: v for k, v in user.items() if k.startswith(str(y))}}
        for d, n in cal.items():
            try:
                day = datetime.fromisoformat(d).astimezone()
            except ValueError:
                continue
            if "Diwali" in n:
                out.append((day - timedelta(hours=7), day + timedelta(days=3), n))
            else:
                out.append((day + timedelta(hours=17), day + timedelta(hours=36), n))
    return out


def festival_context(now, data_dir=DATA_DIR):
    """Festival/event whose smoke could be in the air at `now` (local time)."""
    found = []
    for back in range(0, 3):
        day = (now - timedelta(days=back)).date()
        cal = {**_festival_days(day.year), **_user_events(data_dir)}
        n = cal.get(day.isoformat())
        if not n:
            continue
        night = datetime.combine(day, datetime.min.time()).astimezone() + timedelta(hours=17)
        if back == 0 and now < night and "Diwali" not in n:
            continue                                   # festival day, but its evening hasn't started
        if back == 0:
            found.append(f"{n} {'night' if now >= night else 'day'}")
        elif back == 1 and now < night + timedelta(days=1):   # until 17:00 next day: last night's smoke
            found.append(f"{n} night (smoke from last night)")
        elif "Diwali" in n:
            found.append(f"{n} {back} day(s) ago — firecracker smoke can linger 2–3 days in calm air")
    tomorrow = (now + timedelta(days=1)).date()
    if _festival_days(tomorrow.year).get(tomorrow.isoformat()) == "Diwali" and now.hour >= 17:
        found.append("Choti Diwali night: eve of Diwali, firecrackers start")
    return found


# ── city-wide snapshot (called every recorder sync) ────────────────────────
def record_city(waqi, data_dir=DATA_DIR, now=None, min_gap_min=14, config=None, box=None):
    box = box or city_box(config)
    if not box:
        return None
    now = now or datetime.now().astimezone()
    path = os.path.join(data_dir, f"city_live_{now:%Y-%m}.csv")
    rows = _read(path)
    if rows:
        try:
            if now - datetime.fromisoformat(rows[-1]["polled_at_local"]) < timedelta(minutes=min_gap_min):
                return None
        except ValueError:
            pass
    pts = []
    for s in waqi.stations_in_bounds(box) or []:
        try:
            a = float(s.get("aqi"))
        except (TypeError, ValueError):
            continue                                   # "-" = station offline
        t = ((s.get("station") or {}).get("time") or "")
        try:
            if t and now - datetime.fromisoformat(t.replace("Z", "+00:00")).astimezone() > timedelta(hours=3):
                continue                               # stale station
        except ValueError:
            pass
        pts.append((a, (s.get("station") or {}).get("name", "")))
    if len(pts) < 5:
        return None
    vals = sorted(a for a, _ in pts)
    mx = max(pts)
    row = {"polled_at_local": now.isoformat(timespec="seconds"), "stations": len(vals),
           "median_aqi": round(statistics.median(vals)), "p90_aqi": round(vals[int(0.9 * (len(vals) - 1))]),
           "max_aqi": round(mx[0]), "max_station": mx[1].split(",")[0], "aqi_scale": "us2024"}
    new = not os.path.exists(path)
    if not new:                                   # older file without the aqi_scale column: upgrade once
        with open(path, encoding="utf-8-sig", errors="replace", newline="") as f:
            header = next(csv.reader(f), [])
        if "aqi_scale" not in header:
            old = _read(path)
            with open(path, "w", encoding="utf-8-sig", newline="") as f:
                w = csv.DictWriter(f, fieldnames=CITY_FIELDS, extrasaction="ignore")
                w.writeheader()
                w.writerows(old)
    with open(path, "a", encoding="utf-8-sig" if new else "utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CITY_FIELDS)
        if new:
            w.writeheader()
        w.writerow(row)
    return row


def _read(path):
    try:
        with open(path, encoding="utf-8-sig", errors="replace", newline="") as f:
            return list(csv.DictReader(f))
    except OSError:
        return []


def city_series(data_dir, now, hours=8):
    rows = []
    for m in {now.strftime("%Y-%m"), (now - timedelta(hours=hours)).strftime("%Y-%m")}:
        rows += _read(os.path.join(data_dir, f"city_live_{m}.csv"))
    out = []
    for r in rows:
        try:
            t = datetime.fromisoformat(r["polled_at_local"])
            if now - timedelta(hours=hours) <= t <= now + timedelta(minutes=5):
                from aqi_math import live_row_aqi
                v = live_row_aqi(r, "median_aqi")       # rows before the switch hold WAQI's old scale
                if v is not None:
                    out.append((t, v))
        except (KeyError, ValueError):
            continue
    return sorted(out)


def _change(series, now, window_h=3):
    """(value now, value ~window_h ago, change) from a [(time, value)] series, or None."""
    if not series:
        return None
    t1, v1 = series[-1]
    if now - t1 > timedelta(hours=2):
        return None
    target = t1 - timedelta(hours=window_h)
    cands = [(abs((t - target).total_seconds()), t, v) for t, v in series if t <= t1 - timedelta(hours=1)]
    if not cands:
        return None
    _, t0, v0 = min(cands)
    if abs((t0 - target).total_seconds()) > 2 * 3600:
        return None
    return v1, v0, v1 - v0


def home_live_series(data_dir, now, hours=8):
    rows = []
    for m in {now.strftime("%Y-%m"), (now - timedelta(hours=hours)).strftime("%Y-%m")}:
        rows += [r for r in _read(os.path.join(data_dir, f"live_{m}.csv")) if r.get("target") == "home"]
    uniq = {}
    for r in rows:
        try:
            t = datetime.fromisoformat(r["station_time_local"]).astimezone()
            from aqi_math import live_row_aqi
            v = live_row_aqi(r)
            if v is None:
                continue
            uniq[t] = v
        except (KeyError, ValueError, TypeError):
            continue
    return sorted((t, v) for t, v in uniq.items() if now - timedelta(hours=hours) <= t <= now)


def latest_mix(data_dir, now):
    rows = []
    for m in {now.strftime("%Y-%m"), (now - timedelta(days=1)).strftime("%Y-%m")}:
        rows += [r for r in _read(os.path.join(data_dir, f"readings_{m}.csv")) if r.get("target") == "home"]
    rows.sort(key=lambda r: r.get("hour_start_utc", ""))
    for r in reversed(rows[-4:]):
        try:
            p25, p10 = float(r["pm25_ugm3"]), float(r["pm10_ugm3"])
            if p10 > 0:
                return p25, p10
        except (KeyError, ValueError, TypeError):
            continue
    return None


# ── weather / fires ────────────────────────────────────────────────────────
def fetch_weather(lat, lng, session=requests):
    r = session.get("https://api.open-meteo.com/v1/forecast", timeout=15, params={
        "latitude": lat, "longitude": lng, "past_days": 1, "forecast_days": 1, "timezone": "auto",
        "hourly": "wind_speed_10m,wind_direction_10m,boundary_layer_height,temperature_2m,"
                  "relative_humidity_2m,precipitation"})
    r.raise_for_status()
    return r.json().get("hourly") or {}


def weather_summary(hourly, now):
    times = hourly.get("time") or []
    idx = [i for i, t in enumerate(times) if datetime.fromisoformat(t) <= now.replace(tzinfo=None)]
    if len(idx) < 7:
        return {}
    i = idx[-1]

    def avg(key, a, b):
        v = [hourly.get(key, [None] * len(times))[j] for j in range(max(0, a), b + 1)]
        v = [x for x in v if x is not None]
        return sum(v) / len(v) if v else None

    def val(key):
        v = hourly.get(key)
        return v[i] if v and i < len(v) else None
    rain = [x for x in (hourly.get("precipitation") or [])[max(0, i - 2): i + 1] if x is not None]
    return {"wind_now": avg("wind_speed_10m", i - 2, i), "wind_before": avg("wind_speed_10m", i - 8, i - 5),
            "wind_dir": val("wind_direction_10m"), "blh": val("boundary_layer_height"),
            "blh_before": avg("boundary_layer_height", i - 8, i - 5),
            "rh": val("relative_humidity_2m"), "temp": val("temperature_2m"), "rain3h": sum(rain) if rain else None}


def stubble_fires(map_key, session=requests):
    r = session.get(f"https://firms.modaps.eosdis.nasa.gov/api/area/csv/{map_key}/VIIRS_SNPP_NRT/{STUBBLE_BOX}/1",
                    timeout=20)
    r.raise_for_status()
    lines = [ln for ln in r.text.strip().splitlines() if ln.strip()]
    if not lines or not lines[0].lower().startswith("latitude"):
        raise ValueError(r.text[:80])                 # invalid key / error message
    return len(lines) - 1


# ── the explanation ────────────────────────────────────────────────────────
def explain(config, now=None, data_dir=DATA_DIR, trigger="manual", weather=None, fires=None, session=requests):
    """Returns {"summary": one line, "reasons": [(strength, text)], "unknown": [..], "evidence": {...}}."""
    now = now or datetime.now().astimezone()
    loc = config.get("location") or {}
    cname = city_name(config)
    reasons, unknown, ev = [], [], {"time": now.isoformat(timespec="seconds"), "trigger": trigger}

    fest = festival_context(now, data_dir)
    if fest:
        reasons.append((5, "; ".join(fest)))
        ev["festival"] = "; ".join(fest)

    home = _change(home_live_series(data_dir, now), now)
    city = _change(city_series(data_dir, now), now)
    if home:
        ev["home_rise_aqi"] = round(home[2])
    if city:
        ev["city_rise_aqi"], ev["city_now_aqi"] = round(city[2]), round(city[0])
    if home and city:
        h, c = home[2], city[2]
        if c >= 40 and (h <= 0 or c >= 0.5 * h):
            reasons.append((4, f"city-wide: {cname} median AQI {city[1]:.0f} → {city[0]:.0f} (+{c:.0f}) while your area "
                               f"changed {h:+.0f} — weather, festival or regional smoke, not a source on your street"))
        elif h >= 40 and c < 0.4 * h:
            reasons.append((4, f"local to your area: your AQI {home[1]:.0f} → {home[0]:.0f} (+{h:.0f}) while {cname}'s "
                               f"median moved only {c:+.0f} — a nearby source (burning, fire, traffic jam, "
                               "construction, fireworks at an event)"))
    elif not city:
        unknown.append(f"no {cname}-wide comparison yet (recorded every sync from now on)")

    wx = weather
    if wx is None and loc.get("lat") is not None:
        try:
            wx = weather_summary(fetch_weather(loc["lat"], loc["lng"], session), now)
        except Exception as e:
            unknown.append(f"weather unavailable ({str(e)[:60]})")
            wx = {}
    wx = wx or {}
    w, wb, blh, blhb = wx.get("wind_now"), wx.get("wind_before"), wx.get("blh"), wx.get("blh_before")
    stagnant = (w is not None and w < 5) or (blh is not None and blh < 300)
    ev.update({"wind_now_kmh": _r(w), "wind_before_kmh": _r(wb), "wind_dir_deg": _r(wx.get("wind_dir")),
               "mixing_height_m": _r(blh), "humidity_pct": _r(wx.get("rh")), "rain_mm_3h": _r(wx.get("rain3h"))})
    if w is not None:
        if wb is not None and wb - w >= 4 and w <= 0.6 * wb:
            reasons.append((3, f"wind dropped from {wb:.0f} to {w:.0f} km/h — pollution stops being carried away"))
        elif w < 5:
            reasons.append((3, f"very calm air (wind {w:.0f} km/h) — nothing is dispersing the smoke"))
        elif w < 8:
            reasons.append((2, f"light wind ({w:.0f} km/h)"))
    if blh is not None:
        if blh < 300:
            dropped = f", down from {blhb:.0f} m" if blhb and blhb > 1.5 * blh else ""
            reasons.append((3, f"shallow mixing layer ({blh:.0f} m{dropped}) — an inversion lid traps pollution near "
                               "the ground; normal on Oct–Jan nights"))
        elif blhb and blh < 0.5 * blhb:
            reasons.append((2, f"mixing layer collapsing ({blhb:.0f} → {blh:.0f} m)"))
    if wx.get("rh") is not None and wx["rh"] >= 85:
        reasons.append((2, f"very humid ({wx['rh']:.0f}%) — fog/haze; particles swell and secondary pollution "
                           "forms (and low-cost sensors read high)"))
    if wx.get("rain3h"):
        reasons.append((1, f"rain in the last 3 h ({wx['rain3h']:.1f} mm) should be washing particles out — "
                           "if it's still rising, the source is strong and close"))

    mix = latest_mix(data_dir, now)
    if mix:
        share = 100 * mix[0] / mix[1]
        ev["pm25_share_of_pm10_pct"] = round(share)
        if share >= 60:
            reasons.append((2, f"mostly fine particles (PM2.5 is {share:.0f}% of PM10) — combustion: smoke, "
                               "firecrackers, crop/garbage burning, vehicles"))
        elif share <= 35:
            reasons.append((2, f"mostly coarse dust (PM2.5 only {share:.0f}% of PM10) — dust: construction, "
                               "road dust, dust-laden wind"))

    stubble_season = ((now.month == 10 and now.day >= 1) or (now.month == 11)) and in_north_plains(config)
    d = wx.get("wind_dir")
    if stubble_season:
        if fires is None and config.get("firms_map_key"):
            try:
                fires = stubble_fires(config["firms_map_key"], session)
            except Exception as e:
                unknown.append(f"fire count unavailable ({str(e)[:50]})")
        ev["stubble_fires_24h"] = fires if fires is not None else ""
        nw = d is not None and 280 <= d <= 345 and (w or 0) >= 4
        if fires is not None and fires >= 300 and nw:
            reasons.append((3, f"{fires} fire detections in Punjab + Haryana in 24 h and a north-westerly wind — "
                               f"stubble smoke likely reaching {cname}"))
        elif nw:
            reasons.append((2, "stubble-burning season with a north-westerly wind (from Punjab/Haryana)"
                               + (f"; {fires} fires detected in 24 h" if fires is not None else
                                  " — add a free NASA FIRMS key to confirm fire counts")))
        elif fires is not None and fires >= 300:
            reasons.append((1, f"{fires} farm fires in Punjab + Haryana, but the wind isn't from there right now"))

    if 18 <= now.hour or now.hour < 3:
        if stagnant:
            reasons.append((1, "evening build-up: the usual nightly peak (≈ 21:00–02:00) as the ground cools"))

    reasons.sort(key=lambda x: -x[0])
    if not reasons:
        summary = "No clear cause in the data — possibly a local one-off (fire, burning, event)."
    else:
        summary = "Likely: " + " · ".join(t.split(" — ")[0] for _, t in reasons[:3])
    ev["summary"] = summary
    _log(data_dir, ev)
    return {"summary": summary, "reasons": reasons, "unknown": unknown, "evidence": ev}


def _r(x):
    return "" if x is None else round(x, 1)


def _log(data_dir, ev):
    path = os.path.join(data_dir, "surge_reasons.csv")
    new = not os.path.exists(path)
    try:
        with open(path, "a", encoding="utf-8-sig" if new else "utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=REASON_FIELDS, extrasaction="ignore")
            if new:
                w.writeheader()
            w.writerow(ev)
    except PermissionError:
        pass


def ensure_events_template(data_dir=DATA_DIR):
    path = os.path.join(data_dir, "events.csv")
    if not os.path.exists(path):
        with open(path, "w", encoding="utf-8-sig", newline="") as f:
            f.write("date,name\n")
