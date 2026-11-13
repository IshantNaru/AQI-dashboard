"""
Evaluate the AQI pipeline from the recorded CSVs (no network needed).

There is no ground truth for "the air in your area", so the pipeline is judged
the way spatial estimates usually are:

1. Leave-one-out (LOO) accuracy — hide one monitor, estimate it from the
   others with each method (median / average / distance-weighted), compare to
   what it actually read. Lower error = the method better represents a point
   in the area where there is no monitor (i.e. your house).
2. Robustness — corrupt one monitor on purpose (reads half, or double) and
   measure how far each method's area value moves. Smaller = harder to fool
   with one faulty or sprinkled monitor.
3. Cross-source agreement — the same monitor via OpenAQ (our hourly AQI) vs
   WAQI's live AQI: bias and typical gap.
4. Data health — completeness per monitor, outages where every monitor was
   missing, and integrity checks (duplicates, AQI recomputation).

Run:  python evaluate.py           Output: data/evaluation_report.txt, data/evaluation_loo.csv
"""
import csv
import json
import math
import os
import statistics
import sys
from collections import defaultdict
from datetime import timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from recorder import Store, DATA_DIR, METHOD_COLS, overall_aqi, parse_dt, _num, haversine, _label


def _bucket(iso):
    return (parse_dt(iso) + timedelta(minutes=15)).replace(minute=0, second=0, microsecond=0)


def _est(method, vals, dists):
    if method == "median":
        return statistics.median(vals)
    if method == "mean":
        return statistics.fmean(vals)
    w = [1 / max(d, 0.5) ** 2 for d in dists]
    return sum(v * x for v, x in zip(vals, w)) / sum(w)


def _stats(errs):
    if not errs:
        return None
    return {"n": len(errs), "mae": statistics.fmean(abs(e) for e in errs),
            "rmse": math.sqrt(statistics.fmean(e * e for e in errs)), "bias": statistics.fmean(errs)}


def evaluate(data_dir=DATA_DIR):
    S = Store(data_dir)
    rows = [r for m in S.months() for r in S.read(S.path("readings", m))]
    live = [r for m in S.months("live") for r in S.read(S.path("live", m))]
    with open(os.path.join(data_dir, "state.json"), encoding="utf-8") as f:
        state = json.load(f)
    home = state["stations"].get("home") or {}
    comps = {str(c["location_id"]): c for c in home.get("components") or []}
    lines, loo_out = [], []
    say = lines.append

    # per-monitor hourly PM2.5 for the area monitors
    per_hour = defaultdict(dict)
    for r in rows:
        t = r["target"]
        if t.startswith("home|") and _num(r.get("pm25_ugm3")) is not None:
            per_hour[_bucket(r["hour_start_utc"])][t.split("|")[1]] = float(r["pm25_ugm3"])

    say("AQI PIPELINE EVALUATION")
    say("=" * 72)
    if not comps:
        say("Your area is a single monitor, so the method tests (1–2) don't apply.")
    else:
        names = {k: _label(c["name"]) for k, c in comps.items()}
        hours = sorted(h for h, v in per_hour.items() if len(v) >= 3)
        say(f"Area monitors: {', '.join(names.values())}")
        say(f"Hours with ≥3 monitors reporting: {len(hours)}  ({hours[0]:%d %b} – {hours[-1]:%d %b})" if hours else "No usable hours yet.")

        # 1. leave-one-out
        say("\n1. LEAVE-ONE-OUT ACCURACY  (PM2.5 µg/m³; estimate a hidden monitor from the rest)")
        errs = {m: [] for m in METHOD_COLS}
        by_mon = {m: defaultdict(list) for m in METHOD_COLS}
        for h in hours:
            v = per_hour[h]
            for k, actual in v.items():
                others = [(kk, vv) for kk, vv in v.items() if kk != k]
                c0 = comps[k]
                d = [haversine(c0["lat"], c0["lng"], comps[kk]["lat"], comps[kk]["lng"]) for kk, _ in others]
                for m in METHOD_COLS:
                    e = _est(m, [vv for _, vv in others], d) - actual
                    errs[m].append(e)
                    by_mon[m][k].append(e)
                    loo_out.append({"hour_utc": h.isoformat(), "held_out": names[k], "actual": actual,
                                    "method": m, "estimate": round(actual + e, 1), "error": round(e, 1)})
        say(f"   {'method':22} {'MAE':>7} {'RMSE':>7} {'bias':>7}   (n={len(errs['median'])})")
        for m in METHOD_COLS:
            s = _stats(errs[m])
            if s:
                say(f"   {m:22} {s['mae']:7.1f} {s['rmse']:7.1f} {s['bias']:+7.1f}")
        say("   Error when each monitor is the one hidden (median method) — a monitor that is")
        say("   hard to predict from its neighbours reads differently from the area:")
        for k in comps:
            s = _stats(by_mon["median"][k])
            if s:
                say(f"     {names[k]:24} MAE {s['mae']:6.1f}   bias {s['bias']:+6.1f}  "
                    f"({'neighbours read higher' if s['bias'] > 0 else 'neighbours read lower'})")

        # 2. robustness
        say("\n2. ROBUSTNESS  (one monitor corrupted on purpose; how far the area value moves)")
        lat, lng = home.get("lat"), home.get("lng")
        dist_home = {k: haversine(lat, lng, c["lat"], c["lng"]) for k, c in comps.items()} if lat else {}
        for factor, desc in ((0.5, "reads half (e.g. sprinklers)"), (2.0, "reads double (e.g. local fire)")):
            shift = {m: [] for m in METHOD_COLS}
            for h in hours:
                v = per_hour[h]
                ks = list(v)
                base = {m: _est(m, [v[k] for k in ks], [dist_home.get(k, 1) for k in ks]) for m in METHOD_COLS}
                for bad in ks:
                    vv = [v[k] * (factor if k == bad else 1) for k in ks]
                    for m in METHOD_COLS:
                        new = _est(m, vv, [dist_home.get(k, 1) for k in ks])
                        shift[m].append(abs(new - base[m]) / base[m] * 100 if base[m] else 0)
            say(f"   One monitor {desc}:")
            for m in METHOD_COLS:
                if shift[m]:
                    say(f"     {m:20} average shift {statistics.fmean(shift[m]):5.1f}%   worst {max(shift[m]):5.1f}%")

    # 3. cross-source
    say("\n3. CROSS-SOURCE AGREEMENT  (OpenAQ hourly AQI vs WAQI live AQI, same monitor)")
    oa = {}
    for r in rows:
        if r["target"] in ("tracked",) and _num(r.get("aqi_us")) is not None:
            oa[_bucket(r["hour_start_utc"])] = float(r["aqi_us"])
    best = None
    for off in (0, -1):                      # WAQI time = hour start (0) or hour end (-1)?
        diffs = []
        for r in live:
            if r["target"] != "tracked":
                continue
            t = None
            try:
                from datetime import datetime
                t = datetime.fromisoformat(r["station_time_local"]).astimezone()
            except (ValueError, TypeError):
                continue
            h = (t + timedelta(hours=off, minutes=15)).astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
            w = _num(r.get("waqi_aqi"))
            if h in oa and w is not None and oa[h] <= 500 and w <= 500:
                diffs.append(w - oa[h])
        s = _stats(diffs)
        if s and (best is None or s["mae"] < best[1]["mae"]):
            best = (off, s)
    if best:
        off, s = best
        say(f"   Worst-in-Delhi monitor, {s['n']} matched hours (AQI ≤ 500 only):")
        say(f"     WAQI − OpenAQ: mean {s['bias']:+.0f} AQI, typical gap {s['mae']:.0f}, RMSE {s['rmse']:.0f}")
        say(f"     (best alignment: WAQI timestamp = hour {'start' if off == 0 else 'end'})")
    else:
        say("   Not enough matched hours yet.")

    # 4. data health
    say("\n4. DATA HEALTH")
    keys = [(r["target"], r["hour_start_utc"]) for r in rows]
    say(f"   Rows {len(rows)}, duplicates {len(keys) - len(set(keys))}")
    bad = 0
    for r in rows:
        for c25, c10, ca in METHOD_COLS.values():
            if _num(r.get(ca)) is not None and overall_aqi(_num(r.get(c25)), _num(r.get(c10)))[0] != int(float(r[ca])):
                bad += 1
    say(f"   AQI recomputation mismatches: {bad}")
    for t in sorted({r["target"] for r in rows}):
        rr = [r for r in rows if r["target"] == t and r.get("status")]
        if not rr:
            continue
        ok = sum(1 for r in rr if r["status"] == "ok")
        nd = sum(1 for r in rr if r["status"].startswith("no data"))
        er = sum(1 for r in rr if r["status"].startswith("error"))
        say(f"   {t:12} {rr[0]['station'][:26]:26} completeness {ok / len(rr) * 100:5.1f}%  "
            f"(no data {nd}, errors {er}, of {len(rr)} tracked hours)")
    outs, run = [], None
    area = sorted((_bucket(r["hour_start_utc"]), r["status"]) for r in rows if r["target"] == "home" and r.get("status"))
    for h, st in area:
        if st.startswith("insufficient: 0"):
            run = [run[0] if run else h, h]
        elif run:
            outs.append(run); run = None
    if run:
        outs.append(run)
    say(f"   Full outages (no area monitor published): {len(outs)}, {sum(int((b - a).total_seconds() // 3600) + 1 for a, b in outs)} hours total")
    for a, b in outs[-5:]:
        say(f"     {a.astimezone():%d %b %H:%M} – {b.astimezone() + timedelta(hours=1):%d %b %H:%M}")

    os.makedirs(data_dir, exist_ok=True)
    with open(os.path.join(data_dir, "evaluation_report.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    if loo_out:
        with open(os.path.join(data_dir, "evaluation_loo.csv"), "w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(loo_out[0]))
            w.writeheader(); w.writerows(loo_out)
    return "\n".join(lines)


if __name__ == "__main__":
    print(evaluate())
