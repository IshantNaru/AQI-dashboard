"""
Indoor air logging from Xiaomi (MiOT) purifiers on your Wi-Fi.

Every reading becomes one row in data/indoor_YYYY-MM.csv:
  date, time, timestamp, device, room, power, mode, fan_level, pm25_ugm3, humidity_pct,
  filter_left_pct (read from the purifier if the model exposes it), filter_left_pct_manual
  (blank — type the Mi Home app's % here whenever you like), outdoor references, event,
  standards comment, suggestion, status, note (blank — free text for you).

The file is only ever APPENDED to, never rewritten, so your manual edits (room, notes,
filter %) are kept. If the CSV is open in Excel, new rows wait in memory and are written
at the next poll after you close it.

Polling: every `poll_on_s` seconds while a purifier is on (default 30 s — fast enough to
draw the clean-down curve and catch a door opening), every `poll_off_s` while it's off.

Config (config.json → "indoor"):
{
  "poll_on_s": 30, "poll_off_s": 120, "spike_ugm3": 15,
  "devices": [
    {"name": "Mi 4 Lite", "ip": "192.168.1.50", "token": "…32 hex…", "room": "Bedroom",
     "props": {"pm25": [3, 4], "humidity": [3, 1], "power": [2, 1], "mode": [2, 4],
               "fan_level": null, "filter_left_pct": null}}
  ]
}
The current room can also be changed from the app; it is then remembered in data/indoor_rooms.json.

On/off: the "Indoor logging" switch in the app (or tray menu) pauses everything — no Wi-Fi polling,
no rows. One marker row per purifier ("logging paused by you" / "logging resumed") is written so
the gap in the CSV is clearly intentional. The choice is remembered in data/indoor_settings.json.

Out of reach (laptop away from home, purifier unplugged): after 10 min unreachable the app polls
only every `poll_away_s` (default 300 s) and writes an "unreachable" row at most every 30 min.
"""
import csv
import json
import os
import statistics
import time
from datetime import datetime, timedelta

from recorder import DATA_DIR

FIELDS = ["date", "time", "timestamp", "device", "room", "power", "mode", "fan_level",
          "pm25_ugm3", "humidity_pct", "filter_left_pct", "filter_left_pct_manual",
          "outdoor_pm25_openaq", "outdoor_openaq_hour", "outdoor_waqi_aqi_live",
          "indoor_outdoor_ratio", "event", "standards", "suggestion", "status", "note"]

DEFAULT_PROPS = {"pm25": [3, 4], "humidity": [3, 1], "power": [2, 1], "mode": [2, 4],
                 "fan_level": None, "filter_left_pct": None}
MODES = {0: "auto", 1: "sleep", 2: "favourite", 3: "manual"}

# PM2.5 reference values (µg/m³). Guidelines/limits are averages over the stated period, so a
# single reading is compared as an indication, not a formal exceedance.
WHO_24H = 15        # WHO 2021 air quality guideline, 24-hour
EU_DAILY = 25       # EU directive 2024/2881 daily limit (from 2030)
US_24H = 35         # US EPA 24-hour standard
INDIA_24H = 60      # India NAAQS 24-hour


def us_category(pm):
    from aqi_math import pm25_to_aqi, category
    a = pm25_to_aqi(pm)
    return a, category(a)


def standards_comment(pm):
    if pm is None:
        return "", ""
    aqi, cat = us_category(pm)
    parts = [f"US AQI {aqi} ({cat})", f"{pm / WHO_24H:.1f}× WHO 24-h guideline (15)"]
    parts.append("within EU daily limit (25)" if pm <= EU_DAILY else f"above EU daily limit 25 (×{pm / EU_DAILY:.1f})")
    parts.append("within India 24-h standard (60)" if pm <= INDIA_24H else "above India 24-h standard (60)")
    if pm <= WHO_24H:
        tip = "Good — meets the WHO guideline."
    elif pm <= EU_DAILY:
        tip = "Acceptable by EU/US limits; WHO advises lower. Keep purifier on low/auto."
    elif pm <= 55:
        tip = "Moderate–USG. Run purifier on a higher fan level; keep doors closed."
    elif pm <= 125:
        tip = "Unhealthy indoors. Max fan, close doors/windows, avoid cooking smoke/incense."
    else:
        tip = "Very unhealthy indoors. Max fan, seal gaps, add a second purifier, limit time here."
    return "; ".join(parts), tip


class IndoorDevice:
    def __init__(self, spec):
        self.name = spec.get("name") or spec["ip"]
        self.ip, self.token = spec["ip"], spec["token"]
        self.default_room = spec.get("room", "Bedroom")
        self.props = {**DEFAULT_PROPS, **(spec.get("props") or {})}
        self._dev = None

    def read(self, retries=3):
        """Returns ({prop: value}, None) or (None, error text). Retries ride out Wi-Fi power-save."""
        from miio import Device
        req = [{"did": k, "siid": v[0], "piid": v[1]} for k, v in self.props.items() if v]
        err = None
        for attempt in range(retries):
            try:
                if self._dev is None:
                    self._dev = Device(self.ip, self.token, timeout=4)
                res = self._dev.send("get_properties", req)
                return {r["did"]: r.get("value") for r in res if r.get("code") == 0}, None
            except Exception as e:
                err = str(e)
                self._dev = None                       # fresh handshake next attempt
                time.sleep(0.6 * (attempt + 1))
        low = (err or "").lower()
        if "token" in low or "checksum" in low or "decrypt" in low:
            err = "token rejected — re-run the extractor (token changes after reset/re-pair)"
        return None, f"unreachable: {err}"


class IndoorLogger:
    def __init__(self, config, data_dir=DATA_DIR, device_factory=IndoorDevice):
        cfg = config.get("indoor") or {}
        self.poll_on_s = int(cfg.get("poll_on_s", 30))
        self.poll_off_s = int(cfg.get("poll_off_s", 120))
        self.spike = float(cfg.get("spike_ugm3", 15))
        self.poll_away_s = int(cfg.get("poll_away_s", 300))
        self.dir = data_dir
        os.makedirs(data_dir, exist_ok=True)
        self.devices = [device_factory(d) for d in cfg.get("devices", []) if d.get("ip") and d.get("token")]
        self.rooms_path = os.path.join(data_dir, "indoor_rooms.json")
        try:
            with open(self.rooms_path, encoding="utf-8") as f:
                self.rooms = json.load(f)
        except (OSError, ValueError):
            self.rooms = {}
        self.history = {d.name: [] for d in self.devices}     # recent (time, pm, power) for events
        self.last = {}                                         # latest row per device (for the UI)
        self.pending = []                                      # rows waiting for a locked CSV
        self.unreachable_since = {}
        self.off_since = {}                                    # when each purifier was switched off
        self.last_spike = {}                                   # merge one door-opening into one event
        self.last_unreach_row = {}
        self.last_rise_t = {}
        self.settings_path = os.path.join(data_dir, "indoor_settings.json")
        try:
            with open(self.settings_path, encoding="utf-8") as f:
                self.enabled = bool(json.load(f).get("enabled", True))
        except (OSError, ValueError):
            self.enabled = bool(cfg.get("enabled", True))

    def set_enabled(self, on, now=None):
        """Pause/resume logging from the app. Writes one marker row per purifier."""
        on = bool(on)
        if on == self.enabled:
            return
        self.enabled = on
        with open(self.settings_path, "w", encoding="utf-8") as f:
            json.dump({"enabled": on}, f)
        now = now or datetime.now().astimezone()
        rows = [{"date": now.strftime("%Y-%m-%d"), "time": now.strftime("%H:%M:%S"),
                 "timestamp": now.isoformat(timespec="seconds"), "device": d.name, "room": self.room(d),
                 "status": "resumed" if on else "paused",
                 "event": "logging resumed by you" if on else "logging paused by you (no readings until resumed)"}
                for d in self.devices]
        # history restarts after a pause, so off-durations/spikes never span the gap
        self.history = {d.name: [] for d in self.devices}
        self.off_since, self.unreachable_since, self.last_unreach_row = {}, {}, {}
        self.last = {}
        self._append(rows, now)
    def room(self, dev):
        return self.rooms.get(dev.name, dev.default_room)

    def set_room(self, name, room):
        self.rooms[name] = room
        with open(self.rooms_path, "w", encoding="utf-8") as f:
            json.dump(self.rooms, f)

    def next_interval(self, now=None):
        now = now or datetime.now().astimezone()
        on = any(str(r.get("power")) == "on" for r in self.last.values())
        if on or not self.last:
            return self.poll_on_s
        if self.devices and all(d.name in self.unreachable_since and
                                now - self.unreachable_since[d.name] >= timedelta(minutes=10)
                                for d in self.devices):
            return self.poll_away_s                    # nobody home / laptop elsewhere: poll lightly
        return self.poll_off_s

    def _events(self, dev, now, pm, power):
        h = self.history[dev.name]
        ev = []
        if not h and power == "off":                   # app started while it was off: lower bound only
            self.off_since[dev.name] = ("at least", now)
        if h:
            pt, ppm, ppow = h[-1]
            if power == "on" and ppow == "off":
                kind, since = self.off_since.pop(dev.name, ("unknown", None))
                if kind == "exact":
                    ev.append(f"switched ON (start PM2.5 {pm}; was off {(now - since).total_seconds() / 60:.0f} min)")
                elif since:
                    ev.append(f"switched ON (start PM2.5 {pm}; off for at least "
                              f"{(now - since).total_seconds() / 60:.0f} min — app started while it was off)")
                else:
                    ev.append(f"switched ON (start PM2.5 {pm})")
            elif power == "off" and ppow == "on":
                self.off_since[dev.name] = ("exact", now)
                ev.append(f"switched OFF at PM2.5 {pm}")
            elif power == "on" and pm is not None:
                msg = self._sustained_rise(dev, h, now, pm)
                if msg:
                    ev.append(msg)
        h.append((now, pm, power))
        del h[:-40]
        return "; ".join(ev)

    def _sustained_rise(self, dev, h, now, pm):
        """A rise counts only if it lasts and stands out from the sensor's own jitter.
        current  = median of the last 3 readings (~1.5 min, so 2 of 3 must be high)
        baseline = median of readings 2–6 min earlier (all with the purifier on)
        threshold = max(spike_ugm3, 3 × the baseline's noise (scaled MAD))
        Not checked in the first 5 min after switch-on (fan start-up), and one rise is reported
        once — a new one only after the level has come back near its baseline."""
        on_run = []
        for t, v, p in reversed(h):
            if p != "on" or v is None:
                break
            on_run.append((t, v))
        on_run.reverse()
        if not on_run or now - on_run[0][0] < timedelta(minutes=5):
            return ""
        cur = statistics.median([v for _, v in on_run[-2:]] + [pm])
        base = [v for t, v in on_run if timedelta(minutes=2) <= now - t <= timedelta(minutes=6)]
        if len(base) < 4:
            return ""
        b = statistics.median(base)
        noise = 1.4826 * statistics.median([abs(v - b) for v in base])
        thr = max(self.spike, 3 * noise)
        state = self.last_spike.get(dev.name)
        if state and cur > state + thr / 2:
            return ""                                  # still inside the rise already reported
        if state:
            self.last_spike.pop(dev.name, None)        # back near baseline: re-arm
        last_t = self.last_rise_t.get(dev.name)
        if cur - b >= thr and not (last_t and now - last_t < timedelta(minutes=5)):
            self.last_spike[dev.name] = b
            self.last_rise_t[dev.name] = now
            jitter = f"sensor jitter ±{noise:.0f}" if noise >= 1 else "sensor was steady before"
            return (f"sustained rise {b:.0f} → {cur:.0f} µg/m³ (+{cur - b:.0f}; {jitter}) — "
                    "door/window opened, cooking/incense, or dust stirred up?")
        return ""

    def poll(self, outdoor=None, now=None):
        """outdoor: {"pm25": float, "hour": "HH:MM", "waqi_aqi": float} — latest known values."""
        now = now or datetime.now().astimezone()
        outdoor = outdoor or {}
        rows = []
        if not self.enabled:
            return rows
        for dev in self.devices:
            vals, err = dev.read()
            base = {"date": now.strftime("%Y-%m-%d"), "time": now.strftime("%H:%M:%S"),
                    "timestamp": now.isoformat(timespec="seconds"), "device": dev.name, "room": self.room(dev),
                    "outdoor_pm25_openaq": outdoor.get("pm25", ""), "outdoor_openaq_hour": outdoor.get("hour", ""),
                    "outdoor_waqi_aqi_live": outdoor.get("waqi_aqi", "")}
            if vals is None:
                first = dev.name not in self.unreachable_since
                self.unreachable_since.setdefault(dev.name, now)
                row = {**base, "status": err,
                       "event": "purifier not reachable (logging resumes automatically)" if first else ""}
                self.last[dev.name] = row
                lr = self.last_unreach_row.get(dev.name)
                if not first and lr and now - lr < timedelta(minutes=30):
                    rows.append({**row, "_skip": True})       # shown in the app, not written
                    continue
                self.last_unreach_row[dev.name] = now
            else:
                gone = self.unreachable_since.pop(dev.name, None)
                self.last_unreach_row.pop(dev.name, None)
                pm = vals.get("pm25")
                power = "on" if vals.get("power") else "off"
                std, tip = standards_comment(pm) if power == "on" else ("", "")
                out_pm = outdoor.get("pm25")
                row = {**base, "status": "ok", "power": power,
                       "mode": MODES.get(vals.get("mode"), vals.get("mode", "")),
                       "fan_level": vals.get("fan_level", ""), "pm25_ugm3": pm if pm is not None else "",
                       "humidity_pct": vals.get("humidity", ""), "filter_left_pct": vals.get("filter_left_pct", ""),
                       "indoor_outdoor_ratio": round(pm / out_pm, 2) if (pm is not None and out_pm) else "",
                       "event": "; ".join(x for x in (
                           f"reachable again after {(now - gone).total_seconds() / 60:.0f} min" if gone else "",
                           self._events(dev, now, pm, power)) if x),
                       "standards": std if power == "on" else "purifier off — sensor reading may not reflect room air",
                       "suggestion": tip}
            row.setdefault("filter_left_pct_manual", "")
            row.setdefault("note", "")
            self.last[dev.name] = row
            rows.append(row)
        self._append([r for r in rows if not r.get("_skip")], now)
        return rows

    def _append(self, rows, now):
        self.pending.extend(rows)
        path = os.path.join(self.dir, f"indoor_{now.strftime('%Y-%m')}.csv")
        new = not os.path.exists(path)
        try:
            with open(path, "a", encoding="utf-8-sig" if new else "utf-8", newline="") as f:
                w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
                if new:
                    w.writeheader()
                w.writerows(self.pending)
            self.pending = []
        except PermissionError:
            pass                                       # Excel has it open — try again next poll
        except OSError:
            del self.pending[:-5000]                   # disk/folder problem: keep a bounded backlog
