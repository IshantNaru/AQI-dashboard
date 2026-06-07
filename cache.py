"""JSON cache — saves last successful fetch, loaded when all live sources fail."""
import json
import os
from datetime import datetime

CACHE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "aqi_cache.json")


class AQICache:
    def __init__(self, path=CACHE_PATH):
        self.path = path

    def save(self, lat, lng, city, data):
        payload = {
            "last_updated": datetime.now().isoformat(),
            "lat": lat, "lng": lng, "city": city,
            "data": {k: v for k, v in data.items() if k not in ("errors",)},
        }
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, default=str)

    def load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return None

    def age_minutes(self, cached) -> float | None:
        if not cached:
            return None
        try:
            last = datetime.fromisoformat(cached["last_updated"])
            return round((datetime.now() - last).total_seconds() / 60, 1)
        except Exception:
            return None
