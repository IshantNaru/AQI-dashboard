"""
Area reading from WAQI live data: every WAQI monitor within `radius_km` of a point
(widened to `max_radius_km` if fewer than `min_n`), summarised three ways:
median, plain average, and 1/d² distance-weighted average. Used by the
dashboard's "Your locality" card and by the locality search.

Note: WAQI's bounds listing gives each monitor's current AQI only (not µg/m³). The WAQI client
converts it to the 2024 US scale (via µg/m³); WAQI's own number is kept as "aqi_waqi".
"""
import math
import statistics


def _dist_km(lat1, lng1, lat2, lng2):
    p = math.pi / 180
    a = (0.5 - math.cos((lat2 - lat1) * p) / 2
         + math.cos(lat1 * p) * math.cos(lat2 * p) * (1 - math.cos((lng2 - lng1) * p)) / 2)
    return 12742 * math.asin(math.sqrt(a))


def summarise(points):
    """points: [(value, distance_km)] -> dict of median / mean / idw."""
    vals = [v for v, _ in points]
    w = [1 / max(d, 0.5) ** 2 for _, d in points]
    return {"median": round(statistics.median(vals)),
            "mean": round(statistics.fmean(vals)),
            "idw": round(sum(v * x for v, x in zip(vals, w)) / sum(w))}


def area_reading(waqi, lat, lng, radius_km=5.0, max_radius_km=8.0, min_n=2):
    d = max_radius_km / 111.0
    raw = waqi.stations_in_bounds([lat - d, lng - d, lat + d, lng + d])
    mons = []
    for s in raw:
        try:
            aqi = float(s.get("aqi"))
            slat, slng = float(s["lat"]), float(s["lon"])
        except (TypeError, ValueError, KeyError):
            continue
        name = str((s.get("station") or {}).get("name") or "").split(",")[0].strip()
        mons.append({"name": name, "aqi": aqi, "km": round(_dist_km(lat, lng, slat, slng), 1),
                     "time": (s.get("station") or {}).get("time"), "aqi_waqi": s.get("aqi_waqi"),
                     "uid": s.get("uid")})
    mons.sort(key=lambda m: m["km"])
    used = [m for m in mons if m["km"] <= radius_km]
    r = radius_km
    if len(used) < min_n:
        used, r = [m for m in mons if m["km"] <= max_radius_km], max_radius_km
    if not used:
        return None
    out = summarise([(m["aqi"], m["km"]) for m in used])
    out.update(n=len(used), radius_km=r, monitors=used)
    return out


def describe(area, method="median"):
    names = {"median": "median", "mean": "average", "idw": "distance-weighted avg"}
    lst = " · ".join(f"{m['name']} {m['km']} km ({m['aqi']:.0f})" for m in area["monitors"])
    return (f"{names[method].capitalize()} of {area['n']} WAQI monitors within {area['radius_km']:g} km  ·  "
            f"median {area['median']} / average {area['mean']} / distance-weighted {area['idw']}\n{lst}")
