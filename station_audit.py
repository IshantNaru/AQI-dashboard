"""
Station audit — which nearby monitor can you trust?

Doesn't trust any single monitor. For every reference monitor within N km
(default 6) of your config location it pulls 30 days of hourly PM2.5 from
OpenAQ and checks it against the *other* monitors around it:

  coverage     share of the 720 hours actually reported
  repeats      share of hours identical to the previous hour (stuck sensor)
  r            correlation with the median of its neighbours (tracks the same air?)
  ratio        typical reading / neighbour median (1.0 = in line; <1 reads low)
  ratio_peak   same, only for hours when neighbours exceed 150 µg/m³ — a monitor
               that reads in line normally but low on bad days is suspicious
               (e.g. water sprinklers around it)

Neighbours run by different agencies (CPCB, DPCC, IMD) are a cross-check on
each other. Also saves WAQI's current reading for each WAQI station nearby.

Run:  python station_audit.py [radius_km]
Output: data/station_audit.csv (+ printed table)
"""
import csv
import json
import math
import os
import statistics
import sys
import time
from datetime import datetime, timedelta, timezone

from openaq import OpenAQClient
from waqi import WAQIClient
from recorder import station_candidates, _hour_map, iso_z, parse_dt, haversine, DATA_DIR

APP_DIR = os.path.dirname(os.path.abspath(__file__))


def _bucket(hour_iso):
    """Monitors label hours at :00 or :30 UTC — put both in the same hourly bucket."""
    t = parse_dt(hour_iso) + timedelta(minutes=15)
    return t.replace(minute=0, second=0, microsecond=0)


def _pearson(xs, ys):
    if len(xs) < 24:
        return None
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    sx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    sy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if sx == 0 or sy == 0:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / (sx * sy)


def audit(cfg, oaq, waqi=None, radius_km=6.0, days=30, now=None, sleep=1.1):
    now = now or datetime.now(timezone.utc)
    lat, lng = float(cfg["location"]["lat"]), float(cfg["location"]["lng"])
    frm = now - timedelta(days=days)
    locs = oaq.locations_near_raw(lat, lng, radius_m=radius_km * 1000, limit=1000)
    cands = station_candidates(locs, lat, lng, now, monitors_only=True)

    series = {}
    for c in cands:
        for sid in c["pm25_sensor_ids"]:
            try:
                res = oaq.sensor_hours(sid, iso_z(frm), iso_z(now))
            except Exception as e:
                print(f"  skip {c['name']}: {e}")
                res = []
            if sleep:
                time.sleep(sleep)
            hm = {_bucket(h): v["value"] for h, v in _hour_map(res).items() if v["value"] is not None}
            if len(hm) >= 48:
                series[c["location_id"]] = (c, sid, hm)
                break

    total_hours = days * 24

    def _repeats(hm):
        vals = [hm[k] for k in sorted(hm)]
        return sum(1 for a, b in zip(vals, vals[1:]) if a == b) / max(1, len(vals) - 1)

    stuck = {lid for lid, (_, _, hm) in series.items() if _repeats(hm) > 0.2}   # don't use as neighbours
    out = []
    for lid, (c, sid, hm) in series.items():
        vals = [hm[k] for k in sorted(hm)]
        repeats = _repeats(hm)
        pairs = []
        for k, v in hm.items():
            others = [s[2][k] for l2, s in series.items() if l2 != lid and l2 not in stuck and k in s[2]]
            if len(others) >= 2:
                pairs.append((v, statistics.median(others)))
        r = _pearson([p[0] for p in pairs], [p[1] for p in pairs])
        ratios = [v / m for v, m in pairs if m > 5]
        peak = [v / m for v, m in pairs if m > 150]
        ratio = statistics.median(ratios) if ratios else None
        ratio_peak = statistics.median(peak) if len(peak) >= 12 else None
        coverage = len(hm) / total_hours
        score = None
        if r is not None and ratio:
            dev = max(abs(math.log(ratio)), abs(math.log(ratio_peak)) if ratio_peak else 0)
            score = 100 * (0.35 * min(coverage, 1) + 0.35 * max(r, 0)
                           + 0.20 * max(0.0, 1 - dev * 2)
                           + 0.10 * (1 - min(repeats * 5, 1)))
        out.append({
            "station": c["name"], "provider": c.get("provider", ""),
            "distance_km": c["distance_km"], "openaq_location_id": lid, "pm25_sensor_id": sid,
            "hours": len(hm), "coverage_pct": round(coverage * 100),
            "mean_pm25": round(statistics.fmean(vals), 1),
            "repeats_pct": round(repeats * 100, 1),
            "neighbour_r": round(r, 2) if r is not None else "",
            "ratio_to_neighbours": round(ratio, 2) if ratio else "",
            "ratio_on_bad_hours": round(ratio_peak, 2) if ratio_peak else "",
            "neighbours_compared_hours": len(pairs),
            "score": round(score) if score is not None else "",
            "source_url": f"https://explore.openaq.org/locations/{lid}",
        })
    out.sort(key=lambda x: -(x["score"] if x["score"] != "" else -1))

    waqi_rows = []
    if waqi:
        try:
            d = radius_km / 111.0
            for s in waqi.stations_in_bounds([lat - d, lng - d, lat + d, lng + d]):
                st = s.get("station") or {}
                waqi_rows.append({"waqi_station": st.get("name"), "waqi_uid": s.get("uid"),
                                  "waqi_aqi_now": s.get("aqi"), "waqi_time": st.get("time"),
                                  "distance_km": round(haversine(lat, lng, s["lat"], s["lon"]), 2)
                                  if s.get("lat") is not None else ""})
        except Exception as e:
            print(f"  WAQI snapshot failed: {e}")
    return out, waqi_rows


def main():
    radius = float(sys.argv[1]) if len(sys.argv) > 1 else 6.0
    with open(os.path.join(APP_DIR, "config.json"), encoding="utf-8") as f:
        cfg = json.load(f)
    if not cfg.get("openaq_key"):
        sys.exit("config.json needs an openaq_key")
    oaq = OpenAQClient(cfg["openaq_key"])
    waqi = WAQIClient(cfg["waqi_token"]) if cfg.get("waqi_token") else None
    print(f"Auditing monitors within {radius:g} km — about 2 s per monitor…")
    rows, wrows = audit(cfg, oaq, waqi, radius)
    os.makedirs(DATA_DIR, exist_ok=True)
    path = os.path.join(DATA_DIR, "station_audit.csv")
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        if rows:
            w = csv.DictWriter(f, fieldnames=list(rows[0]))
            w.writeheader(); w.writerows(rows)
    with open(os.path.join(DATA_DIR, "station_audit_waqi.csv"), "w", encoding="utf-8-sig", newline="") as f:
        if wrows:
            w = csv.DictWriter(f, fieldnames=list(wrows[0]))
            w.writeheader(); w.writerows(wrows)
    print(f"\n{'station':38} {'km':>5} {'cov%':>5} {'r':>5} {'ratio':>6} {'bad-hr':>7} {'stuck%':>7} {'score':>6}")
    for r in rows:
        print(f"{r['station'][:38]:38} {r['distance_km']:>5} {r['coverage_pct']:>5} {str(r['neighbour_r']):>5} "
              f"{str(r['ratio_to_neighbours']):>6} {str(r['ratio_on_bad_hours']):>7} {r['repeats_pct']:>7} {str(r['score']):>6}")
    print(f"\nSaved {path}")


if __name__ == "__main__":
    main()
