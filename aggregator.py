"""
Aggregator — multi-source fallback chain + consensus AQI.

Source priority:
  1. WAQI   (CPCB + embassy; best Indian coverage)
  2. IQAir  (independent sensor network)
  3. OpenAQ (CPCB via separate pipeline)
  4. OWM    (model/satellite-derived)

Consensus = median of all sources that responded successfully.
"""
import math
import statistics
from typing import Optional

from waqi import WAQIClient
from openweather import OpenWeatherClient
from openaq import OpenAQClient
from iqair import IQAirClient
from cache import AQICache

EARTH_R_KM = 6371.0


def haversine(lat1, lng1, lat2, lng2) -> float:
    p = math.pi / 180.0
    a = (0.5 - math.cos((lat2 - lat1) * p) / 2
         + math.cos(lat1 * p) * math.cos(lat2 * p)
         * (1 - math.cos((lng2 - lng1) * p)) / 2)
    return 2 * EARTH_R_KM * math.asin(math.sqrt(a))


def bbox_around(lat, lng, radius_km):
    dlat    = radius_km / 111.0
    cos_lat = math.cos(math.radians(lat)) or 1e-6
    dlng    = radius_km / (111.0 * cos_lat)
    return [lat - dlat, lng - dlng, lat + dlat, lng + dlng]


def _safe_int_aqi(val) -> Optional[int]:
    try:
        return int(val)
    except (TypeError, ValueError):
        return None


class AQIAggregator:
    def __init__(self, waqi_token, owm_key=None, iqair_key=None, openaq_key=None):
        self.waqi   = WAQIClient(waqi_token)
        self.owm    = OpenWeatherClient(owm_key)   if owm_key   else None
        self.iqair  = IQAirClient(iqair_key)       if iqair_key else None
        self.openaq = OpenAQClient(openaq_key)     if openaq_key else OpenAQClient()
        self.cache  = AQICache()

    def fetch_all(self, lat, lng, city, radius_km=10, hotspot_radius_km=30):
        out = {
            "locality": None, "city": None,
            "nearby": [], "hotspots": [],
            "source_readings": {}, "consensus_aqi": None,
            "primary_source": None, "sources": [],
            "from_cache": False, "cache_age_minutes": None,
            "errors": [],
        }
        readings = {}

        # WAQI
        try:
            d   = self.waqi.by_geo(lat, lng)
            aqi = _safe_int_aqi(d.get("aqi")) if d else None
            readings["WAQI"] = {"ok": aqi is not None, "aqi": aqi, "data": d,
                                 "error": None if aqi is not None else "No AQI returned"}
        except Exception as e:
            readings["WAQI"] = {"ok": False, "aqi": None, "data": None, "error": str(e)}

        # IQAir
        if self.iqair:
            try:
                d   = self.iqair.nearest_city(lat, lng)
                aqi = _safe_int_aqi(d.get("aqi")) if d else None
                readings["IQAir"] = {"ok": aqi is not None, "aqi": aqi, "data": d,
                                      "error": None if aqi is not None else "No AQI returned"}
            except Exception as e:
                readings["IQAir"] = {"ok": False, "aqi": None, "data": None, "error": str(e)}

        # OpenAQ
        try:
            d   = self.openaq.nearest_aqi(lat, lng, radius_m=8000)
            aqi = _safe_int_aqi(d.get("aqi")) if d else None
            readings["OpenAQ"] = {"ok": aqi is not None, "aqi": aqi, "data": d,
                                   "error": None if aqi is not None else "No station within 8 km"}
        except Exception as e:
            readings["OpenAQ"] = {"ok": False, "aqi": None, "data": None, "error": str(e)}

        # OWM
        if self.owm:
            try:
                d   = self.owm.by_geo(lat, lng)
                aqi = _safe_int_aqi(d.get("aqi")) if d else None
                readings["OWM"] = {"ok": aqi is not None, "aqi": aqi, "data": d,
                                    "error": None if aqi is not None else "No data"}
            except Exception as e:
                readings["OWM"] = {"ok": False, "aqi": None, "data": None, "error": str(e)}

        out["source_readings"] = readings

        # Consensus
        ok_aqis = [r["aqi"] for r in readings.values() if r["ok"] and r["aqi"] is not None]
        if ok_aqis:
            out["consensus_aqi"] = int(statistics.median(ok_aqis))

        # Locality — first successful source
        for name in ["WAQI", "IQAir", "OpenAQ", "OWM"]:
            r = readings.get(name, {})
            if r.get("ok") and r.get("data"):
                out["locality"]       = r["data"]
                out["primary_source"] = name
                break

        # City feed
        if readings.get("WAQI", {}).get("ok"):
            try:
                out["city"] = self.waqi.by_city(city) if city else None
            except Exception as e:
                out["errors"].append(f"WAQI/city: {e}")
        if not out["city"]:
            out["city"] = out["locality"]

        # Nearby stations — WAQI primary, OpenAQ fallback
        waqi_nearby_ok = False
        if readings.get("WAQI", {}).get("ok"):
            try:
                raw = self.waqi.stations_in_bounds(bbox_around(lat, lng, radius_km))
                enriched = []
                for s in raw:
                    slat, slon = s.get("lat"), s.get("lon")
                    if slat is None or slon is None:
                        continue
                    s["distance_km"] = round(haversine(lat, lng, slat, slon), 2)
                    s["aqi_int"]     = _safe_int_aqi(s.get("aqi"))
                    if s["distance_km"] <= radius_km:
                        enriched.append(s)
                enriched.sort(key=lambda x: x["distance_km"])
                out["nearby"]     = enriched
                waqi_nearby_ok    = True
            except Exception as e:
                out["errors"].append(f"WAQI/nearby: {e}")

        if not waqi_nearby_ok:
            try:
                out["nearby"] = sorted(
                    self.openaq.stations_near(lat, lng, radius_m=int(radius_km * 1000)),
                    key=lambda x: x.get("distance_km") or 999,
                )
            except Exception as e:
                out["errors"].append(f"OpenAQ/nearby: {e}")

        # Hotspots — WAQI only
        if readings.get("WAQI", {}).get("ok"):
            try:
                raw   = self.waqi.stations_in_bounds(bbox_around(lat, lng, hotspot_radius_km))
                valid = []
                for s in raw:
                    aqi_int = _safe_int_aqi(s.get("aqi"))
                    if aqi_int is None:
                        continue
                    slat, slon    = s.get("lat"), s.get("lon")
                    s["aqi_int"]  = aqi_int
                    s["distance_km"] = (
                        round(haversine(lat, lng, slat, slon), 2)
                        if slat is not None and slon is not None else None
                    )
                    valid.append(s)
                valid.sort(key=lambda x: -x["aqi_int"])
                out["hotspots"] = valid[:5]
            except Exception as e:
                out["errors"].append(f"WAQI/hotspots: {e}")

        # Cache
        if out["locality"] is not None:
            try:
                self.cache.save(lat, lng, city, out)
            except Exception as e:
                out["errors"].append(f"Cache write: {e}")
        else:
            cached = self.cache.load()
            if cached:
                age = self.cache.age_minutes(cached)
                cd  = cached.get("data") or {}
                out.update({
                    "locality":          cd.get("locality"),
                    "city":              cd.get("city"),
                    "nearby":            cd.get("nearby", []),
                    "hotspots":          cd.get("hotspots", []),
                    "consensus_aqi":     cd.get("consensus_aqi"),
                    "from_cache":        True,
                    "cache_age_minutes": age,
                })
            else:
                out["errors"].append("All sources failed and no cache found.")

        out["sources"] = [name for name, r in readings.items() if r["ok"]]
        return out
