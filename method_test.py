"""
method_test.py — test the "your area" methods yourself.

WHAT IT ANSWERS
    "Which way of combining nearby monitors (median / average / distance-weighted)
     gives the most trustworthy number for a place that has no monitor of its own?"

WHY THIS IS HARD
    Your house has no monitor, so there is no "true" value to compare against.

THE TRICK: LEAVE-ONE-OUT (LOO) CROSS-VALIDATION
    Every monitor IS a place with a known true value. So, one hour at a time:
        1. hide one monitor,
        2. estimate its reading from the remaining monitors with each method,
        3. compare the estimate with what the hidden monitor actually measured.
    Repeat for every monitor and every hour. The method with the smallest typical
    error is the best at estimating an unmeasured spot — like your house.

SECOND TEST: FAULT INJECTION (ROBUSTNESS)
    Pretend one monitor misbehaves (e.g. reads half because of water sprinklers,
    or double because of a fire next to it) and measure how far each method's
    area value moves. A good method shouldn't be dragged around by one bad monitor.

INPUT   data/readings_YYYY-MM.csv  (rows with target "home|<id>" = each monitor's hourly PM2.5)
        data/state.json            (monitor names and coordinates)
OUTPUT  printed tables + data/method_test_results.csv (every single prediction)

USAGE
    python method_test.py                      # all data, IDW power 2
    python method_test.py --power 1            # try a gentler distance weighting
    python method_test.py --from 2026-11-01    # only November
    python method_test.py --min-monitors 4     # only hours where ≥4 monitors reported
    python method_test.py --faults 0.5 0.7 1.5 2

Only the Python standard library is used, so it runs anywhere.
"""
import argparse
import csv
import glob
import json
import math
import os
import statistics
from collections import defaultdict
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")


# ─────────────────────────── small helpers ───────────────────────────────────

def distance_km(lat1, lng1, lat2, lng2):
    """Great-circle (haversine) distance between two points on Earth, in km."""
    p = math.pi / 180
    a = (0.5 - math.cos((lat2 - lat1) * p) / 2
         + math.cos(lat1 * p) * math.cos(lat2 * p) * (1 - math.cos((lng2 - lng1) * p)) / 2)
    return 12742 * math.asin(math.sqrt(a))


def hour_key(iso_utc):
    """Monitors label hours at :00 or :30 UTC; round both into the same hourly bucket."""
    t = datetime.fromisoformat(iso_utc.replace("Z", "+00:00")) + timedelta(minutes=15)
    return t.replace(minute=0, second=0, microsecond=0)


def short_name(name):
    """'Pusa, Delhi - DPCC' -> 'Pusa (DPCC)' so the two Pusa monitors stay distinct."""
    first = name.split(",")[0].strip()
    agency = name.rsplit(" - ", 1)[1].strip() if " - " in name else ""
    return f"{first} ({agency})" if agency else first


# ─────────────────────────── the three methods ───────────────────────────────
# Each takes the readings of the monitors used and their distances (km) to the
# point being estimated, and returns one number.

def median(values, distances, power):
    # Middle value. With 4 values it is the average of the middle two.
    return statistics.median(values)


def average(values, distances, power):
    # Plain mean: every monitor counts equally.
    return statistics.fmean(values)


def distance_weighted(values, distances, power):
    # Inverse-distance weighting (IDW): weight = 1 / distance^power.
    # power 2 is the common default; distances under 0.5 km are clamped so one
    # very close monitor can't get an infinite weight.
    weights = [1 / max(d, 0.5) ** power for d in distances]
    return sum(v * w for v, w in zip(values, weights)) / sum(weights)


METHODS = {"median": median, "average": average, "distance-weighted": distance_weighted}


# ─────────────────────────── load the recorded data ──────────────────────────

def load(date_from=None, date_to=None):
    with open(os.path.join(DATA, "state.json"), encoding="utf-8") as f:
        home = (json.load(f).get("stations") or {}).get("home") or {}
    monitors = {str(c["location_id"]): c for c in home.get("components") or []}
    if not monitors:
        raise SystemExit("Your area uses a single monitor — there is nothing to compare.")

    # readings[hour][monitor_id] = PM2.5 µg/m³
    readings = defaultdict(dict)
    for path in sorted(glob.glob(os.path.join(DATA, "readings_*.csv"))):
        with open(path, encoding="utf-8-sig", newline="") as f:
            for r in csv.DictReader(f):
                tgt = r["target"]
                if not tgt.startswith("home|") or not r.get("pm25_ugm3"):
                    continue                       # only real per-monitor readings
                day = r["hour_start_local"][:10]
                if (date_from and day < date_from) or (date_to and day > date_to):
                    continue
                mid = tgt.split("|", 1)[1]
                if mid in monitors:
                    readings[hour_key(r["hour_start_utc"])][mid] = float(r["pm25_ugm3"])
    return home, monitors, readings


# ─────────────────────────── test 1: leave-one-out ───────────────────────────

def leave_one_out(monitors, readings, min_monitors, power):
    """Returns (errors[method] list, errors_by_monitor[method][id] list, all prediction rows)."""
    errors = {m: [] for m in METHODS}
    by_monitor = {m: defaultdict(list) for m in METHODS}
    rows = []
    for hour, vals in sorted(readings.items()):
        if len(vals) < min_monitors:
            continue                               # too few monitors to hide one fairly
        for hidden, actual in vals.items():
            others = {k: v for k, v in vals.items() if k != hidden}
            h = monitors[hidden]
            # distances FROM THE HIDDEN MONITOR to each remaining monitor
            dists = [distance_km(h["lat"], h["lng"], monitors[k]["lat"], monitors[k]["lng"]) for k in others]
            for name, fn in METHODS.items():
                estimate = fn(list(others.values()), dists, power)
                err = estimate - actual            # + means the method over-estimated
                errors[name].append(err)
                by_monitor[name][hidden].append(err)
                rows.append({"hour_utc": hour.isoformat(), "hidden_monitor": short_name(h["name"]),
                             "actual_pm25": actual, "method": name,
                             "estimate_pm25": round(estimate, 1), "error": round(err, 1)})
    return errors, by_monitor, rows


def summarise(errs):
    """MAE = typical size of error; RMSE = punishes big misses; bias = systematic over/under."""
    if not errs:
        return None
    return {"n": len(errs),
            "mae": statistics.fmean(abs(e) for e in errs),
            "rmse": math.sqrt(statistics.fmean(e * e for e in errs)),
            "bias": statistics.fmean(errs)}


# ─────────────────────────── test 2: fault injection ─────────────────────────

def fault_injection(home, monitors, readings, min_monitors, power, factors):
    """For each hour, corrupt each monitor in turn by `factor` and measure the % change
    in the area value (estimated for YOUR location, like the app does)."""
    lat, lng = home["lat"], home["lng"]
    d_home = {k: distance_km(lat, lng, c["lat"], c["lng"]) for k, c in monitors.items()}
    result = {}
    for factor in factors:
        shifts = {m: [] for m in METHODS}
        for vals in readings.values():
            if len(vals) < min_monitors:
                continue
            ids = list(vals)
            dists = [d_home[k] for k in ids]
            for name, fn in METHODS.items():
                base = fn([vals[k] for k in ids], dists, power)
                for bad in ids:
                    corrupted = [vals[k] * (factor if k == bad else 1) for k in ids]
                    if base > 0:
                        shifts[name].append(abs(fn(corrupted, dists, power) - base) / base * 100)
        result[factor] = shifts
    return result, d_home


# ─────────────────────────── report ──────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--from", dest="date_from", help="first local date to include, YYYY-MM-DD")
    ap.add_argument("--to", dest="date_to", help="last local date to include, YYYY-MM-DD")
    ap.add_argument("--min-monitors", type=int, default=3, help="skip hours with fewer monitors (default 3)")
    ap.add_argument("--power", type=float, default=2.0, help="IDW distance power (default 2)")
    ap.add_argument("--faults", type=float, nargs="+", default=[0.5, 2.0], help="corruption factors to test")
    a = ap.parse_args()

    home, monitors, readings = load(a.date_from, a.date_to)
    names = {k: short_name(c["name"]) for k, c in monitors.items()}
    usable = [h for h, v in readings.items() if len(v) >= a.min_monitors]
    if not usable:
        raise SystemExit("No hours with enough monitors in that date range.")
    print(f"Monitors: {', '.join(names.values())}")
    print(f"Hours tested: {len(usable)}  ({min(usable):%d %b} – {max(usable):%d %b} UTC)   "
          f"IDW power: {a.power:g}\n")

    errors, by_mon, rows = leave_one_out(monitors, readings, a.min_monitors, a.power)
    print("TEST 1 — LEAVE-ONE-OUT (PM2.5 µg/m³; lower is better)")
    print(f"  {'method':20} {'MAE':>7} {'RMSE':>7} {'bias':>7}")
    ranked = sorted(METHODS, key=lambda m: summarise(errors[m])["mae"])
    for m in ranked:
        s = summarise(errors[m])
        print(f"  {m:20} {s['mae']:7.1f} {s['rmse']:7.1f} {s['bias']:+7.1f}")
    print(f"  → best: {ranked[0]}\n")

    print("  Per monitor (how well its neighbours predict it, median method):")
    for k in monitors:
        s = summarise(by_mon["median"][k])
        if s:
            side = "reads LOWER than its neighbours" if s["bias"] > 0 else "reads HIGHER than its neighbours"
            print(f"    {names[k]:22} MAE {s['mae']:6.1f}   bias {s['bias']:+6.1f}  → {side}")

    shifts, d_home = fault_injection(home, monitors, readings, a.min_monitors, a.power, a.faults)
    print("\nTEST 2 — FAULT INJECTION (% the area value moves: average / worst case; lower is better)")
    print("  " + f"{'one monitor reads ×':22}" + "".join(f"{m:>22}" for m in METHODS))
    for f, sh in shifts.items():
        print("  " + f"{f:<22g}" + "".join(f"{statistics.fmean(sh[m]):11.1f}% /{max(sh[m]):6.1f}%" for m in METHODS))
    print("  Note: averaged over *which* monitor fails, average and distance-weighted move the same")
    print("  amount (a maths identity). They differ in the worst case: distance-weighted is hit")
    print("  hardest when the heavily-weighted nearest monitor is the faulty one.")

    print("\n  Weight each monitor gets in the distance-weighted average at your location:")
    w = {k: 1 / max(d, 0.5) ** a.power for k, d in d_home.items()}
    for k in sorted(w, key=w.get, reverse=True):
        print(f"    {names[k]:22} {d_home[k]:4.1f} km  → {w[k] / sum(w.values()) * 100:5.1f}% of the weight")

    out = os.path.join(DATA, "method_test_results.csv")
    with open(out, "w", encoding="utf-8-sig", newline="") as f:
        wr = csv.DictWriter(f, fieldnames=list(rows[0]))
        wr.writeheader(); wr.writerows(rows)
    print(f"\nEvery prediction saved to {out}")


if __name__ == "__main__":
    main()
