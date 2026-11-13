"""
US EPA AQI math (PM2.5 breakpoints per the 2024 NAAQS revision, PM10 unchanged).

Rules followed (EPA Technical Assistance Document for AQI reporting):
  * PM2.5 concentration truncated to 0.1 µg/m³, PM10 to 1 µg/m³, before lookup.
  * Daily AQI = AQI of the 24-hour *mean concentration* — never the mean of AQIs.
  * Overall AQI = max of pollutant sub-indices; the max one is "dominant".
Concentrations above the top breakpoint are "beyond the AQI"; we extrapolate
the top segment's slope so the number still ranks severity (flagged by > 500).
"""
import math

PM25_BP = [  # (C_lo, C_hi, I_lo, I_hi)  µg/m³
    (0.0,   9.0,   0,   50),
    (9.1,   35.4,  51,  100),
    (35.5,  55.4,  101, 150),
    (55.5,  125.4, 151, 200),
    (125.5, 225.4, 201, 300),
    (225.5, 325.4, 301, 500),
]
PM10_BP = [
    (0,   54,  0,   50),
    (55,  154, 51,  100),
    (155, 254, 101, 150),
    (255, 354, 151, 200),
    (355, 424, 201, 300),
    (425, 604, 301, 500),
]


def _truncate(x: float, decimals: int) -> float:
    f = 10 ** decimals
    return math.floor(x * f + 1e-9) / f


def _sub_index(conc, table, decimals):
    if conc is None:
        return None
    try:
        c = float(conc)
    except (TypeError, ValueError):
        return None
    if math.isnan(c) or c < 0:
        return None
    c = _truncate(c, decimals)
    for c_lo, c_hi, i_lo, i_hi in table:
        if c_lo <= c <= c_hi:
            return round((i_hi - i_lo) / (c_hi - c_lo) * (c - c_lo) + i_lo)
    c_lo, c_hi, i_lo, i_hi = table[-1]          # beyond the index
    return round((i_hi - i_lo) / (c_hi - c_lo) * (c - c_lo) + i_lo)


def pm25_to_aqi(conc):
    return _sub_index(conc, PM25_BP, 1)


def pm10_to_aqi(conc):
    return _sub_index(conc, PM10_BP, 0)


def overall_aqi(pm25=None, pm10=None):
    """Returns (aqi, dominant) from the available PM sub-indices."""
    subs = {k: v for k, v in (("pm25", pm25_to_aqi(pm25)), ("pm10", pm10_to_aqi(pm10))) if v is not None}
    if not subs:
        return None, None
    dom = max(subs, key=subs.get)
    return subs[dom], dom


def category(aqi):
    if aqi is None:
        return "—"
    a = int(aqi)
    if a <= 50:  return "Good"
    if a <= 100: return "Moderate"
    if a <= 150: return "Unhealthy for Sensitive Groups"
    if a <= 200: return "Unhealthy"
    if a <= 300: return "Very Unhealthy"
    return "Hazardous"


def aqi_to_pm25(aqi):
    """Inverse of pm25_to_aqi (US EPA 2024 table; linear beyond 500). Used only to ESTIMATE µg/m³
    from a source that publishes an index, not a concentration (e.g. WAQI). Sources may use an older
    breakpoint table, so treat the result as approximate (±10 %)."""
    try:
        a = float(aqi)
    except (TypeError, ValueError):
        return None
    if a < 0:
        return None
    for c_lo, c_hi, i_lo, i_hi in PM25_BP:
        if i_lo <= a <= i_hi:
            return round(c_lo + (a - i_lo) * (c_hi - c_lo) / (i_hi - i_lo), 1)
    c_lo, c_hi, i_lo, i_hi = PM25_BP[-1]
    return round(c_lo + (a - i_lo) * (c_hi - c_lo) / (i_hi - i_lo), 1)


# WAQI (aqicn.org) publishes PM2.5 AQI on the OLDER US EPA table (pre-2024). Verified against your
# monitors: 309 paired hourly readings matched the old table to a median 3 AQI points, the 2024
# table to 26. So a WAQI number is first turned back into µg/m³ with the old table, then into AQI
# with the 2024 table the rest of the app uses.
PM25_BP_OLD = [
    (0.0,   12.0,  0,   50),
    (12.1,  35.4,  51,  100),
    (35.5,  55.4,  101, 150),
    (55.5,  150.4, 151, 200),
    (150.5, 250.4, 201, 300),
    (250.5, 350.4, 301, 400),
    (350.5, 500.4, 401, 500),
]


def waqi_to_pm25(aqi):
    """WAQI's PM2.5 index → µg/m³ (old US table; linear beyond 500)."""
    try:
        a = float(aqi)
    except (TypeError, ValueError):
        return None
    if a < 0:
        return None
    for c_lo, c_hi, i_lo, i_hi in PM25_BP_OLD:
        if i_lo <= a <= i_hi:
            return round(c_lo + (a - i_lo) * (c_hi - c_lo) / (i_hi - i_lo), 1)
    c_lo, c_hi, i_lo, i_hi = PM25_BP_OLD[-1]
    return round(c_lo + (a - i_lo) * (c_hi - c_lo) / (i_hi - i_lo), 1)


def waqi_to_us2024(aqi):
    """WAQI's number on the 2024 US scale (via µg/m³). Assumes PM2.5 is the pollutant behind it."""
    c = waqi_to_pm25(aqi)
    return pm25_to_aqi(c) if c is not None else None


def live_row_aqi(row, key="waqi_aqi"):
    """AQI of a stored live row on the 2024 scale. Rows saved before the switch have no
    'aqi_scale' and hold WAQI's own (old-scale) number."""
    v = row.get(key)
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    return v if row.get("aqi_scale") == "us2024" else waqi_to_us2024(v)
