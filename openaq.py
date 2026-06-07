"""OpenAQ v3 API client."""
import requests
from openweather import pm25_to_us_aqi

BASE = "https://api.openaq.org/v3"


class OpenAQClient:
    def __init__(self, api_key: str = None):
        self.session = requests.Session()
        if api_key:
            self.session.headers["X-API-Key"] = api_key
        self.session.headers["Accept"] = "application/json"

    def nearest_aqi(self, lat: float, lng: float, radius_m: int = 8000):
        results = self._locations_near(lat, lng, radius_m, limit=10)
        for loc in results:
            pm25 = self._extract_pm25(loc)
            if pm25 is None:
                continue
            coords = loc.get("coordinates") or {}
            return {
                "aqi":      pm25_to_us_aqi(pm25),
                "station":  loc.get("name") or loc.get("locality") or "Unknown",
                "lat":      coords.get("latitude"),
                "lng":      coords.get("longitude"),
                "dominant": "pm25",
                "iaqi":     {"pm25": round(pm25, 1)},
                "time":     self._latest_time(loc),
                "source":   "OpenAQ",
            }
        return None

    def stations_near(self, lat: float, lng: float, radius_m: int = 12000, limit: int = 20):
        results = self._locations_near(lat, lng, radius_m, limit=limit)
        out = []
        for loc in results:
            pm25 = self._extract_pm25(loc)
            if pm25 is None:
                continue
            coords      = loc.get("coordinates") or {}
            distance_m  = loc.get("distance")
            out.append({
                "station":     {"name": loc.get("name") or loc.get("locality") or "Unknown"},
                "aqi":         str(pm25_to_us_aqi(pm25)),
                "aqi_int":     pm25_to_us_aqi(pm25),
                "lat":         coords.get("latitude"),
                "lon":         coords.get("longitude"),
                "distance_km": round(distance_m / 1000, 2) if distance_m else None,
            })
        return out

    def _locations_near(self, lat, lng, radius_m, limit):
        r = self.session.get(
            f"{BASE}/locations",
            params={"coordinates": f"{lat},{lng}", "radius": int(radius_m), "limit": limit, "order_by": "distance"},
            timeout=12,
        )
        r.raise_for_status()
        return r.json().get("results") or []

    @staticmethod
    def _extract_pm25(loc) -> float | None:
        for sensor in (loc.get("sensors") or []):
            name = ((sensor.get("parameter") or {}).get("name") or "").lower()
            if name not in ("pm25", "pm2.5"):
                continue
            value = (sensor.get("latest") or {}).get("value")
            if value is not None:
                return float(value)
        return None

    @staticmethod
    def _latest_time(loc) -> str | None:
        for sensor in (loc.get("sensors") or []):
            t = (sensor.get("latest") or {}).get("datetime") or {}
            utc = t.get("utc") if isinstance(t, dict) else t
            if utc:
                return str(utc)
        return None
