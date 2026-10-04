import requests
from flask import current_app

OPEN_METEO_GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"


def location_found(location: str) -> bool | None:
    """Whether Open-Meteo's geocoding finds the location, using the same lookup the device does
    (WeatherClient._geocode in countdown). None if Open-Meteo couldn't be asked."""
    try:
        response = requests.get(
            OPEN_METEO_GEOCODING_URL, params={"name": location, "count": 1, "format": "json"}, timeout=5
        )
        response.raise_for_status()
        return bool(response.json().get("results"))
    except (requests.RequestException, ValueError):
        current_app.logger.warning("Couldn't check weather location %r with Open-Meteo", location, exc_info=True)
        return None
