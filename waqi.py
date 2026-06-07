"""
WAQI (aqicn.org) API client.

Endpoints used:
  GET /feed/{city}/?token=...           -> city-level feed
  GET /feed/geo:{lat};{lng}/?token=...  -> nearest-station feed
  GET /feed/@{uid}/?token=...           -> feed by station uid
  GET /map/bounds/?latlng=...&token=... -> all stations in bounding box
"""
import requests


class WAQIClient:
    BASE = "https://api.waqi.info"

    def __init__(self, token: str):
        if not token or token == "YOUR_WAQI_TOKEN_HERE":
            raise ValueError(
                "WAQI token not configured. "
                "Get one free at https://aqicn.org/data-platform/token/"
            )
        self.token = token

    def by_city(self, city: str):
        if not city:
            return None
        r = requests.get(
            f"{self.BASE}/feed/{city}/",
            params={"token": self.token}, timeout=10,
        )
        r.raise_for_status()
        d = r.json()
        if d.get("status") != "ok":
            return None
        return self._parse_feed(d["data"])

    def by_geo(self, lat: float, lng: float):
        r = requests.get(
            f"{self.BASE}/feed/geo:{lat};{lng}/",
            params={"token": self.token}, timeout=10,
        )
        r.raise_for_status()
        d = r.json()
        if d.get("status") != "ok":
            return None
        return self._parse_feed(d["data"])

    def by_uid(self, uid: int):
        r = requests.get(
            f"{self.BASE}/feed/@{uid}/",
            params={"token": self.token}, timeout=10,
        )
        r.raise_for_status()
        d = r.json()
        if d.get("status") != "ok":
            return None
        return self._parse_feed(d["data"])

    def stations_in_bounds(self, latlng_box):
        """
        latlng_box = [lat1, lng1, lat2, lng2].
        Returns list of station dicts with lat, lon, uid, aqi, station.
        """
        r = requests.get(
            f"{self.BASE}/map/bounds/",
            params={
                "latlng":   ",".join(str(x) for x in latlng_box),
                "token":    self.token,
                "networks": "all",
            },
            timeout=15,
        )
        r.raise_for_status()
        d = r.json()
        if d.get("status") != "ok":
            return []
        return d.get("data") or []

    @staticmethod
    def _parse_feed(data):
        city  = data.get("city") or {}
        geo   = city.get("geo") or [None, None]
        iaqi  = data.get("iaqi") or {}
        return {
            "aqi":          data.get("aqi"),
            "station":      city.get("name"),
            "lat":          geo[0] if len(geo) > 0 else None,
            "lng":          geo[1] if len(geo) > 1 else None,
            "dominant":     data.get("dominentpol"),
            "iaqi":         {k: v.get("v") for k, v in iaqi.items() if isinstance(v, dict)},
            "time":         (data.get("time") or {}).get("s"),
            "url":          city.get("url"),
            "attributions": [a.get("name") for a in (data.get("attributions") or []) if isinstance(a, dict)],
        }
