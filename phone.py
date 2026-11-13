"""
phone.py — air-quality messages to your phone through ntfy (free push notifications).

Setup: install the "ntfy" app (Android / iPhone), tap +, and subscribe to the topic name in
config.json → "phone" → "topic" (server: ntfy.sh). Anyone who knows the topic name can read the
messages, so it is a long random name. Messages contain AQI numbers and the name "your area" only.

What is sent:
  • a scheduled update at each time in "times" (default every 2 h: 00:00, 02:00 … 22:00), laid out
    like the dashboard cards — Your area / City / All sources, each AQI with PM2.5 under it — plus
    the purifier reading. Sent at ntfy priority 4 ("high"), which pops up on the phone screen;
    set "priority": 3 for quiet notifications in the shade only;
  • every spike alert the app raises (your area or city-wide), with its likely cause.

The app on your laptop sends them, so nothing arrives while the laptop is off or asleep. A slot
missed by up to "late_ok_min" minutes (default 90) is still sent, marked late; older ones are
skipped rather than sent hours later. Each slot is sent once (remembered in data/phone_state.json).

Config (config.json → "phone"):
  {"enabled": true, "server": "https://ntfy.sh", "topic": "aqi-…",
   "times": ["00:00","02:00", … ,"22:00"], "priority": 4,
   "surge_alerts": true, "late_ok_min": 90}
"""
import json
import os
from datetime import datetime, timedelta

import requests

from recorder import DATA_DIR

DEFAULT_TIMES = [f"{h:02d}:00" for h in range(0, 24, 2)]          # every 2 hours


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def dot(aqi):
    """US AQI colour as an emoji — the only colour a plain notification can carry."""
    a = _f(aqi)
    if a is None:
        return "⚪"
    for hi, e in ((50, "🟢"), (100, "🟡"), (150, "🟠"), (200, "🔴"), (300, "🟣")):
        if a <= hi:
            return e
    return "🟤"


def _category(aqi):
    from aqi_math import category
    try:
        return category(float(aqi))
    except (TypeError, ValueError):
        return ""


class PhoneNotifier:
    def __init__(self, config, data_dir=DATA_DIR, session=requests):
        c = config.get("phone") or {}
        self.enabled = bool(c.get("enabled")) and bool(c.get("topic"))
        self.server = (c.get("server") or "https://ntfy.sh").rstrip("/")
        self.topic = c.get("topic", "")
        self.times = c.get("times") or DEFAULT_TIMES
        self.surge = c.get("surge_alerts", True)
        self.late_ok = timedelta(minutes=int(c.get("late_ok_min", 90)))
        # ntfy priority 4 = "high": pops up on screen (heads-up) with sound; 3 = quiet, shade only
        self.priority = int(c.get("priority", 4))
        self.session = session
        self.state_path = os.path.join(data_dir, "phone_state.json")
        try:
            with open(self.state_path, encoding="utf-8") as f:
                self.state = json.load(f)
        except (OSError, ValueError):
            self.state = {"sent": []}

    # ── transport ───────────────────────────────────────────────────────────
    def send(self, title, message, priority=3, tags=None):
        """ntfy JSON publish (UTF-8 safe). Returns (ok, error text)."""
        if not self.enabled:
            return False, "phone notifications are off (config.json → phone)"
        try:
            r = self.session.post(self.server, timeout=15, json={
                "topic": self.topic, "title": title, "message": message,
                "priority": priority, "tags": tags or []})
            if r.status_code >= 400:
                return False, f"ntfy answered {r.status_code}: {r.text[:120]}"
            self._remember_own(r)
            return True, ""
        except Exception as e:
            return False, str(e)[:160]

    def _remember_own(self, r):
        try:
            mid = r.json().get("id")
        except Exception:
            mid = None
        if mid:
            self.own_ids = (getattr(self, "own_ids", []) + [mid])[-200:]

    def send_image(self, png_bytes, filename, title, message=""):
        """Upload a PNG as an ntfy attachment (shown as a picture in the notification).
        Headers must be plain ASCII, so title/message here avoid symbols."""
        if not self.enabled:
            return False, "phone notifications are off"
        try:
            r = self.session.put(f"{self.server}/{self.topic}", data=png_bytes, timeout=60, headers={
                "Filename": filename, "Title": title.encode("ascii", "ignore").decode(),
                "Message": message.encode("ascii", "ignore").decode(), "Priority": "3"})
            if r.status_code >= 400:
                return False, f"ntfy answered {r.status_code}: {r.text[:120]}"
            self._remember_own(r)
            return True, ""
        except Exception as e:
            return False, str(e)[:160]

    def is_command(self, ev, max_age_s=600):
        """A message typed by you in the ntfy app (not one of ours, not stale)."""
        if ev.get("id") in getattr(self, "own_ids", []) or ev.get("title") or ev.get("attachment"):
            return False                       # our messages always carry a title or a picture
        if _time.time() - float(ev.get("time") or 0) > max_age_s:
            return False                       # sent while the laptop was off long ago: skip
        done = self.state.get("handled", [])
        if ev.get("id") in done:
            return False
        self.state["handled"] = (done + [ev.get("id")])[-100:]
        self.state["last_id"] = ev.get("id")
        try:
            tmp = self.state_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.state, f)
            os.replace(tmp, self.state_path)
        except OSError:
            pass
        return True

    # ── scheduled updates ───────────────────────────────────────────────────
    def due_slot(self, now=None):
        """The most recent scheduled time that hasn't been sent and is ≤ late_ok old, else None."""
        now = now or datetime.now()
        best = None
        for day in (now.date() - timedelta(days=1), now.date()):
            for hm in self.times:
                try:
                    h, m = (int(x) for x in hm.split(":"))
                except ValueError:
                    continue
                t = datetime.combine(day, datetime.min.time()).replace(hour=h, minute=m)
                if t <= now and now - t <= self.late_ok and t.isoformat() not in self.state["sent"]:
                    best = t if best is None or t > best else best
        return best

    def mark_sent(self, slot):
        self.state["sent"] = (self.state["sent"] + [slot.isoformat()])[-60:]
        tmp = self.state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.state, f)
        os.replace(tmp, self.state_path)

    @staticmethod
    def summary(ctx, slot, now, prev=None):
        """Compact, colour-coded update. Returns (title, message, values-for-next-trend).

        Title = your area at a glance:  🔴 Your area · AQI 180 ↑12 · PM2.5 108 · Unhealthy
        Body  = one line per place:      🔴 <your city> · AQI 171 ↓4 · PM2.5 ≈84
                                         🟢 Bedroom · PM2.5 22 · purifier on
                                         footer: units + which hour the measured PM2.5 is
        Colours follow the US AQI scale (🟢 Good 🟡 Moderate 🟠 Sensitive 🔴 Unhealthy
        🟣 Very unhealthy 🟤 Hazardous). Indoor colour comes from its PM2.5's AQI."""
        from aqi_math import aqi_to_pm25, pm25_to_aqi
        prev = prev or {}
        cards = ctx.get("cards") or {}
        area = cards.get("area") or {}
        a_aqi = _f(area.get("aqi")) if area else None
        a_live = area.get("live", "") if area else ""
        a_pm = area.get("pm25") if area else None
        a_approx = area.get("approx", False) if area else False
        meas = area.get("measured") if area else None
        if a_aqi is None and _f(ctx.get("waqi_aqi")) is not None:     # fetch failed: recorder's live value
            a_aqi, a_approx, a_live = _f(ctx["waqi_aqi"]), True, ""
            a_pm = aqi_to_pm25(a_aqi)
        if meas is None and _f(ctx.get("pm25")) is not None:
            h = str(ctx.get("hour") or "")
            span = f"{h[:5]}–{(int(h[:2]) + 1) % 24:02d}:00" if h[:2].isdigit() else h
            meas = {"pm25": _f(ctx["pm25"]), "aqi": pm25_to_aqi(_f(ctx["pm25"])), "span": span}
        d_aqi = _f(ctx.get("city_aqi"))                        # city-wide median of all monitors
        d_label = ctx.get("city_name") or "City"
        if d_aqi is None:
            d_aqi = _f((cards.get("city") or {}).get("aqi"))
        d_pm = aqi_to_pm25(d_aqi) if d_aqi is not None else None

        def trend(key, v):
            p = _f(prev.get(key))
            if v is None or p is None:
                return ""
            d = v - p
            return f" ↑{d:.0f}" if d >= 5 else (f" ↓{-d:.0f}" if d <= -5 else " →")

        def pm(v, approx):
            return "PM2.5 —" if v is None else f"PM2.5 {'≈' if approx else ''}{v:.0f}"

        lines = []
        if meas:
            lines.append(f"📏 Measured {meas['span']} · PM2.5 {meas['pm25']:.0f} → AQI {meas['aqi']} "
                         f"{dot(meas['aqi'])}")
        if d_aqi is not None:
            lines.append(f"{dot(d_aqi)} {d_label} (live) · AQI {d_aqi:.0f}{trend('delhi', d_aqi)} · {pm(d_pm, True)}")
        for d in ctx.get("indoor") or []:
            name = d.get("room") or d.get("device") or "Indoor"
            v = _f(d.get("pm25"))
            if v is not None:
                state = "" if d.get("power") == "on" else " · purifier off"
                lines.append(f"{dot(pm25_to_aqi(v))} {name} · PM2.5 {v:.0f}{state}")
        if not lines:
            lines.append("No fresh readings (network or data sources down).")
        lines.append("PM2.5 in µg/m³ · ≈ = estimated from live AQI · measured = govt monitors, 1–2 h old")
        lines.append(HELP_HINT)

        late = now - slot > timedelta(minutes=10)
        if a_aqi is not None:                  # your area is the headline: colour, AQI, trend, PM2.5
            title = (f"{dot(a_aqi)} Your area · AQI {a_aqi:.0f}{trend('area', a_aqi)} "
                     f"({'live ' + a_live if a_live else 'live'}) · {pm(a_pm, a_approx)} · {_category(a_aqi)}")
        else:
            title = "AQI update"
        if late:
            title += f" ({slot:%H:%M} update, late — laptop was off)"
        return title, "\n".join(lines), {"area": a_aqi, "delhi": d_aqi}

    def maybe_send_update(self, ctx, now=None):
        now = now or datetime.now()
        slot = self.due_slot(now)
        if slot is None or not self.enabled:
            return None
        title, msg, vals = self.summary(ctx, slot, now, self.state.get("last"))
        ok, err = self.send(title, msg, priority=self.priority)
        if ok:
            self.state["last"] = vals
            self.mark_sent(slot)
        return {"slot": slot.strftime("%H:%M"), "ok": ok, "error": err}

    # ── surge alerts ────────────────────────────────────────────────────────
    def send_alert(self, alert):
        if not (self.enabled and self.surge) or alert.get("kind") != "spike":
            return None
        body = alert["body"]
        main, _, cause = body.partition(" Likely: ")
        msg = main.strip() + (f"\n💡 Likely: {cause.strip()}" if cause else "") + "\n" + HELP_HINT
        return self.send("🚨 " + alert["title"].replace("⚠ ", ""), msg, priority=5)


# ── talking back: commands typed in the ntfy app ────────────────────────────
# The ntfy "send" box posts to the same topic. The app listens to it and answers. No LLM: the
# message is scanned for keywords, and the first matching intent (in this order) wins:
#   help → why → graph → room → aqi in <place> → now      (anything else → help)
import re
import time as _time

HELP_TEXT = (
    "Ask in your own words. I look for these words:\n\n"
    "📍 aqi / now — current AQI + PM2.5 for your area, your city and your room\n"
    "    e.g. \"what's the AQI right now?\" · \"send the update again\"\n\n"
    "💡 why — likely reasons the air is bad (festival, calm wind, smoke vs dust…)\n"
    "    e.g. \"why is the AQI so high?\"\n\n"
    "🗺 aqi in <place> — any locality's AQI from its nearby monitors\n"
    "    e.g. \"AQI in Karol Bagh\" · \"pm2.5 near Dwarka sector 10\"\n\n"
    "🛏 room / purifier — indoor PM2.5, purifier on/off, how much lower than outside\n"
    "    e.g. \"how is my room?\" · \"is the purifier on?\"\n\n"
    "📈 graph — a chart picture: today, yesterday, month or purifier\n"
    "    e.g. \"show today's graph\" · \"purifier graph\" · \"graph of the month\"\n\n"
    "❓ help — this list\n"
    "Replies need the laptop app to be running."
)
HELP_HINT = "💬 Type help to see what you can ask"

_W = {
    "help": {"help", "commands", "command", "menu", "options", "hi", "hello", "hey", "start"},
    "why": {"why", "reason", "reasons", "cause", "causes", "caused", "explain", "because"},
    "graph": {"graph", "graphs", "chart", "charts", "plot", "trend", "trends", "picture", "image", "curve"},
    "room": {"room", "indoor", "indoors", "bedroom", "inside", "purifier", "purifiers", "filter"},
    "now": {"aqi", "air", "pm", "pm2.5", "pm25", "pollution", "smog", "now", "current", "currently",
            "update", "status", "again", "notification", "notify", "resend", "send", "outside",
            "level", "levels", "quality", "today", "how", "bad", "reading", "readings"},
}
_HERE = {"my area", "here", "home", "my home", "our area", "the area", "area", "my place", "my locality",
         "delhi", "new delhi", "the city", "city", "my room", "room", "the room", "bedroom"}
_TIMEWORDS = {"now", "right now", "today", "currently", "please", "the moment", "this moment", "tonight",
              "the morning", "morning", "the evening", "evening", "night", "the night", "afternoon",
              "the afternoon", "last hour", "this hour", "a while", "general"}


def parse_command(text):
    """→ (intent, argument). intent ∈ help, why, graph, room, place, now, ignore, unknown."""
    t = (text or "").lower().strip()
    words = set(re.findall(r"[a-z0-9.]+", t))
    has = lambda key: bool(words & _W[key])
    if words and words <= {"thanks", "thank", "you", "ok", "okay", "cool", "nice", "great", "thx", "ty", "k"}:
        return "ignore", None
    if not t or t in {"?", "h"} or has("help"):
        return "help", None
    if has("why"):
        return "why", None
    if has("graph"):
        if words & {"month", "monthly", "30"}:
            return "graph", "month"
        if "yesterday" in words:
            return "graph", "yesterday"
        if has("room"):
            return "graph", "purifier"
        return "graph", "today"
    if has("room"):
        return "room", None
    m = re.search(r"\b(?:in|at|near|around|for|of)\s+([a-z][a-z0-9 .,'-]{1,60})", t)
    if m:
        place = re.sub(r"[?!.,]+$", "", m.group(1)).strip()
        for tw in sorted(_TIMEWORDS, key=len, reverse=True):          # "in rohini right now" → rohini
            place = re.sub(rf"\b{re.escape(tw)}\b", "", place).strip(" ,.")
        place = re.sub(r"\s{2,}", " ", place).strip()
        if place and place not in _HERE and place not in _TIMEWORDS and not place.startswith(("the moment", "my ")):
            return "place", place
    if has("now"):
        return "now", None
    return "unknown", None


def listen(server, topic, since, on_message, should_stop, on_status=None):
    """Blocking loop: streams the topic (ntfy /json endpoint) and calls on_message(event) for every
    message. Reconnects with back-off. `since` = last seen message id or a duration like "10m"."""
    url = f"{server.rstrip('/')}/{topic}/json"
    delay = 2
    while not should_stop():
        try:
            with requests.get(url, params={"since": since}, stream=True, timeout=(10, 70)) as r:
                r.raise_for_status()
                if on_status:
                    on_status("listening")
                delay = 2
                for line in r.iter_lines(decode_unicode=True):
                    if should_stop():
                        return
                    if not line:
                        continue
                    try:
                        ev = json.loads(line)
                    except ValueError:
                        continue
                    if ev.get("event") == "message":
                        since = ev.get("id") or since
                        on_message(ev)
        except Exception as e:
            if on_status:
                on_status(f"reconnecting ({str(e)[:60]})")
            for _ in range(int(delay * 10)):
                if should_stop():
                    return
                _time.sleep(0.1)
            delay = min(delay * 2, 120)
