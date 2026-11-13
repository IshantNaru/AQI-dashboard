"""
OpenWeatherMap Air Pollution API client.

Returns US AQI computed from PM2.5 using EPA 2024 NAAQS breakpoints.
"""
import requests

from aqi_math import pm25_to_aqi as pm25_to_us_aqi  # fixed: old table had gaps (9.05 -> 500)


class OpenWeatherClient:
    BASE = "https://api.openweathermap.org/data/2.5/air_pollution"

    def __init__(self, api_key: str):
        if not api_key:
            raise ValueError("OpenWeatherMap API key not provided")
        self.api_key = api_key

    def by_geo(self, lat: float, lng: float):
        r = requests.get(
            self.BASE,
            params={"lat": lat, "lon": lng, "appid": self.api_key},
            timeout=10,
        )
        r.raise_for_status()
        items = r.json().get("list") or []
        if not items:
            return None
        item       = items[0]
        components = item.get("components") or {}
        pm25       = components.get("pm2_5")
        return {
            "aqi":        pm25_to_us_aqi(pm25) if pm25 is not None else None,
            "owm_index":  (item.get("main") or {}).get("aqi"),
            "components": components,
            "time_unix":  item.get("dt"),
        }
