"""Looks weather places up with Open-Meteo's geocoding: searching by name, and fetching one by its id.

A place is Open-Meteo's (GeoNames') town or city: {id, name, admin1, country, country_code, latitude, longitude,
label}, label being what the settings page shows ("London, England, United Kingdom").
"""

from http import HTTPStatus
from typing import Any

import requests
from flask import current_app

OPEN_METEO_GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
OPEN_METEO_PLACE_URL = "https://geocoding-api.open-meteo.com/v1/get"
# The settings page's picker searches as the user types, so a slow answer is worth less than none.
TIMEOUT = 4
SEARCH_COUNT = 8


class GeocodingUnavailableError(Exception):
    """Open-Meteo couldn't be asked, or answered with something that isn't a geocoding result."""


def _place(result: dict[str, Any]) -> dict[str, Any]:
    name = result["name"]
    admin1 = result.get("admin1") or ""
    country = result.get("country") or ""
    # A city-state's region is often the city again ("Oslo, Oslo, Norway"), which says nothing.
    parts = [name, *(part for part in (admin1, country) if part and part != name)]
    return {
        "id": int(result["id"]),
        "name": name,
        "admin1": admin1,
        "country": country,
        "country_code": (result.get("country_code") or "").upper(),
        "latitude": float(result["latitude"]),
        "longitude": float(result["longitude"]),
        "label": ", ".join(dict.fromkeys(parts)),
    }


def search_places(name: str, count: int = SEARCH_COUNT) -> list[dict[str, Any]]:
    """Return the places Open-Meteo finds for a name, best first; [] for none.

    The first is the one a device looking the name up itself (WeatherClient._geocode in countdown) would use.
    Raises GeocodingUnavailableError if Open-Meteo couldn't be asked.
    """
    try:
        response = requests.get(
            OPEN_METEO_GEOCODING_URL,
            params={"name": name, "count": count, "language": "en", "format": "json"},
            timeout=TIMEOUT,
        )
        response.raise_for_status()
        # Open-Meteo leaves "results" out entirely when nothing matches.
        return [_place(result) for result in response.json().get("results") or []]
    except (requests.RequestException, ValueError, KeyError, TypeError) as e:
        current_app.logger.warning("Couldn't search Open-Meteo for weather place %r", name, exc_info=True)
        raise GeocodingUnavailableError from e


def get_place(place_id: int) -> dict[str, Any] | None:
    """Return the place with Open-Meteo's id, or None if there's no such place.

    Raises GeocodingUnavailableError if Open-Meteo couldn't be asked.
    """
    try:
        response = requests.get(
            OPEN_METEO_PLACE_URL, params={"id": place_id, "language": "en", "format": "json"}, timeout=TIMEOUT
        )
        # An unknown id is a 400 (or 404) with {"error": true, "reason": ...}: not there, rather than unreachable.
        # Being rate limited is the latter.
        status = response.status_code
        if (
            HTTPStatus.BAD_REQUEST <= status < HTTPStatus.INTERNAL_SERVER_ERROR
            and status != HTTPStatus.TOO_MANY_REQUESTS
        ):
            return None
        response.raise_for_status()
        result = response.json()
        if not isinstance(result, dict) or result.get("error") or "id" not in result:
            return None
        return _place(result)
    except (requests.RequestException, ValueError, KeyError, TypeError) as e:
        current_app.logger.warning("Couldn't get weather place %s from Open-Meteo", place_id, exc_info=True)
        raise GeocodingUnavailableError from e
