"""
OpenWeatherMap Air Pollution API client.

Returns US AQI computed from PM2.5 using EPA 2024 NAAQS breakpoints.
"""
import requests

PM25_BREAKPOINTS = [
    (0.0,   9.0,   0,   50),
    (9.1,   35.4,  51,  100),
    (35.5,  55.4,  101, 150),
    (55.5,  125.4, 151, 200),
    (125.5, 225.4, 201, 300),
    (225.5, 325.4, 301, 500),
]


def pm25_to_us_aqi(pm25: float) -> int:
    if pm25 is None:
        return None
    if pm25 < 0:
        return 0
    for c_lo, c_hi, i_lo, i_hi in PM25_BREAKPOINTS:
        if c_lo <= pm25 <= c_hi:
            return round(((i_hi - i_lo) / (c_hi - c_lo)) * (pm25 - c_lo) + i_lo)
    return 500


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
