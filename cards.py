"""
cards.py — AQI and PM2.5 for the three top cards (and the phone message), from one fetch.

Every AQI here is on the 2024 US scale and is computed FROM a PM2.5 concentration:
  • Your area — LIVE: WAQI's latest readings of the monitors within your radius, turned back into
    µg/m³ (WAQI uses the older US table) → median → AQI. Marked "≈" and "live HH:MM".
    MEASURED: the OpenAQ hourly median of your 5 government monitors, for the last published hour
    (1–2 h old) → AQI. Shown on its own line with its hour, never mixed with the live number.
  • City overview: WAQI's city station, same conversion, "live HH:MM".
  • Consensus: median of the µg/m³ values available (area measured, OpenWeather model, OpenAQ
    nearest station, WAQI estimate); its AQI is the median of the sources' AQIs.
"""
import re
import statistics
from datetime import datetime

from aqi_math import aqi_to_pm25, pm25_to_aqi


def _num(x):
    try:
        v = float(x)
        return v if v >= 0 else None
    except (TypeError, ValueError):
        return None


def _hhmm(t):
    """'2026-11-12 23:00:00', ISO with offset, or None → local 'HH:MM' (or '')."""
    if not t:
        return ""
    try:
        d = datetime.fromisoformat(str(t).replace("Z", "+00:00"))
        return (d.astimezone() if d.tzinfo else d).strftime("%H:%M")
    except ValueError:
        m = re.search(r"(\d{2}):(\d{2})", str(t))
        return f"{m.group(1)}:{m.group(2)}" if m else ""


def card_values(data, outdoor_ctx=None):
    data, oc = data or {}, outdoor_ctx or {}
    out = {}

    measured = None
    m_pm = _num(oc.get("pm25"))
    if m_pm is not None:
        h = str(oc.get("hour") or "")
        span = f"{h[:5]}–{(int(h[:2]) + 1) % 24:02d}:00" if len(h) >= 2 and h[:2].isdigit() else (h or "?")
        measured = {"pm25": m_pm, "aqi": pm25_to_aqi(m_pm), "span": span}

    area = data.get("area")
    if area:
        live_aqi = area["median"]
        times = [_hhmm(m.get("time")) for m in area.get("monitors", [])]
        live_t = max((t for t in times if t), default="")
        out["area"] = {"aqi": live_aqi, "pm25": aqi_to_pm25(live_aqi), "approx": True, "live": live_t,
                       "note": f"live{' ' + live_t if live_t else ''} · estimated from WAQI readings of "
                               f"{area['n']} monitors", "measured": measured}
    elif measured:
        out["area"] = {"aqi": measured["aqi"], "pm25": m_pm, "approx": False, "live": "",
                       "note": f"measured {measured['span']} (no live reading)", "measured": None}
    else:
        out["area"] = {"aqi": None, "pm25": None, "approx": True, "live": "", "note": "", "measured": None}

    city = data.get("city") or {}
    c_aqi = _num(city.get("aqi"))
    c_sub = _num((city.get("iaqi") or {}).get("pm25"))
    c_t = _hhmm(city.get("time"))
    out["city"] = {"aqi": c_aqi, "pm25": aqi_to_pm25(c_sub if c_sub is not None else c_aqi), "approx": True,
                   "live": c_t, "note": f"live{' ' + c_t if c_t else ''} · estimated from WAQI's reading",
                   "name": (city.get("station") or "").split(",")[0]}

    vals, used = [], []
    if m_pm is not None:
        vals.append(m_pm); used.append(f"area measured {measured['span']}")
    sr = data.get("source_readings") or {}
    owm = (((sr.get("OWM") or {}).get("data") or {}).get("components") or {}).get("pm2_5")
    if _num(owm) is not None and (sr.get("OWM") or {}).get("ok"):
        vals.append(float(owm)); used.append("OpenWeather (model)")
    oaq = (((sr.get("OpenAQ") or {}).get("data") or {}).get("iaqi") or {}).get("pm25")
    if _num(oaq) is not None and (sr.get("OpenAQ") or {}).get("ok"):
        vals.append(float(oaq)); used.append("OpenAQ")
    if out["city"]["pm25"] is not None:
        vals.append(out["city"]["pm25"]); used.append("WAQI est.")
    out["consensus"] = {"aqi": _num(data.get("consensus_aqi")),
                        "pm25": round(statistics.median(vals), 1) if vals else None, "approx": True, "live": "",
                        "note": f"median of {len(vals)}: {', '.join(used)}" if vals else "no µg/m³ source"}
    return out


def pm_text(card, unit=True):
    if not card or card.get("pm25") is None:
        return "PM2.5 —"
    return f"PM2.5 {'≈ ' if card.get('approx') else ''}{card['pm25']:.0f}{' µg/m³' if unit else ''}"
