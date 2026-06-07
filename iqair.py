"""IQAir AirVisual API client (optional)."""
import requests

POLLUTANT_MAP = {"p2": "pm25", "p1": "pm10", "o3": "o3", "n2": "no2", "s2": "so2", "co": "co"}


class IQAirClient:
    def __init__(self, api_key: str):
        if not api_key:
            raise ValueError("IQAir API key required")
        self.api_key = api_key

    def nearest_city(self, lat: float, lng: float):
        r = requests.get(
            "https://api.airvisual.com/v2/nearest_city",
            params={"lat": lat, "lon": lng, "key": self.api_key},
            timeout=10,
        )
        r.raise_for_status()
        body = r.json()
        if body.get("status") != "success":
            raise RuntimeError(f"IQAir error: {body.get('data') or body.get('message')}")
        data      = body.get("data") or {}
        pollution = (data.get("current") or {}).get("pollution") or {}
        aqi_us    = pollution.get("aqius")
        if aqi_us is None:
            return None
        location  = data.get("location", {}).get("coordinates") or []
        city_name = ", ".join(filter(None, [data.get("city"), data.get("state"), data.get("country")]))
        return {
            "aqi":      int(aqi_us),
            "station":  city_name or "IQAir nearest city",
            "lat":      location[1] if len(location) > 1 else None,
            "lng":      location[0] if len(location) > 0 else None,
            "dominant": POLLUTANT_MAP.get(pollution.get("mainus", ""), ""),
            "iaqi":     {},
            "time":     pollution.get("ts"),
            "source":   "IQAir",
        }
