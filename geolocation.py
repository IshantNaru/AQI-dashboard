"""IP-based geolocation. Uses ipapi.co as primary, ip-api.com as fallback."""
import requests


def get_location():
    """
    Returns dict with city, region, country, lat, lng — or None if all sources fail.
    """
    try:
        r = requests.get("https://ipapi.co/json/", timeout=6)
        if r.ok:
            d = r.json()
            if d.get("latitude") is not None:
                return {
                    "city":    d.get("city"),
                    "region":  d.get("region"),
                    "country": d.get("country_name"),
                    "lat":     float(d["latitude"]),
                    "lng":     float(d["longitude"]),
                }
    except Exception:
        pass

    try:
        r = requests.get("http://ip-api.com/json/", timeout=6)
        if r.ok:
            d = r.json()
            if d.get("status") == "success":
                return {
                    "city":    d.get("city"),
                    "region":  d.get("regionName"),
                    "country": d.get("country"),
                    "lat":     float(d["lat"]),
                    "lng":     float(d["lon"]),
                }
    except Exception:
        pass

    return None
