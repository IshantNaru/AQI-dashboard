"""
Spike alerts for "your area".

Checked after every recorder sync (every 15 min). Two signals:
  • hourly PM2.5 (OpenAQ median of your area's monitors) — accurate, but ~1–2 h late
  • WAQI live AQI (median of the same monitors WAQI carries) — earlier warning

An alert fires when:
  • PM2.5 rises by ≥ pm25_jump µg/m³ from one hour to the next, or
  • PM2.5 crosses up through pm25_level, or
  • WAQI live AQI rises by ≥ waqi_aqi_jump between readings, or crosses up through waqi_aqi_level.
  • City-wide: the median live AQI of all monitors in your city (recorded each sync by surge.py) rises by
    ≥ city_aqi_jump within ~1 h, or crosses city_aqi_level.
Every spike alert carries a "Likely cause" line from surge.explain() (festival, city-wide vs local,
wind/mixing height, particle mix, stubble fires).
A follow-up "air improving" notice fires once PM2.5 falls back below pm25_clear.

Each hour / live reading is judged only once (remembered in data/alerts_state.json, so a
restart never repeats an alert), and only fresh data counts (hour started within 4 h,
live reading within 3 h) — back-filling old hours after the laptop was off never alerts.
Every alert is also written to data/alerts.csv.

Config (config.json → "alerts"), all optional:
  {"enabled": true, "sound": true, "pm25_jump": 100, "pm25_level": 250,
   "pm25_clear": 150, "waqi_aqi_jump": 100, "waqi_aqi_level": 300,
   "city_aqi_jump": 80, "city_aqi_level": 300, "explain": true}
"""
import csv
import json
import os
from datetime import datetime, timedelta, timezone

from recorder import Store, parse_dt, _num
from aqi_math import live_row_aqi


class AlertManager:
    def __init__(self, config, store=None):
        a = config.get("alerts") or {}
        self.enabled = a.get("enabled", True)
        self.sound = a.get("sound", True)
        self.pm25_jump = float(a.get("pm25_jump", 100))
        self.pm25_level = float(a.get("pm25_level", 250))
        self.pm25_clear = float(a.get("pm25_clear", 150))
        self.aqi_jump = float(a.get("waqi_aqi_jump", 100))
        self.aqi_level = float(a.get("waqi_aqi_level", 300))
        self.city_jump = float(a.get("city_aqi_jump", 80))
        self.city_level = float(a.get("city_aqi_level", 300))
        self.explain = a.get("explain", True)
        self.city = (config.get("location") or {}).get("city") or "your city"
        self.store = store or Store()
        self.state_path = os.path.join(self.store.dir, "alerts_state.json")
        try:
            with open(self.state_path, encoding="utf-8") as f:
                self.state = json.load(f)
        except (OSError, ValueError):
            self.state = {"seen": [], "active": False}

    # ── helpers ──────────────────────────────────────────────────────────────
    def _months(self, now):
        m1 = now.astimezone().strftime("%Y-%m")
        m0 = (now.astimezone().replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
        return [m0, m1]

    def _hourly(self, now):
        rows = [r for m in self._months(now) for r in self.store.read(self.store.path("readings", m))
                if r["target"] == "home" and _num(r.get("pm25_ugm3")) is not None]
        return sorted(rows, key=lambda r: r["hour_start_utc"])

    def _live(self, now):
        rows = [r for m in self._months(now) for r in self.store.read(self.store.path("live", m))
                if r["target"] == "home" and live_row_aqi(r) is not None and r.get("station_time_local")]
        uniq = {}
        for r in rows:
            uniq[r["station_time_local"]] = r
        return [uniq[k] for k in sorted(uniq)]

    def _seen(self, key):
        if key in self.state["seen"]:
            return True
        self.state["seen"] = (self.state["seen"] + [key])[-300:]
        return False

    # ── main ─────────────────────────────────────────────────────────────────
    def check(self, now=None, explainer=None):
        """Returns a list of alerts: {"kind": "spike"|"clear", "title", "body", "time"}.
        explainer(trigger) -> {"summary": ...} is called once if any spike fired."""
        if not self.enabled:
            return []
        now = now or datetime.now(timezone.utc)
        out = []

        hourly = self._hourly(now)
        if hourly:
            last = hourly[-1]
            t = parse_dt(last["hour_start_utc"])
            if now - t <= timedelta(hours=4) and not self._seen("h:" + last["hour_start_utc"]):
                p = float(last["pm25_ugm3"])
                prev = None
                if len(hourly) > 1 and t - parse_dt(hourly[-2]["hour_start_utc"]) <= timedelta(hours=1, minutes=5):
                    prev = float(hourly[-2]["pm25_ugm3"])
                hr = t.astimezone().strftime("%H:%M")
                why = []
                if prev is not None and p - prev >= self.pm25_jump:
                    why.append(f"jumped {prev:.0f} → {p:.0f} µg/m³ in one hour (+{p - prev:.0f})")
                if p >= self.pm25_level and (prev is None or prev < self.pm25_level):
                    why.append(f"crossed {self.pm25_level:.0f} µg/m³ (now {p:.0f}, AQI {last.get('aqi_us')})")
                if why:
                    out.append({"kind": "spike", "title": f"⚠ PM2.5 spike in your area ({hr} hour)",
                                "body": "; ".join(why) + ". Close windows, purifiers to max."})
                    self.state["active"] = True
                elif self.state.get("active") and p < self.pm25_clear:
                    out.append({"kind": "clear", "title": "Air improving in your area",
                                "body": f"PM2.5 back down to {p:.0f} µg/m³ for the {hr} hour."})
                    self.state["active"] = False

        live = self._live(now)
        if len(live) >= 1:
            cur = live[-1]
            try:
                ct = datetime.fromisoformat(cur["station_time_local"]).astimezone()
            except ValueError:
                ct = None
            if ct and now - ct <= timedelta(hours=3) and not self._seen("w:" + cur["station_time_local"]):
                a = live_row_aqi(cur)
                pa = None
                if len(live) > 1:
                    try:
                        pt = datetime.fromisoformat(live[-2]["station_time_local"]).astimezone()
                        if ct - pt <= timedelta(hours=3):
                            pa = live_row_aqi(live[-2])
                    except ValueError:
                        pass
                why = []
                if pa is not None and a - pa >= self.aqi_jump:
                    why.append(f"WAQI live AQI jumped {pa:.0f} → {a:.0f}")
                if a >= self.aqi_level and (pa is None or pa < self.aqi_level):
                    why.append(f"WAQI live AQI crossed {self.aqi_level:.0f} (now {a:.0f})")
                if why:
                    out.append({"kind": "spike", "title": f"⚠ Air getting worse now ({ct:%H:%M})",
                                "body": "; ".join(why) + " — early warning; hourly PM2.5 confirms in ~1–2 h."})
                    self.state["active"] = True

        try:
            from surge import city_series
            cs = city_series(self.store.dir, now.astimezone(), hours=3)
        except Exception:
            cs = []
        if cs and now - cs[-1][0] <= timedelta(hours=1) and not self._seen("c:" + cs[-1][0].isoformat()):
            t1, v1 = cs[-1]
            prev = [(t, v) for t, v in cs if timedelta(minutes=40) <= t1 - t <= timedelta(minutes=80)]
            why = []
            if prev and v1 - prev[0][1] >= self.city_jump:
                why.append(f"{self.city}-wide median AQI jumped {prev[0][1]:.0f} → {v1:.0f} in about an hour")
            low = [v for t, v in cs if t < t1]
            if v1 >= self.city_level and low and low[-1] < self.city_level:
                why.append(f"{self.city}-wide median AQI crossed {self.city_level:.0f} (now {v1:.0f})")
            if why:
                out.append({"kind": "spike", "title": f"⚠ Air worsening across {self.city} ({t1:%H:%M})",
                            "body": "; ".join(why) + "."})
                self.state["active"] = True

        if explainer and self.explain and any(x["kind"] == "spike" for x in out):
            try:
                ex = explainer(", ".join(x["title"] for x in out if x["kind"] == "spike"))
                for x in out:
                    if x["kind"] == "spike":
                        x["body"] += f" {ex['summary']}"
                        x["reasons"] = ex.get("reasons", [])
            except Exception as e:
                for x in out:
                    if x["kind"] == "spike":
                        x["body"] += f" (cause check failed: {str(e)[:60]})"

        stamp = now.astimezone().isoformat(timespec="seconds")
        for x in out:
            x["time"] = stamp
        self._save(out)
        return out

    def _save(self, alerts):
        tmp = self.state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.state, f)
        os.replace(tmp, self.state_path)
        if alerts:
            path = os.path.join(self.store.dir, "alerts.csv")
            new = not os.path.exists(path)
            with open(path, "a", encoding="utf-8-sig" if new else "utf-8", newline="") as f:
                w = csv.DictWriter(f, fieldnames=["time", "kind", "title", "body"], extrasaction="ignore")
                if new:
                    w.writeheader()
                w.writerows(alerts)
