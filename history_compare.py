"""
history_compare.py — your area's PM2.5 in the same weeks of past years (from OpenAQ's archive).

Uses the same idea as the app's "Your area": the government monitors within your area radius
(config.json → recorder.home_area), median across monitors each hour, then per day (India time).
For every monitor it uses ALL its PM2.5 sensors, including retired ones, because OpenAQ often
moved a station to a new sensor id at some point.

Window: 15 Oct – 30 Nov of this year and the two before (this year: up to now).
Also fetches the satellite-model estimate (Copernicus CAMS, independent of ground monitors) for the
same days, so the year-to-year trend can be checked against a source the monitors can't affect.

Output:
  data/history_compare.csv   one row per year per day (mean, median, worst/cleanest hour, hours per band)
  printed summary            coverage per monitor per year + a same-dates comparison

Run (double-click "Compare past Novembers.bat", or):  .venv\\Scripts\\python history_compare.py
Takes ~2–4 minutes (about 100 requests, paced to stay under OpenAQ's rate limit).
"""
import csv
import json
import os
import statistics
import sys
import time
from datetime import datetime, timedelta, timezone

from openaq import OpenAQClient
from recorder import DATA_DIR, _g, _valid_conc, parse_dt, haversine

IST = timezone(timedelta(hours=5, minutes=30))
BANDS = [(9.0, "good"), (35.4, "moderate"), (55.4, "sensitive"), (125.4, "unhealthy"),
         (225.4, "very_unhealthy"), (1e9, "hazardous")]
FIELDS = ["year", "date", "md", "hours", "monitors_median", "mean", "median", "max", "max_hour", "min",
          "min_hour", "day_mean_06_18", "night_mean_18_06"] + [f"h_{n}" for _, n in BANDS] + ["model_mean"]


def band(v):
    for hi, n in BANDS:
        if v <= hi:
            return n


def get(client, sid, frm, to, tries=4):
    for i in range(tries):
        try:
            time.sleep(0.5)                                   # pacing: OpenAQ allows ~60 req/min
            return client.sensor_hours(sid, frm, to, max_pages=4)
        except Exception as e:
            msg = str(e)
            if "429" in msg or "Too Many" in msg:
                time.sleep(20 * (i + 1))
                continue
            if i == tries - 1:
                print(f"    sensor {sid}: {msg[:90]}")
            time.sleep(2)
    return []


def monitors(client, cfg):
    loc = cfg.get("location") or {}
    area = (cfg.get("recorder") or {}).get("home_area") or {}
    lat, lng = float(loc["lat"]), float(loc["lng"])
    radius = float(area.get("radius_km", 5))
    exclude = {int(i) for i in area.get("exclude_location_ids", [])}
    out = []
    for l in client.locations_near_raw(lat, lng, radius_m=max(radius, 8) * 1000, limit=1000):
        if _g(l, "isMobile") or _g(l, "isMonitor") is False or _g(l, "id") in exclude:
            continue
        c = _g(l, "coordinates", default={}) or {}
        d = haversine(lat, lng, float(c.get("latitude")), float(c.get("longitude")))
        if d > radius:
            continue
        sids = [_g(s, "id") for s in _g(l, "sensors", default=[]) or []
                if str(_g(_g(s, "parameter", default={}) or {}, "name", default="")).lower().replace(".", "") == "pm25"]
        if sids:
            out.append({"id": _g(l, "id"), "name": _g(l, "name"), "km": round(d, 1), "sensors": sids})
    return sorted(out, key=lambda m: m["km"])


def year_hours(client, mons, year, now):
    frm = datetime(year, 10, 15, tzinfo=IST)
    to = min(datetime(year, 12, 1, tzinfo=IST), now)
    f, t = frm.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), to.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    per_mon, coverage = {}, {}
    for m in mons:
        hours = {}
        for sid in m["sensors"]:
            for r in get(client, sid, f, t):
                start = (_g(_g(r, "period", default={}) or {}, "datetimeFrom", default={}) or {})
                utc = parse_dt(_g(start, "utc"))
                v = _valid_conc(_g(r, "value"))
                if utc is None or v is None:
                    continue
                hours.setdefault(utc, []).append(v)
        per_mon[m["name"]] = {k: statistics.mean(v) for k, v in hours.items()}
        coverage[m["name"]] = len(per_mon[m["name"]])
    allh = sorted({h for d in per_mon.values() for h in d})
    area = {}
    for h in allh:
        vals = [d[h] for d in per_mon.values() if h in d]
        if len(vals) >= 2 or len(per_mon) == 1:
            area[h] = (statistics.median(vals), len(vals))
    return area, coverage


def daily(area, year):
    days = {}
    for h, (v, n) in area.items():
        loc = h.astimezone(IST)
        days.setdefault(loc.date(), []).append((loc.hour, v, n))
    rows = []
    for d in sorted(days):
        hv = days[d]
        vs = [v for _, v, _ in hv]
        mx, mn = max(hv, key=lambda x: x[1]), min(hv, key=lambda x: x[1])
        dayv = [v for h, v, _ in hv if 6 <= h < 18]
        nightv = [v for h, v, _ in hv if h < 6 or h >= 18]
        row = {"year": year, "date": d.isoformat(), "md": d.strftime("%m-%d"), "hours": len(vs),
               "monitors_median": statistics.median(n for _, _, n in hv),
               "mean": round(statistics.mean(vs), 1), "median": round(statistics.median(vs), 1),
               "max": round(mx[1]), "max_hour": f"{mx[0]:02d}:00", "min": round(mn[1]), "min_hour": f"{mn[0]:02d}:00",
               "day_mean_06_18": round(statistics.mean(dayv), 1) if dayv else "",
               "night_mean_18_06": round(statistics.mean(nightv), 1) if nightv else ""}
        for _, name in BANDS:
            row[f"h_{name}"] = sum(1 for v in vs if band(v) == name)
        rows.append(row)
    return rows


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "config.json"), encoding="utf-8") as f:
        cfg = json.load(f)
    client = OpenAQClient(cfg.get("openaq_key"))
    now = datetime.now(IST)
    print("Finding your area's monitors (incl. retired sensors)…")
    mons = monitors(client, cfg)
    for m in mons:
        print(f"  {m['name']} ({m['km']} km) — PM2.5 sensors {m['sensors']}")
    if not mons:
        sys.exit("No monitors found — check config.json location / home_area.")
    all_rows = []
    for y in (now.year - 2, now.year - 1, now.year):
        print(f"\n{y}: fetching 15 Oct – 30 Nov…")
        area, cov = year_hours(client, mons, y, now)
        for name, n in cov.items():
            print(f"  {name}: {n} hours")
        rows = daily(area, y)
        print(f"  → {len(rows)} days with an area value")
        # independent check: satellite-model (CAMS) daily means for the same days
        try:
            import cams
            loc = cfg.get("location") or {}
            end = min(datetime(y, 11, 30, tzinfo=IST), now).strftime("%Y-%m-%d")
            mod = cams.fetch(loc["lat"], loc["lng"], start=f"{y}-10-15", end=end)
            by = {}
            for t, v in mod:
                by.setdefault(t.date().isoformat(), []).append(v["pm25_model"])
            for r in rows:
                if by.get(r["date"]):
                    r["model_mean"] = round(statistics.mean(by[r["date"]]), 1)
            print(f"  satellite model: {len(by)} days")
        except Exception as e:
            print(f"  satellite model unavailable: {str(e)[:80]}")
        all_rows += rows
    out = os.path.join(DATA_DIR, "history_compare.csv")
    with open(out, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(all_rows)

    # same-dates comparison: 1 Nov → today's date
    upto = now.strftime("%m-%d")
    print(f"\nSame dates, 1 Nov – {now:%d %b} (daily mean PM2.5, µg/m³):")
    for y in (now.year - 2, now.year - 1, now.year):
        rs = [r for r in all_rows if r["year"] == y and "11-01" <= r["md"] <= upto]
        if not rs:
            print(f"  {y}: no data")
            continue
        means = [r["mean"] for r in rs]
        worst = max(rs, key=lambda r: r["mean"])
        haz = sum(r["h_hazardous"] for r in rs)
        mods = [r["model_mean"] for r in rs if r.get("model_mean") not in (None, "")]
        print(f"  {y}: monitors average {statistics.mean(means):.0f} over {len(rs)} days · worst day {worst['date']} "
              f"({worst['mean']:.0f}) · {haz} hazardous hours"
              + (f" · satellite model average {statistics.mean(mods):.0f}" if mods else ""))
    print(f"\nSaved {out} — tell Claude it's done.")
    if os.name == "nt":
        input("\nPress Enter to close…")


if __name__ == "__main__":
    main()
