"""
verify.py — "How is this number calculated?" for each dashboard card.

Every number shown can be traced and recomputed by anyone:
  1. Source + time   — which feed, which monitors, which hour (or "live" time).
  2. Raw values      — each monitor's own reading, with a link to its public page.
  3. Arithmetic      — the US EPA 2024 formula with the actual numbers plugged in.
  4. Cross-checks    — monitors reporting, live vs measured, and the satellite model (independent
                       of the monitors) against its usual ratio to them.
The app can't certify a government instrument; what it can do is make every step reproducible
and flag when sources stop agreeing.
"""
import statistics
from datetime import datetime, timedelta

from aqi_math import PM25_BP, PM25_BP_OLD, pm25_to_aqi, waqi_to_pm25, category

EPA_NOTE = ("US EPA AQI, 2024 PM2.5 breakpoints (Federal Register, Feb 2024). Above 500 the scale is officially "
            "undefined; the app extends the top segment linearly.")
CPCB = "https://www.data.gov.in/resource/real-time-air-quality-index-various-locations"   # CPCB feed, official


def _band(c, table):
    for lo, hi, ilo, ihi in table:
        if lo <= c <= hi:
            return lo, hi, ilo, ihi
    return table[-1]


def formula(c):
    """'311 µg/m³ → AQI 471' with the arithmetic shown."""
    lo, hi, ilo, ihi = _band(c, PM25_BP)
    a = pm25_to_aqi(c)
    return (f"{c:.0f} µg/m³ is in the {lo}–{hi} band ({ilo}–{ihi}) → "
            f"AQI = {ilo} + ({c:.0f} − {lo}) × ({ihi} − {ilo}) / ({hi} − {lo}) = <b>{a}</b> ({category(a)})")


def waqi_formula(raw):
    c = waqi_to_pm25(raw)
    lo, hi, ilo, ihi = _band(raw, [(b[2], b[3], b[0], b[1]) for b in PM25_BP_OLD])   # index → band
    return c, (f"WAQI {raw:.0f} (older US table: AQI {lo}–{hi} = {ilo}–{ihi} µg/m³) → "
               f"{c:.0f} µg/m³ → 2024 AQI {pm25_to_aqi(c)}")


def latest_home_row(store, now=None):
    now = now or datetime.now().astimezone()
    rows = []
    for m in {now.strftime("%Y-%m"), (now - timedelta(days=2)).strftime("%Y-%m")}:
        rows += [r for r in store.read(store.path("readings", m))
                 if r.get("target") == "home" and r.get("pm25_ugm3")]
    return max(rows, key=lambda r: r["hour_start_utc"]) if rows else None


def model_ratio(store, now=None, days=7):
    """(latest model value, its hour, today's ratio monitors/model, typical ratio over `days`)."""
    import cams
    now = now or datetime.now().astimezone()
    months = sorted({now.strftime("%Y-%m"), (now - timedelta(days=days)).strftime("%Y-%m")})
    model = {}
    for r in cams.read(store.dir, months):
        try:
            model[r["time_local"][:13]] = float(r["pm25_model"])
        except (KeyError, ValueError):
            pass
    meas = {}
    for m in months:
        for r in store.read(store.path("readings", m)):
            if r.get("target") == "home" and r.get("pm25_ugm3"):
                meas[r["hour_start_local"][:13]] = float(r["pm25_ugm3"])
    keys = sorted(k for k in meas if k in model and model[k] > 0)
    if not model:
        return None
    last_k = max(model)
    cut = (now - timedelta(days=days)).strftime("%Y-%m-%dT%H")
    hist = [meas[k] / model[k] for k in keys if k >= cut]
    today = [meas[k] / model[k] for k in keys if k[:10] == now.strftime("%Y-%m-%d")]
    return {"value": model[last_k], "hour": last_k[11:13] + ":00", "date": last_k[:10],
            "ratio_today": statistics.median(today) if today else None,
            "ratio_typical": statistics.median(hist) if hist else None, "n": len(hist)}


def checks(store, area, measured_row, cards=None, configured_monitors=5):
    """Short status list [(ok: bool|None, text)] for the badge and the dialog."""
    out = []
    if measured_row:
        used = [p for p in (measured_row.get("monitors_used") or "").split(";") if p.strip()]
        n = len(used)
        out.append((n >= configured_monitors, f"{n}/{configured_monitors} government monitors reported in the last "
                    f"published hour" + (f" — missing: {measured_row.get('monitors_missing')}"
                                         if measured_row.get("monitors_missing") else "")))
    live = (cards or {}).get("area") or {}
    m = live.get("measured")
    if m and live.get("pm25"):
        d = abs(live["pm25"] - m["pm25"]) / max(m["pm25"], 1)
        out.append((d <= 0.30, f"Live estimate {live['pm25']:.0f} vs measured {m['pm25']:.0f} µg/m³ "
                    f"({d:.0%} apart; different times, and WAQI carries only some of the monitors)"))
    try:
        mr = model_ratio(store)
    except Exception:
        mr = None
    if mr and mr.get("ratio_today") and mr.get("ratio_typical"):
        dev = mr["ratio_today"] / mr["ratio_typical"]
        ok = 0.6 <= dev <= 1.6
        out.append((ok, f"Satellite model agrees with the monitors' usual pattern (today monitors = "
                    f"{mr['ratio_today']:.1f}× model, usual {mr['ratio_typical']:.1f}×)" if ok else
                    f"Monitors and satellite model diverge from their usual ratio (today {mr['ratio_today']:.1f}× vs "
                    f"usual {mr['ratio_typical']:.1f}×) — a local source, or a monitor problem"))
    elif mr is None:
        out.append((None, "Satellite model: no data yet (fetched hourly while the app runs)"))
    else:
        out.append((None, f"Satellite model: latest {mr['value']:.0f} µg/m³ ({mr['date']} {mr['hour']}); no hours "
                          "overlapping with today's measured data yet"
                          + (f" — usually monitors read {mr['ratio_typical']:.1f}× the model" if mr.get('ratio_typical') else "")))
    return out


def badge(chks):
    bad = [c for c in chks if c[0] is False]
    return ("⚠ " + str(len(bad)) + " check" + ("s" if len(bad) > 1 else "") + " to look at") if bad else "✓ checks pass"


def area_html(store, data, cards, outdoor_ctx, configured_monitors=5):
    area = (data or {}).get("area") or {}
    c = (cards or {}).get("area") or {}
    row = latest_home_row(store)
    parts = ["<h3>Your area — how the numbers are made</h3>"]
    # live
    parts.append(f"<h4>Big number: live AQI {c.get('aqi')} ({'live ' + c['live'] if c.get('live') else 'live'})</h4>")
    if area.get("monitors"):
        parts.append("<p>Source: WAQI (aqicn.org) — the latest reading of each monitor within "
                     f"{area.get('radius_km')} km. WAQI publishes an index on the <i>older</i> US table, so each is "
                     "turned back into µg/m³ and re-scored on the 2024 table:</p><ul>")
        for mon in area["monitors"]:
            raw = mon.get("aqi_waqi")
            if raw is not None:
                _, txt = waqi_formula(float(raw))
                parts.append(f"<li>{mon['name']} ({mon['km']} km, reported {mon.get('time') or '?'}): "
                             f"{txt}</li>")
        parts.append(f"</ul><p>Median of the {area.get('n')} → <b>AQI {area.get('median')}</b> "
                     f"≈ PM2.5 {c.get('pm25') or 0:.0f} µg/m³. Check any of these on the "
                     f"<a href='https://aqicn.org/map/delhi/'>aqicn.org Delhi map</a>.</p>")
    # measured
    if row:
        parts.append(f"<h4>Measured line: {row['hour_start_local'][11:16]} hour</h4>"
                     "<p>Source: OpenAQ, which mirrors the official CPCB/DPCC feeds — hourly averages from the "
                     "government monitors, published 1–2 h after the hour. Each monitor's value:</p><p>"
                     + (row.get("monitors_used") or "").replace(";", "<br>") + "</p>"
                     f"<p>Median → <b>{float(row['pm25_ugm3']):.0f} µg/m³</b>.<br>{formula(float(row['pm25_ugm3']))}</p>")
        urls = (row.get("source_url") or "").split()
        if urls:
            parts.append("<p>Monitor pages: " + " · ".join(f"<a href='{u}'>{i + 1}</a>" for i, u in enumerate(urls))
                         + f" &nbsp;|&nbsp; cross-check against the official "
                           f"<a href='{CPCB}'>CPCB real-time feed (data.gov.in)</a>.</p>")
    # checks
    chks = checks(store, area, row, cards, configured_monitors)
    parts.append("<h4>Cross-checks</h4><ul>" + "".join(
        f"<li>{'✅' if ok else ('⚠️' if ok is False else '•')} {txt}</li>" for ok, txt in chks) + "</ul>")
    parts.append(f"<p style='color:#8a8aa3'>{EPA_NOTE} Every hourly value, with its monitors and source "
                 "links, is saved in data/readings_YYYY-MM.csv, so any number can be recomputed later.</p>")
    return "".join(parts), chks


def city_html(data, cards):
    city = (data or {}).get("city") or {}
    c = (cards or {}).get("city") or {}
    raw = (city.get("iaqi_waqi") or {}).get("pm25") or city.get("aqi_waqi")
    p = [f"<h3>City overview — {city.get('station', '?')}</h3>",
         f"<p>Source: WAQI live feed for one station, reported {city.get('time') or '?'}.</p>"]
    if raw is not None:
        _, txt = waqi_formula(float(raw))
        p.append(f"<p>{txt}</p>")
    if city.get("url"):
        p.append(f"<p><a href='{city['url']}'>Station page on aqicn.org</a></p>")
    p.append(f"<p>Shown: AQI {c.get('aqi')}, PM2.5 ≈ {c.get('pm25') or 0:.0f} µg/m³ (estimated from the index).</p>"
             f"<p style='color:#8a8aa3'>{EPA_NOTE}</p>")
    return "".join(p)


def consensus_html(data, cards):
    sr = (data or {}).get("source_readings") or {}
    c = (cards or {}).get("consensus") or {}
    p = ["<h3>Consensus — median of independent feeds</h3><ul>"]
    for name, r in sr.items():
        if r.get("ok"):
            extra = ""
            d = r.get("data") or {}
            if name == "WAQI" and d.get("aqi_waqi") is not None:
                extra = f" (WAQI's own number {d['aqi_waqi']}, older table)"
            if name == "OWM":
                extra = f" (model; PM2.5 {(d.get('components') or {}).get('pm2_5')} µg/m³ → 2024 table)"
            p.append(f"<li>{name}: AQI {r.get('aqi')}{extra}</li>")
        else:
            p.append(f"<li>{name}: failed — {r.get('error')}</li>")
    p.append(f"</ul><p>Median AQI → <b>{c.get('aqi')}</b>. PM2.5: {c.get('note', '')}.</p>"
             f"<p style='color:#8a8aa3'>{EPA_NOTE}</p>")
    return "".join(p)
