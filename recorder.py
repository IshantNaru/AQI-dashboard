"""
Background AQI recorder.

Every poll (default 15 min) it:
  1. Pulls *hourly* PM2.5 / PM10 averages from OpenAQ for each tracked station
     (your area + one fixed station). Because OpenAQ keeps history, hours when
     the laptop was off or asleep are back-filled on the next poll — the daily
     average is a true 24-hour figure, not "hours the laptop was on".
  2. Computes US AQI from the concentrations (EPA 2024 breakpoints, aqi_math.py).
  3. Logs WAQI's live AQI for the same spot as an independent cross-check.
  4. Rewrites the month's daily summary.

Files (all in ./data, one per month, UTF-8 with BOM so Excel opens them cleanly):
  readings_YYYY-MM.csv  hourly rows, one per (target, hour) — the ground truth
  daily_YYYY-MM.csv     daily means + EPA daily AQI + coverage
  live_YYYY-MM.csv      WAQI live snapshots (cross-check)
  state.json            which OpenAQ station/sensors each target is pinned to
"""
import csv
import json
import logging
import math
import os
import collections
import statistics
import time
from datetime import datetime, timedelta, timezone

from aqi_math import live_row_aqi, overall_aqi, pm25_to_aqi, category

log = logging.getLogger("aqi")

APP_DIR  = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(APP_DIR, "data")

# Second line: the monitor with the highest 30-day mean PM2.5 in the city, re-picked each month.
DEFAULT_TRACKED = {"mode": "worst_30d", "city": "Delhi", "lat": 28.6139, "lng": 77.2090,
                   "radius_km": 25, "name_filter": "Delhi"}

HOURLY_FIELDS = [
    "target", "hour_start_local", "hour_start_utc", "station", "openaq_location_id", "provider",
    "pm25_ugm3", "pm25_coverage_pct", "pm10_ugm3", "pm10_coverage_pct",
    "aqi_us", "dominant", "aqi_category",
    "pm25_sensor_id", "pm10_sensor_id", "source_url", "fetched_at_local",
    "monitors_used", "spread_pm25",
    # area targets only: the same hour by the other two methods
    "pm25_mean", "pm10_mean", "aqi_mean", "pm25_idw", "pm10_idw", "aqi_idw",
    "monitors_missing", "status",
]
METHOD_COLS = {   # method -> (pm25, pm10, aqi) columns in readings_*.csv
    "median": ("pm25_ugm3", "pm10_ugm3", "aqi_us"),
    "mean":   ("pm25_mean", "pm10_mean", "aqi_mean"),
    "idw":    ("pm25_idw", "pm10_idw", "aqi_idw"),
}
METHOD_NAMES = {"median": "median", "mean": "average", "idw": "distance-weighted average"}
LIVE_FIELDS = [
    "target", "polled_at_local", "station_time_local", "waqi_station", "waqi_uid",
    "waqi_aqi", "waqi_pm25_subindex", "waqi_dominant", "waqi_url", "waqi_parts",
    "waqi_aqi_raw", "aqi_scale",          # raw = WAQI's own (older-scale) number; scale of waqi_aqi
]
DAILY_FIELDS = [
    "date", "target", "station", "pm25_hours", "coverage_pct", "epa_valid_24h",
    "pm25_mean_ugm3", "pm10_mean_ugm3", "aqi_daily_us", "dominant", "aqi_category",
    "pm25_max_hour_ugm3", "aqi_hourly_max", "aqi_hourly_max_at",
    "waqi_live_aqi_mean", "waqi_live_samples", "openaq_location_id", "source_url",
    "pm25_daily_mean_method", "aqi_daily_mean_method", "pm25_daily_idw_method", "aqi_daily_idw_method",
]

EXPLORE_URL = "https://explore.openaq.org/locations/{}"


# ─────────────────────────── helpers ─────────────────────────────────────────

def _g(d, *keys, default=None):
    """First present key — tolerates camelCase (raw API) and snake_case."""
    if not isinstance(d, dict):
        return default
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return default


def parse_dt(s):
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def iso_z(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def local_iso(dt):
    return dt.astimezone().isoformat(timespec="seconds")


def haversine(lat1, lng1, lat2, lng2):
    p = math.pi / 180
    a = (0.5 - math.cos((lat2 - lat1) * p) / 2
         + math.cos(lat1 * p) * math.cos(lat2 * p) * (1 - math.cos((lng2 - lng1) * p)) / 2)
    return 12742 * math.asin(math.sqrt(a))


def _num(x):
    try:
        v = float(x)
        return None if math.isnan(v) else v
    except (TypeError, ValueError):
        return None


def _valid_conc(v):
    v = _num(v)
    return v if v is not None and 0 <= v <= 1500 else None   # drops sentinels / negatives


# ─────────────────────────── storage ─────────────────────────────────────────

class Store:
    def __init__(self, data_dir=DATA_DIR):
        self.dir = data_dir
        os.makedirs(self.dir, exist_ok=True)

    def path(self, kind, month):
        return os.path.join(self.dir, f"{kind}_{month}.csv")

    @staticmethod
    def read(path):
        try:
            with open(path, "r", encoding="utf-8-sig", newline="") as f:
                return list(csv.DictReader(f))
        except FileNotFoundError:
            return []

    @staticmethod
    def write_atomic(path, fields, rows):
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)
        os.replace(tmp, path)   # PermissionError if the CSV is open in Excel

    def months(self, kind="readings"):
        out = []
        for name in os.listdir(self.dir):
            if name.startswith(kind + "_") and name.endswith(".csv"):
                out.append(name[len(kind) + 1:-4])
        return sorted(out)

    def upsert_hourly(self, rows):
        """Insert or replace by (target, hour_start_utc). Returns (added, updated, months)."""
        by_month = {}
        for r in rows:
            by_month.setdefault(r["hour_start_local"][:7], []).append(r)
        added = updated = 0
        for month, new_rows in by_month.items():
            p = self.path("readings", month)
            existing = {(r["target"], r["hour_start_utc"]): r for r in self.read(p)}
            for r in new_rows:
                k = (r["target"], r["hour_start_utc"])
                placeholder = r.get("pm25_ugm3") in ("", None) and r.get("pm10_ugm3") in ("", None)
                if placeholder and k in existing and (existing[k].get("pm25_ugm3") or existing[k].get("pm10_ugm3")):
                    continue
                if k not in existing:
                    added += 0 if placeholder else 1
                elif any(str(existing[k].get(f, "")) != str(r.get(f, ""))
                         for f in ("pm25_ugm3", "pm10_ugm3", "pm25_coverage_pct")):
                    updated += 1
                existing[k] = {**existing.get(k, {}), **r}
            merged = sorted(existing.values(), key=lambda r: (r["hour_start_utc"], r["target"]))
            self.write_atomic(p, HOURLY_FIELDS, merged)
        return added, updated, set(by_month)

    def purge_foreign(self, target, location_id, since_utc):
        """Drop this target's rows from `since_utc` on that came from a different monitor
        (left over from before a station switch, for hours the new monitor didn't report)."""
        removed, months = 0, set()
        for month in self.months():
            p = self.path("readings", month)
            rows = self.read(p)
            keep = [r for r in rows if not (r["target"] == target
                                            and r["hour_start_utc"] >= iso_z(since_utc)
                                            and str(r.get("openaq_location_id")) != str(location_id))]
            if len(keep) != len(rows):
                removed += len(rows) - len(keep)
                months.add(month)
                self.write_atomic(p, HOURLY_FIELDS, keep)
        return removed, months

    def purge_live(self, target, month):
        """Drop a target's WAQI cross-check rows for a month (used when its monitor is switched)."""
        p = self.path("live", month)
        rows = self.read(p)
        keep = [r for r in rows if r["target"] != target]
        if len(keep) != len(rows):
            self.write_atomic(p, LIVE_FIELDS, keep)
        return len(rows) - len(keep)

    def append_live(self, row):
        month = row["polled_at_local"][:7]
        p = self.path("live", month)
        rows = self.read(p)
        for r in rows:   # same station report already logged → skip
            if (r["target"], r["waqi_uid"], r["station_time_local"]) == \
               (row["target"], str(row["waqi_uid"]), row["station_time_local"]):
                return False
        rows.append(row)
        self.write_atomic(p, LIVE_FIELDS, rows)
        return True

    def rebuild_daily(self, month):
        hourly = self.read(self.path("readings", month))
        live   = self.read(self.path("live", month))
        daily  = compute_daily(hourly, live)
        self.write_atomic(self.path("daily", month), DAILY_FIELDS, daily)
        return daily


def compute_daily(hourly_rows, live_rows=()):
    """
    Daily figures per (local date, target).
    Hourly values are equal-length averages, so their plain mean *is* the
    time-weighted daily mean. EPA daily AQI = AQI(mean concentration).
    EPA treats a 24-h average as valid with >= 18 of 24 hours (75 %).
    """
    groups = {}
    for r in hourly_rows:
        groups.setdefault((r["hour_start_local"][:10], r["target"]), []).append(r)

    live_groups = {}
    for r in live_rows:
        a = live_row_aqi(r)
        if a is not None:
            live_groups.setdefault((r["station_time_local"][:10], r["target"]), []).append(a)

    out = []
    for (date, target), rows in sorted(groups.items()):
        pm25 = [v for v in (_num(r.get("pm25_ugm3")) for r in rows) if v is not None]
        pm10 = [v for v in (_num(r.get("pm10_ugm3")) for r in rows) if v is not None]
        pm25_mean = round(statistics.fmean(pm25), 1) if pm25 else None
        pm10_mean = round(statistics.fmean(pm10), 1) if pm10 else None
        aqi, dom = overall_aqi(pm25_mean, pm10_mean)

        hourly_aqi = [(int(_num(r["aqi_us"])), r["hour_start_local"]) for r in rows if _num(r.get("aqi_us")) is not None]
        peak = max(hourly_aqi) if hourly_aqi else (None, "")
        lv = live_groups.get((date, target), [])
        stations = sorted({r.get("station", "") for r in rows if r.get("station")})
        loc_ids  = sorted({r.get("openaq_location_id", "") for r in rows if r.get("openaq_location_id")})
        extra = {}
        for m in ("mean", "idw"):
            c25, c10, _ = METHOD_COLS[m]
            a25 = [v for v in (_num(r.get(c25)) for r in rows) if v is not None]
            a10 = [v for v in (_num(r.get(c10)) for r in rows) if v is not None]
            if a25:
                m25 = round(statistics.fmean(a25), 1)
                m10 = round(statistics.fmean(a10), 1) if a10 else None
                extra[f"pm25_daily_{m}_method"] = m25
                extra[f"aqi_daily_{m}_method"] = overall_aqi(m25, m10)[0]
        ids_flat = sorted({i for x in loc_ids for i in str(x).split("+") if i})

        out.append({
            "date": date, "target": target, "station": " / ".join(stations),
            "pm25_hours": len(pm25), "coverage_pct": round(len(pm25) / 24 * 100),
            "epa_valid_24h": "yes" if len(pm25) >= 18 else "no",
            "pm25_mean_ugm3": pm25_mean, "pm10_mean_ugm3": pm10_mean,
            "aqi_daily_us": aqi, "dominant": dom or "", "aqi_category": category(aqi) if aqi is not None else "",
            "pm25_max_hour_ugm3": max(pm25) if pm25 else None,
            "aqi_hourly_max": peak[0], "aqi_hourly_max_at": peak[1][11:16] if peak[1] else "",
            "waqi_live_aqi_mean": round(statistics.fmean(lv)) if lv else None,
            "waqi_live_samples": len(lv),
            "openaq_location_id": " / ".join(loc_ids),
            "source_url": " ".join(EXPLORE_URL.format(i) for i in ids_flat),
            **extra,
        })
    return out


# ─────────────────────────── recorder ────────────────────────────────────────

class Recorder:
    def __init__(self, config, openaq, waqi=None, data_dir=DATA_DIR, now_fn=None):
        self.config = config
        self.openaq = openaq
        self.waqi   = waqi
        self.store  = Store(data_dir)
        self.now    = now_fn or (lambda: datetime.now(timezone.utc))
        self.state_path = os.path.join(self.store.dir, "state.json")
        self.state = self._load_state()
        self._switched = set()

    # ── config / state ──────────────────────────────────────────────────────
    @property
    def rcfg(self):
        return self.config.get("recorder") or {}

    def targets(self):
        loc = self.config.get("location") or {}
        t = {}
        ids = self.rcfg.get("home_location_ids")
        area = self.rcfg.get("home_area")
        if (ids or area) and loc.get("lat") is not None:
            area = area or {}
            st = self.state["stations"].get("home") or {}
            n = len(st.get("components") or []) if st.get("mode") == "median" else (len(ids) if ids else 0)
            t["home"] = {"mode": "median", "name": None,
                         "location_ids": [int(i) for i in ids] if ids else None,
                         "radius_km": float(area.get("radius_km", 5)),
                         "max_radius_km": float(area.get("max_radius_km", 8)),
                         "exclude": [int(i) for i in area.get("exclude_location_ids", [])],
                         "lat": float(loc["lat"]), "lng": float(loc["lng"]),
                         "min_monitors": int(self.rcfg.get("min_monitors", 2)),
                         "label": f"Your area ({n} monitors)" if n else "Your area"}
        elif loc.get("lat") is not None and loc.get("lng") is not None:
            hs = self.rcfg.get("home_station") or None      # e.g. "Shadipur" pins that monitor
            t["home"] = {"name": hs, "lat": float(loc["lat"]), "lng": float(loc["lng"]),
                         "label": f"Your area ({hs or loc.get('city', '')})".replace(" ()", "")}
        tr = self.rcfg.get("tracked_station", DEFAULT_TRACKED)
        if tr and tr.get("mode") == "worst_30d":
            st = self.state["stations"].get("tracked") or {}
            nm = _short_name(st.get("name")) if str(st.get("sig", "")).startswith("worst|") else ""
            city = tr.get("city", "city")
            t["tracked"] = {"mode": "worst_30d", "name": None,
                            "lat": float(tr.get("lat", 28.6139)), "lng": float(tr.get("lng", 77.2090)),
                            "radius_km": float(tr.get("radius_km", 25)),
                            "name_filter": tr.get("name_filter", city),
                            "label": f"Worst in {city}: {nm}" if nm else f"Worst in {city} (30-day)"}
        elif tr and tr.get("lat") is not None:
            t["tracked"] = {"name": tr.get("name"), "lat": float(tr["lat"]), "lng": float(tr["lng"]),
                            "label": tr.get("name") or "Tracked station"}
        return t

    def _load_state(self):
        try:
            with open(self.state_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {"stations": {}, "last_hour": {}, "last_sweep": ""}

    def _save_state(self):
        tmp = self.state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.state, f, indent=2)
        os.replace(tmp, self.state_path)

    # ── station resolution ──────────────────────────────────────────────────
    def _station(self, key, spec):
        if spec.get("mode") == "worst_30d":
            return self._worst_station(key, spec)
        if spec.get("mode") == "median":
            return self._median_station(key, spec)
        sig = f"v2|{spec['lat']:.4f},{spec['lng']:.4f},{spec.get('name') or ''}"
        cached = self.state["stations"].get(key)
        if cached and cached.get("sig") == sig and not self._stale(key, cached) \
                and not self._fallback_due(spec, cached):
            return cached
        locs = self.openaq.locations_near_raw(spec["lat"], spec["lng"],
                                              radius_m=5000 if spec.get("name") else 15000)
        st = pick_station(locs, spec["lat"], spec["lng"], spec.get("name"), self.now())
        if not st:
            raise RuntimeError(f"No active OpenAQ PM2.5 station near {spec['lat']:.3f},{spec['lng']:.3f}")
        st["sig"] = sig
        st["resolved_at"] = iso_z(self.now())
        if cached and cached.get("sig") != sig:      # settings/format changed → re-fetch full window
            self.state["last_hour"].pop(key, None)
        if cached and cached.get("location_id") != st["location_id"]:
            log.info("recorder: %s station changed %s -> %s", key, cached.get("name"), st["name"])
            self.state["last_hour"].pop(key, None)
            self._switched.add(key)
        elif cached:                       # same monitor: keep which sensors were working
            for k in ("pm25_sensor_id", "pm10_sensor_id"):
                if cached.get(k) in st.get(k + "s", []):
                    st[k] = cached[k]
        self.state["stations"][key] = st
        return st

    def _median_station(self, key, spec):
        """A fixed set of nearby monitors; the hourly value is their median (robust to one bad monitor)."""
        now = self.now()
        cached = self.state["stations"].get(key)
        if spec.get("location_ids"):
            sig = "median|" + ",".join(map(str, spec["location_ids"]))
        else:
            sig = (f"area|{spec['lat']:.4f},{spec['lng']:.4f}|{spec['radius_km']}|"
                   f"{spec['max_radius_km']}|{','.join(map(str, spec['exclude']))}")
        resolved = parse_dt((cached or {}).get("resolved_at"))
        if cached and cached.get("sig") == sig and cached.get("components") \
                and resolved and now - resolved < timedelta(days=1):
            return cached
        locs = self.openaq.locations_near_raw(spec["lat"], spec["lng"],
                                              radius_m=max(12000, spec.get("max_radius_km", 8) * 1000), limit=1000)
        by_id = {c["location_id"]: c for c in station_candidates(locs, spec["lat"], spec["lng"], now,
                                                               monitors_only=True)}
        if spec.get("location_ids"):
            ids = spec["location_ids"]
        elif cached and cached.get("sig") == sig:          # same place: keep the same set
            ids = [int(i) for i in cached["location_id"].split("+")]
        else:                                               # new place: nearest monitors within radius
            pool = sorted((c for c in by_id.values() if c["location_id"] not in spec["exclude"]),
                          key=lambda c: c["distance_km"])
            ids = [c["location_id"] for c in pool if c["distance_km"] <= spec["radius_km"]]
            if len(ids) < spec["min_monitors"]:
                ids = [c["location_id"] for c in pool if c["distance_km"] <= spec["max_radius_km"]]
            if not ids:
                raise RuntimeError(f"No monitors within {spec['max_radius_km']:g} km of this location")
            log.info("recorder: area monitors chosen: %s",
                     [(by_id[i]["name"], by_id[i]["distance_km"]) for i in ids])
        if cached and cached.get("components"):        # keep a monitor that is only briefly inactive
            for c in cached["components"]:
                by_id.setdefault(c["location_id"], c)
        comps = [by_id[i] for i in ids if i in by_id]
        if not comps:
            raise RuntimeError("None of the configured monitors were found on OpenAQ")
        if cached and cached.get("components"):        # keep sensors already found to work
            old = {c["location_id"]: c for c in cached["components"]}
            for c in comps:
                for k in ("pm25_sensor_id", "pm10_sensor_id"):
                    if old.get(c["location_id"], {}).get(k) in c.get(k + "s", []):
                        c[k] = old[c["location_id"]][k]
        st = {"mode": "median", "name": f"{len(ids)} nearby monitors",
              "location_id": "+".join(map(str, ids)), "components": comps,
              "lat": spec["lat"], "lng": spec["lng"], "sig": sig, "resolved_at": iso_z(now)}
        missing = [i for i in ids if i not in by_id]
        if missing:
            log.info("recorder: configured monitors not found on OpenAQ: %s", missing)
        if not cached or cached.get("location_id") != st["location_id"]:
            self.state["last_hour"].pop(key, None)
            self._switched.add(key)
        self.state["stations"][key] = st
        return st

    def _median_rows(self, key, st, frm, now, min_monitors=2):
        """Hourly area value by three methods (median / mean / 1/d² distance-weighted), plus a row
        for every monitor-hour that OpenAQ didn't return, so gaps are recorded in the CSV."""
        comp_rows, buckets, fetched = [], {}, local_iso(now)
        settled = now - timedelta(hours=3)                 # older hours should have arrived by now
        for c in st["components"]:
            tgt, err = f"{key}|{c['location_id']}", None
            try:
                pm25 = self._hours(c, "pm25", frm, now)
                pm10 = self._hours(c, "pm10", frm, now)
                rows = build_hourly_rows(tgt, c, pm25, pm10, now)
            except Exception as e:
                rows, err = [], str(e)[:160]
            for r in rows:
                r["status"] = "ok"
            comp_rows += rows
            comp_rows += missing_rows(tgt, c, {r["hour_start_utc"] for r in rows}, frm, settled, fetched,
                                      f"error: {err}" if err else
                                      "no data: app asked OpenAQ for this hour, nothing returned")
            for r in rows:
                b = parse_dt(r["hour_start_utc"]) + timedelta(minutes=15)
                buckets.setdefault(b.replace(minute=0, second=0, microsecond=0), []).append((r, c))
        names = {c["location_id"]: _label(c["name"]) for c in st["components"]}
        dist = {c["location_id"]: max(float(c.get("distance_km") or 0.5), 0.5) for c in st["components"]}
        n_total = len(st["components"])
        urls = " ".join(EXPLORE_URL.format(c["location_id"]) for c in st["components"])
        out = []
        for b, rc in sorted(buckets.items()):
            ok = [(r, c) for r, c in rc if r["pm25_ugm3"] != ""]
            rep_utc = collections.Counter(r["hour_start_utc"] for r, _ in rc).most_common(1)[0][0]
            rep_local = next(r["hour_start_local"] for r, _ in rc if r["hour_start_utc"] == rep_utc)
            missing = [names[c["location_id"]] for c in st["components"]
                       if c["location_id"] not in {cc["location_id"] for _, cc in ok}]
            base = {"target": key, "hour_start_local": rep_local, "hour_start_utc": rep_utc,
                    "station": st["name"], "openaq_location_id": st["location_id"], "provider": "",
                    "source_url": urls, "fetched_at_local": fetched,
                    "monitors_missing": "; ".join(missing)}
            if len(ok) < min_monitors:
                if b <= settled:
                    out.append({**base, "status": f"insufficient: {len(ok)} of {n_total} monitors reported"})
                continue
            p25 = [float(r["pm25_ugm3"]) for r, _ in ok]
            w = [1 / dist[c["location_id"]] ** 2 for _, c in ok]
            ok10 = [(float(r["pm10_ugm3"]), 1 / dist[c["location_id"]] ** 2) for r, c in ok if r["pm10_ugm3"] != ""]
            has10 = len(ok10) >= min_monitors
            vals = {
                "median": (statistics.median(p25), statistics.median(v for v, _ in ok10) if has10 else None),
                "mean":   (statistics.fmean(p25), statistics.fmean(v for v, _ in ok10) if has10 else None),
                "idw":    (sum(a * b for a, b in zip(p25, w)) / sum(w),
                           sum(v * ww for v, ww in ok10) / sum(ww for _, ww in ok10) if has10 else None),
            }
            row = {**base, "status": "ok", "pm25_coverage_pct": round(len(ok) / n_total * 100),
                   "pm10_coverage_pct": round(len(ok10) / n_total * 100),
                   "pm25_sensor_id": "", "pm10_sensor_id": "",
                   "monitors_used": "; ".join(f"{names[c['location_id']]} {r['pm25_ugm3']}" for r, c in ok),
                   "spread_pm25": round(max(p25) - min(p25), 1)}
            for m, (v25, v10) in vals.items():
                c25, c10, ca = METHOD_COLS[m]
                v25 = round(v25, 1)
                v10 = round(v10, 1) if v10 is not None else None
                aqi, dom = overall_aqi(v25, v10)
                row[c25], row[c10] = v25, (v10 if v10 is not None else "")
                row[ca] = aqi if aqi is not None else ""
                if m == "median":
                    row["dominant"], row["aqi_category"] = dom or "", category(aqi) if aqi is not None else ""
            out.append(row)
        none = missing_rows(key, {"name": st["name"], "location_id": st["location_id"]},
                            {r["hour_start_utc"] for r in out}, frm, settled, fetched,
                            f"insufficient: 0 of {n_total} monitors reported")
        for r in none:
            r["source_url"] = urls
            r["monitors_missing"] = "; ".join(names.values())
        return out + none, comp_rows

    def _waqi_median(self, spec, st):
        """WAQI's live AQI for the same monitors (those WAQI carries), as a median."""
        lat, lng, d = spec["lat"], spec["lng"], 12 / 111
        hints = []
        for c in st["components"]:
            h = _short_name(c["name"]).lower()
            if h and h not in hints:
                hints.append(h)
        picked = {}
        for s in self.waqi.stations_in_bounds([lat - d, lng - d, lat + d, lng + d]):
            info = s.get("station") or {}
            name = str(info.get("name") or "")
            a = _num(s.get("aqi"))
            if a is None:
                continue
            for h in hints:
                if h in name.lower() and h not in picked:
                    picked[h] = (name, a, parse_dt(info.get("time")), _num(s.get("aqi_waqi")))
        if not picked:
            return None
        times = [v[2] for v in picked.values() if v[2]]
        t = max(times).astimezone().strftime("%Y-%m-%d %H:%M:%S") if times else ""
        raw = [v[3] for v in picked.values() if v[3] is not None]
        return {"aqi": round(statistics.median(v[1] for v in picked.values())), "uid": "median",
                "aqi_waqi": round(statistics.median(raw)) if raw else "",
                "station": f"Median of {len(picked)} nearby monitors (WAQI)", "time": t,
                "iaqi": {}, "dominant": "", "url": "",
                "parts": "; ".join(f"{_short_name(v[0])} {int(v[1])}" for v in picked.values())}

    def _worst_station(self, key, spec):
        """Rank every reference monitor in the city by 30-day mean PM2.5; pin the worst for this month."""
        now = self.now()
        month = now.astimezone().strftime("%Y-%m")
        sig = f"worst|{spec['lat']:.3f},{spec['lng']:.3f},{spec['radius_km']},{spec.get('name_filter') or ''}"
        cached = self.state["stations"].get(key)
        if cached and cached.get("sig") == sig and cached.get("month") == month and not self._stale(key, cached):
            return cached

        locs = self.openaq.locations_near_raw(spec["lat"], spec["lng"],
                                              radius_m=spec["radius_km"] * 1000, limit=1000)
        filt = (spec.get("name_filter") or "").lower()
        cands = [c for c in station_candidates(locs, spec["lat"], spec["lng"], now, monitors_only=True)
                 if not filt or filt in str(c["name"]).lower()]
        frm = now - timedelta(days=30)
        ranked = []
        for c in cands:
            for sid in c["pm25_sensor_ids"]:
                try:
                    res = self.openaq.sensor_hours(sid, iso_z(frm), iso_z(now))
                except Exception as e:
                    log.info("recorder: ranking skipped %s: %s", c["name"], e)
                    res = []
                time.sleep(1.1)                       # stay well under OpenAQ's 60 req/min
                vals = [v for v in (_valid_conc(_g(r, "value")) for r in res) if v is not None]
                if len(vals) >= 360:                  # need >= half the hours to rank fairly
                    c["pm25_sensor_id"] = sid
                    ranked.append((statistics.fmean(vals), c))
                    break
        if not ranked:
            if cached:
                return cached
            raise RuntimeError(f"Couldn't rank any {filt or ''} monitors on OpenAQ")
        ranked.sort(key=lambda t: -t[0])
        mean, st = ranked[0]
        first = not (cached and str(cached.get("sig", "")).startswith("worst|"))
        month_start = now.astimezone().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        st.update(sig=sig, month=month, resolved_at=iso_z(now), mean_pm25_30d=round(mean, 1),
                  ranking=[[c["name"], round(m, 1)] for m, c in ranked[:5]],
                  # first pick back-fills the window it was ranked on; later picks start at their month
                  valid_from=iso_z(now - timedelta(days=int(self.rcfg.get("backfill_days", 31))))
                  if first else iso_z(month_start))
        if not cached or cached.get("location_id") != st["location_id"]:
            self.state["last_hour"].pop(key, None)
            self._switched.add(key)
        self.state["stations"][key] = st
        log.info("recorder: worst 30-day monitor = %s (%.0f µg/m³); top 5: %s", st["name"], mean, st["ranking"])
        return st

    def _fallback_due(self, spec, st):
        """Pinned to a fallback because the named monitor was offline? Look for it again every 6 h."""
        hint = (spec.get("name") or "").lower()
        if not hint or hint in str(st.get("name", "")).lower():
            return False
        resolved = parse_dt(st.get("resolved_at"))
        return resolved is None or self.now() - resolved > timedelta(hours=6)

    def _hours(self, st, param, frm, now):
        """Hourly data for a parameter; if the pinned sensor returns nothing, try the
        location's other sensors for that parameter (stations often carry a dead legacy one)."""
        current = st.get(f"{param}_sensor_id")
        ids = [current] + [i for i in st.get(f"{param}_sensor_ids", []) if i != current]
        for sid in [i for i in ids if i is not None]:
            res = self.openaq.sensor_hours(sid, iso_z(frm), iso_z(now))
            if any(_valid_conc(_g(r, "value")) is not None for r in res):
                if sid != current:
                    log.info("recorder: %s %s sensor %s -> %s", st.get("name"), param, current, sid)
                    st[f"{param}_sensor_id"] = sid
                return res
        return []

    def _waqi_reading(self, key, spec, st):
        """WAQI feed for the same monitor: match by station name nearby, else nearest to its coords."""
        lat, lng = (st["lat"], st["lng"]) if st else (spec["lat"], spec["lng"])
        hint = (spec.get("name") or _short_name(st.get("name") if st else "")).lower()
        sig = f"{lat:.4f},{lng:.4f},{hint}"
        cache = self.state.setdefault("waqi", {}).get(key)
        if hint and not (cache and cache.get("sig") == sig):
            uid = None
            try:
                for s in self.waqi.stations_in_bounds([lat - 0.05, lng - 0.05, lat + 0.05, lng + 0.05]):
                    name = str((s.get("station") or {}).get("name") or "").lower()
                    if hint in name:
                        uid = s.get("uid")
                        break
            except Exception as e:
                log.info("recorder: WAQI station search failed: %s", e)
            cache = {"sig": sig, "uid": uid}
            self.state["waqi"][key] = cache
        if cache and cache.get("uid"):
            w = self.waqi.by_uid(cache["uid"])
            if w:
                return w
        return self.waqi.by_geo(lat, lng)

    def _stale(self, key, st):
        """Re-pick the station if it has produced nothing for 48 h (station went offline)."""
        last = parse_dt(self.state["last_hour"].get(key))
        resolved = parse_dt(st.get("resolved_at"))
        now = self.now()
        return bool(last and resolved and now - last > timedelta(hours=48)
                    and now - resolved > timedelta(hours=24))

    # ── main entry ──────────────────────────────────────────────────────────
    def poll(self):
        now = self.now()
        status = {"ok": True, "added": 0, "updated": 0, "errors": [], "targets": {},
                  "at": local_iso(now)}
        backfill_days = int(self.rcfg.get("backfill_days", 31))
        today = now.astimezone().strftime("%Y-%m-%d")
        sweep = self.state.get("last_sweep") != today          # once a day: re-check last 7 days
        last_full = parse_dt(self.state.get("last_full_sweep"))
        full = last_full is None or now - last_full > timedelta(days=7)   # weekly: whole window, in case
                                                                         # OpenAQ back-filled an outage
        months = {now.astimezone().strftime("%Y-%m")}

        for key, spec in self.targets().items():
            info = {"label": spec["label"]}
            st = None
            status["targets"][key] = info
            try:
                st = self._station(key, spec)
                info["station"] = st["name"]
                last = parse_dt(self.state["last_hour"].get(key))
                frm = now - timedelta(hours=48)
                if sweep:
                    frm = now - timedelta(days=7)
                if full:
                    frm = now - timedelta(days=backfill_days)
                if last is None:
                    frm = now - timedelta(days=backfill_days)
                elif last < frm:
                    frm = last - timedelta(hours=1)
                frm = max(frm, now - timedelta(days=backfill_days))
                vf = parse_dt(st.get("valid_from"))
                if vf:                                    # don't overwrite last month's worst station
                    frm = max(frm, vf)

                if st.get("mode") == "median":
                    rows, comp_rows = self._median_rows(key, st, frm, now, spec.get("min_monitors", 2))
                    if comp_rows:                     # each monitor's own readings, kept for transparency
                        months |= self.store.upsert_hourly(comp_rows)[2]
                else:
                    pm25 = self._hours(st, "pm25", frm, now)
                    pm10 = self._hours(st, "pm10", frm, now)
                    rows = build_hourly_rows(key, st, pm25, pm10, now)
                    for r in rows:
                        r["status"] = "ok"
                    rows += missing_rows(key, st, {r["hour_start_utc"] for r in rows},
                                         max(frm, vf) if vf else frm, now - timedelta(hours=3), local_iso(now),
                                         "no data: app asked OpenAQ for this hour, nothing returned")
                if vf:
                    rows = [r for r in rows if parse_dt(r["hour_start_utc"]) >= vf]
                gone, gm = self.store.purge_foreign(key, st["location_id"], frm)
                if gone:
                    log.info("recorder: removed %d %s rows from a previous station", gone, key)
                    months |= gm
                if rows:
                    a, u, m = self.store.upsert_hourly(rows)
                    status["added"] += a
                    status["updated"] += u
                    months |= m
                    real = [r for r in rows if r.get("status") == "ok"]
                    if real:
                        self.state["last_hour"][key] = max(r["hour_start_utc"] for r in real)
                        latest = max(real, key=lambda r: r["hour_start_utc"])
                        info.update(latest_hour=latest["hour_start_local"], aqi=latest["aqi_us"],
                                    pm25=latest["pm25_ugm3"])
                info["rows"] = len([r for r in rows if r.get("status") == "ok"])
            except PermissionError as e:
                status["errors"].append(f"{key}: CSV is open in another program (Excel?) — will retry. {e}")
            except Exception as e:
                status["errors"].append(f"{key}/OpenAQ: {e}")

            if key in self._switched:            # old monitor's cross-check rows no longer apply
                try:
                    n = self.store.purge_live(key, now.astimezone().strftime("%Y-%m"))
                    if n:
                        log.info("recorder: removed %d WAQI rows of the previous %s monitor", n, key)
                except PermissionError:
                    pass
                self._switched.discard(key)
            if self.waqi:
                try:
                    # query WAQI at the pinned monitor's coordinates so both sources
                    # describe the same physical station
                    w = (self._waqi_median(spec, st) if st and st.get("mode") == "median"
                         else self._waqi_reading(key, spec, st))
                    if w and w.get("aqi") not in (None, "-"):
                        self.store.append_live({
                            "target": key, "polled_at_local": local_iso(now),
                            "station_time_local": str(w.get("time") or ""),
                            "waqi_station": w.get("station") or "", "waqi_uid": w.get("uid") or "",
                            "waqi_aqi": w.get("aqi"),
                            "waqi_pm25_subindex": (w.get("iaqi") or {}).get("pm25", ""),
                            "waqi_dominant": w.get("dominant") or "", "waqi_url": w.get("url") or "",
                            "waqi_parts": w.get("parts", ""),
                            "waqi_aqi_raw": w.get("aqi_waqi", ""), "aqi_scale": "us2024",
                        })
                        info["waqi_aqi"] = w.get("aqi")
                except PermissionError as e:
                    status["errors"].append(f"{key}: live CSV locked — {e}")
                except Exception as e:
                    status["errors"].append(f"{key}/WAQI: {e}")

        for m in sorted(months):
            try:
                self.store.rebuild_daily(m)
            except PermissionError as e:
                status["errors"].append(f"daily_{m}.csv locked — {e}")

        if sweep and not status["errors"]:
            self.state["last_sweep"] = today
        if full and not status["errors"]:
            self.state["last_full_sweep"] = iso_z(now)
        try:
            self._save_state()
        except Exception as e:
            status["errors"].append(f"state: {e}")
        status["ok"] = not status["errors"]
        log.info("recorder poll: +%d rows, %d updated, errors=%s",
                 status["added"], status["updated"], status["errors"])
        return status


def _short_name(name):
    return str(name or "").split(",")[0].strip()


def _label(name):
    """'Pusa, Delhi - DPCC' -> 'Pusa (DPCC)' so the two Pusa monitors stay distinguishable."""
    n = str(name or "")
    agency = n.rsplit(" - ", 1)[1].strip() if " - " in n else ""
    return f"{_short_name(n)} ({agency})" if agency else _short_name(n)


def station_candidates(locs, lat, lng, now, monitors_only=False):
    cands = []
    for loc in locs:
        if _g(loc, "isMobile", "is_mobile"):
            continue
        if monitors_only and _g(loc, "isMonitor", "is_monitor") is False:   # skip low-cost sensors
            continue
        sensors = {}
        for s in _g(loc, "sensors", default=[]) or []:
            p = _g(s, "parameter", default={}) or {}
            pname = str(_g(p, "name", default="")).lower().replace(".", "")
            units = str(_g(p, "units", default="")).lower()
            if pname in ("pm25", "pm10") and ("g/m" in units or units == "") and _g(s, "id") is not None:
                sensors.setdefault(pname, []).append(_g(s, "id"))
        if "pm25" not in sensors:
            continue
        last = parse_dt(_g(_g(loc, "datetimeLast", "datetime_last", default={}) or {}, "utc"))
        if last and now - last > timedelta(days=3):
            continue
        c = _g(loc, "coordinates", default={}) or {}
        clat, clng = _num(_g(c, "latitude")), _num(_g(c, "longitude"))
        if clat is None or clng is None:
            continue
        cands.append({
            "location_id": _g(loc, "id"),
            "name": _g(loc, "name", "locality", default="Unknown"),
            "provider": _g(_g(loc, "provider", default={}) or {}, "name", default=""),
            "lat": clat, "lng": clng,
            "distance_km": round(haversine(lat, lng, clat, clng), 2),
            "pm25_sensor_ids": sorted(sensors["pm25"], reverse=True),
            "pm10_sensor_ids": sorted(sensors.get("pm10", []), reverse=True),
            "pm25_sensor_id": max(sensors["pm25"]),
            "pm10_sensor_id": max(sensors["pm10"]) if sensors.get("pm10") else None,
        })
    return cands


def pick_station(locs, lat, lng, name_hint, now):
    cands = station_candidates(locs, lat, lng, now)
    if not cands:
        return None
    if name_hint:
        hint = name_hint.lower()
        named = [c for c in cands if hint in str(c["name"]).lower()]
        if named:
            return min(named, key=lambda c: c["distance_km"])
    return min(cands, key=lambda c: c["distance_km"])


def missing_rows(target, st, have, frm, until, fetched, status):
    """Rows for hours in [frm, until] that a monitor didn't return, aligned to its usual minute."""
    minute = 30
    for h in have:
        minute = parse_dt(h).minute
        break
    t = frm.astimezone(timezone.utc).replace(minute=minute, second=0, microsecond=0)
    if t < frm:
        t += timedelta(hours=1)
    out = []
    while t <= until:
        k = iso_z(t)
        if k not in have:
            out.append({"target": target, "hour_start_local": t.astimezone().isoformat(timespec="seconds"),
                        "hour_start_utc": k, "station": st.get("name", ""),
                        "openaq_location_id": st.get("location_id", ""), "provider": st.get("provider", ""),
                        "source_url": EXPLORE_URL.format(st["location_id"]) if st.get("location_id") else "",
                        "fetched_at_local": fetched, "status": status})
        t += timedelta(hours=1)
    return out


def _hour_map(results):
    out = {}
    for r in results or []:
        period = _g(r, "period", default={}) or {}
        start = _g(period, "datetimeFrom", "datetime_from", default={}) or {}
        utc = parse_dt(_g(start, "utc"))
        if not utc:
            continue
        cov = _g(r, "coverage", default={}) or {}
        out[iso_z(utc)] = {
            "value": _valid_conc(_g(r, "value")),
            "local": _g(start, "local"),
            "coverage": _num(_g(cov, "percentComplete", "percent_complete",
                                "percentCoverage", "percent_coverage")),
        }
    return out


def build_hourly_rows(target, st, pm25_results, pm10_results, now):
    h25, h10 = _hour_map(pm25_results), _hour_map(pm10_results)
    fetched = local_iso(now)
    rows = []
    for hour in sorted(set(h25) | set(h10)):
        a, b = h25.get(hour, {}), h10.get(hour, {})
        pm25, pm10 = a.get("value"), b.get("value")
        if pm25 is None and pm10 is None:
            continue
        local = a.get("local") or b.get("local")
        local_dt = parse_dt(local) if local else None
        local_s = (local_dt or parse_dt(hour).astimezone()).isoformat(timespec="seconds")
        aqi, dom = overall_aqi(pm25, pm10)
        rows.append({
            "target": target, "hour_start_local": local_s, "hour_start_utc": hour,
            "station": st["name"], "openaq_location_id": st["location_id"], "provider": st.get("provider", ""),
            "pm25_ugm3": round(pm25, 1) if pm25 is not None else "",
            "pm25_coverage_pct": a.get("coverage") if a.get("coverage") is not None else "",
            "pm10_ugm3": round(pm10, 1) if pm10 is not None else "",
            "pm10_coverage_pct": b.get("coverage") if b.get("coverage") is not None else "",
            "aqi_us": aqi if aqi is not None else "", "dominant": dom or "",
            "aqi_category": category(aqi) if aqi is not None else "",
            "pm25_sensor_id": st["pm25_sensor_id"], "pm10_sensor_id": st.get("pm10_sensor_id") or "",
            "source_url": EXPLORE_URL.format(st["location_id"]),
            "fetched_at_local": fetched,
        })
    return rows
