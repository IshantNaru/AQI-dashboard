"""
area_test.py — run the same accuracy tests as method_test.py, but for OTHER locations.

For each location it:
  1. finds the government reference monitors within RADIUS km (widens to MAX_RADIUS
     if fewer than 3), exactly like the app does when you change your location;
  2. downloads the last DAYS of hourly PM2.5 for those monitors from OpenAQ
     (each monitor is downloaded once and reused if several locations share it);
  3. runs leave-one-out (hide a monitor, predict it from the rest) for the three methods,
     and the fault test (one monitor reads ×0.5 / ×2 — how far does the area value move?).

Needs internet + the openaq_key in config.json (run it on your Windows machine).
Takes ~2 s per monitor because of OpenAQ's rate limit; 10 locations ≈ 2–3 minutes.

USAGE
    python area_test.py                         # the built-in list of Delhi-NCR locations
    python area_test.py --days 14 --radius 4
    python area_test.py --add "My office" 28.5355 77.2410     # add your own (repeatable)

OUTPUT
    data/area_test_report.txt    readable summary
    data/area_test_summary.csv   one row per location and method
"""
import argparse
import csv
import json
import math
import os
import statistics
import time
from datetime import datetime, timedelta, timezone

from openaq import OpenAQClient
from recorder import station_candidates, _hour_map, iso_z
from method_test import METHODS, distance_km, short_name, summarise

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")

# Spread across Delhi-NCR on purpose: dense central areas, the periphery, and two
# neighbouring cities, so the test covers places with many and with few monitors.
LOCATIONS = [
    ("Connaught Place (example home)", 28.6315, 77.2167),
    ("Connaught Place",         28.6315, 77.2167),
    ("Rohini",                  28.7360, 77.1130),
    ("Dwarka",                  28.5810, 77.0570),
    ("Saket",                   28.5245, 77.2066),
    ("Okhla",                   28.5355, 77.2700),
    ("Anand Vihar",             28.6470, 77.3160),
    ("Narela",                  28.8526, 77.0929),
    ("Noida Sector 62",         28.6270, 77.3650),
    ("Gurugram",                28.4595, 77.0266),
]


def fetch_series(oaq, cand, frm, now, cache, sleep):
    """Hourly PM2.5 {hour_bucket: value} for one monitor, trying its PM2.5 sensors newest first."""
    lid = cand["location_id"]
    if lid in cache:
        return cache[lid]
    series = {}
    for sid in cand["pm25_sensor_ids"]:
        try:
            res = oaq.sensor_hours(sid, iso_z(frm), iso_z(now))
        except Exception as e:
            print(f"    ! {cand['name']}: {e}")
            res = []
        time.sleep(sleep)
        for h, v in _hour_map(res).items():
            if v["value"] is not None:
                t = datetime.fromisoformat(h.replace("Z", "+00:00")) + timedelta(minutes=15)
                series[t.replace(minute=0, second=0, microsecond=0)] = v["value"]
        if len(series) >= 48:
            break
    cache[lid] = series
    return series


def test_location(name, lat, lng, oaq, args, cache, now):
    frm = now - timedelta(days=args.days)
    locs = oaq.locations_near_raw(lat, lng, radius_m=args.max_radius * 1000, limit=1000)
    time.sleep(args.sleep)
    pool = sorted(station_candidates(locs, lat, lng, now, monitors_only=True), key=lambda c: c["distance_km"])
    pool = [c for c in pool if c["location_id"] not in args.exclude]
    chosen = [c for c in pool if c["distance_km"] <= args.radius]
    radius = args.radius
    if len(chosen) < 3:
        chosen, radius = [c for c in pool if c["distance_km"] <= args.max_radius], args.max_radius
    out = {"name": name, "radius": radius, "monitors": chosen, "result": None}
    if len(chosen) < 3:
        return out                                    # can't hide one and still have 2 to combine
    data = {}
    for c in chosen:
        s = fetch_series(oaq, c, frm, now, cache, args.sleep)
        if len(s) >= 48:
            data[c["location_id"]] = (c, s)
    if len(data) < 3:
        return out

    # leave-one-out, hour by hour
    hours = sorted(set().union(*[set(s) for _, s in data.values()]))
    errs = {m: [] for m in METHODS}
    rel = {m: [] for m in METHODS}
    shifts = {f: {m: [] for m in METHODS} for f in (0.5, 2.0)}
    used_hours = 0
    for h in hours:
        vals = {lid: s[h] for lid, (_, s) in data.items() if h in s}
        if len(vals) < 3:
            continue
        used_hours += 1
        for hid, actual in vals.items():
            hc = data[hid][0]
            others = [(k, v) for k, v in vals.items() if k != hid]
            d = [distance_km(hc["lat"], hc["lng"], data[k][0]["lat"], data[k][0]["lng"]) for k, _ in others]
            for m, fn in METHODS.items():
                e = fn([v for _, v in others], d, args.power) - actual
                errs[m].append(e)
                if actual > 5:
                    rel[m].append(abs(e) / actual * 100)
        ids = list(vals)
        dh = [distance_km(lat, lng, data[k][0]["lat"], data[k][0]["lng"]) for k in ids]
        for m, fn in METHODS.items():
            base = fn([vals[k] for k in ids], dh, args.power)
            if base <= 0:
                continue
            for f in shifts:
                for bad in ids:
                    v2 = [vals[k] * (f if k == bad else 1) for k in ids]
                    shifts[f][m].append(abs(fn(v2, dh, args.power) - base) / base * 100)
    if not used_hours:
        return out
    level = statistics.fmean(v for _, s in data.values() for v in s.values())
    out["result"] = {
        "hours": used_hours, "n_used": len(data), "level": level,
        "loo": {m: summarise(errs[m]) for m in METHODS},
        "rel": {m: statistics.median(rel[m]) if rel[m] else None for m in METHODS},
        "shift": {f: {m: statistics.fmean(v) if v else None for m, v in sh.items()} for f, sh in shifts.items()},
    }
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--radius", type=float, default=5.0)
    ap.add_argument("--max-radius", type=float, default=8.0)
    ap.add_argument("--power", type=float, default=2.0, help="IDW power")
    ap.add_argument("--sleep", type=float, default=1.1, help="seconds between OpenAQ requests")
    ap.add_argument("--add", nargs=3, action="append", metavar=("NAME", "LAT", "LNG"), default=[])
    ap.add_argument("--only-added", action="store_true", help="skip the built-in list")
    a = ap.parse_args()

    with open(os.path.join(HERE, "config.json"), encoding="utf-8") as f:
        cfg = json.load(f)
    a.exclude = set((cfg.get("recorder") or {}).get("home_area", {}).get("exclude_location_ids", []))
    oaq = OpenAQClient(cfg["openaq_key"])
    locs = ([] if a.only_added else LOCATIONS) + [(n, float(la), float(lo)) for n, la, lo in a.add]
    now, cache, results = datetime.now(timezone.utc), {}, []
    for name, lat, lng in locs:
        print(f"• {name} …")
        try:
            results.append(test_location(name, lat, lng, oaq, a, cache, now))
        except Exception as e:
            print(f"    ! failed: {e}")

    lines = [f"AREA METHOD TEST — {len(results)} locations, last {a.days} days, radius {a.radius:g} km "
             f"(widened to {a.max_radius:g} if < 3 monitors), IDW power {a.power:g}",
             "LOO = estimate a hidden monitor from the others (PM2.5 µg/m³). Typical % = median |error| / actual.",
             ""]
    rows = []
    for r in results:
        mons = ", ".join(f"{short_name(c['name'])} {c['distance_km']:.1f}" for c in r["monitors"])
        lines.append(f"{r['name']}  —  {len(r['monitors'])} monitors within {r['radius']:g} km: {mons or 'none'}")
        res = r["result"]
        if not res:
            lines.append("   not testable (needs ≥3 monitors with data)\n")
            continue
        lines.append(f"   {res['hours']} hours tested, {res['n_used']} monitors, average PM2.5 {res['level']:.0f}")
        best = min(METHODS, key=lambda m: res["loo"][m]["mae"])
        for m in METHODS:
            s = res["loo"][m]
            lines.append(f"   {m:18} MAE {s['mae']:6.1f}  RMSE {s['rmse']:6.1f}  bias {s['bias']:+6.1f}  "
                         f"typical {res['rel'][m]:5.1f}%  | fault ×0.5 {res['shift'][0.5][m]:5.1f}%  ×2 {res['shift'][2.0][m]:5.1f}%"
                         + ("   ← best" if m == best else ""))
            rows.append({"location": r["name"], "monitors": res["n_used"], "radius_km": r["radius"],
                         "hours": res["hours"], "avg_pm25": round(res["level"], 1), "method": m,
                         "mae": round(s["mae"], 1), "rmse": round(s["rmse"], 1), "bias": round(s["bias"], 1),
                         "typical_pct_error": round(res["rel"][m], 1),
                         "fault_half_shift_pct": round(res["shift"][0.5][m], 1),
                         "fault_double_shift_pct": round(res["shift"][2.0][m], 1)})
        lines.append("")
    if rows:
        lines.append("OVERALL (average across testable locations):")
        for m in METHODS:
            rr = [x for x in rows if x["method"] == m]
            wins = sum(1 for loc in {x["location"] for x in rows}
                       if min((x for x in rows if x["location"] == loc), key=lambda x: x["mae"])["method"] == m)
            lines.append(f"   {m:18} MAE {statistics.fmean(x['mae'] for x in rr):6.1f}   typical "
                         f"{statistics.fmean(x['typical_pct_error'] for x in rr):5.1f}%   best at {wins} location(s)")
    os.makedirs(DATA, exist_ok=True)
    with open(os.path.join(DATA, "area_test_report.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    if rows:
        with open(os.path.join(DATA, "area_test_summary.csv"), "w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0])); w.writeheader(); w.writerows(rows)
    print("\n".join(lines))


if __name__ == "__main__":
    main()
