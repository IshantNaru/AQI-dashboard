"""
Locality geocoder.

Converts a free-text location query (e.g. "West Patel Nagar Delhi") into
lat/lng coordinates, which are then passed to WAQI's geo endpoint to find
the nearest air quality station.

Primary:  Nominatim (OpenStreetMap) — free, no key, accurate for Indian localities
Fallback: Photon (also OSM-based, separate infrastructure)
"""
import requests

_HEADERS = {"User-Agent": "AQI-Dashboard-Desktop/1.0 (personal air quality monitor)"}
_TIMEOUT = 8


def geocode(query: str, country_bias: str = "IN") -> dict | None:
    """
    Returns {"lat": float, "lng": float, "display_name": str, "source": str} or None.
    """
    if not query or not query.strip():
        return None

    result = _nominatim(query, country_bias)
    if result:
        return result

    return _photon(query, country_bias)


def _nominatim(query: str, country_bias: str) -> dict | None:
    try:
        params = {"q": query, "format": "json", "limit": 1, "addressdetails": 0}
        if country_bias:
            params["countrycodes"] = country_bias.lower()

        r = requests.get(
            "https://nominatim.openstreetmap.org/search",
            params=params, headers=_HEADERS, timeout=_TIMEOUT,
        )
        r.raise_for_status()
        results = r.json()

        if not results and country_bias:
            # Retry without country filter — user may be searching another country
            params.pop("countrycodes")
            r2 = requests.get(
                "https://nominatim.openstreetmap.org/search",
                params=params, headers=_HEADERS, timeout=_TIMEOUT,
            )
            r2.raise_for_status()
            results = r2.json()

        if results:
            top = results[0]
            return {
                "lat":          float(top["lat"]),
                "lng":          float(top["lon"]),
                "display_name": top.get("display_name", query),
                "source":       "Nominatim",
            }
    except Exception:
        pass
    return None


def _photon(query: str, country_bias: str) -> dict | None:
    try:
        q = f"{query} India" if country_bias.upper() == "IN" else query
        r = requests.get(
            "https://photon.komoot.io/api/",
            params={"q": q, "limit": 1},
            headers=_HEADERS, timeout=_TIMEOUT,
        )
        r.raise_for_status()
        features = r.json().get("features") or []
        if features:
            coords = features[0].get("geometry", {}).get("coordinates", [])
            props  = features[0].get("properties", {})
            if len(coords) >= 2:
                name_parts = filter(None, [
                    props.get("name"),
                    props.get("city") or props.get("state"),
                    props.get("country"),
                ])
                return {
                    "lat":          float(coords[1]),
                    "lng":          float(coords[0]),
                    "display_name": ", ".join(name_parts) or query,
                    "source":       "Photon",
                }
    except Exception:
        pass
    return None
