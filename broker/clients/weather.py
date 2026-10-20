"""Looks weather places up with Open-Meteo's geocoding: searching by name, and fetching one by its id.

A place is Open-Meteo's (GeoNames') town or city: {id, name, admin1, country, country_code, latitude, longitude,
label}, label being what the settings page shows ("London, England, United Kingdom").
"""

import logging
from http import HTTPStatus
from typing import Any

import requests

OPEN_METEO_GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
OPEN_METEO_PLACE_URL = "https://geocoding-api.open-meteo.com/v1/get"
# The settings page's picker searches as the user types, so a slow answer is worth less than none.
TIMEOUT = 4
SEARCH_COUNT = 8

logger = logging.getLogger(__name__)


class GeocodingUnavailableError(Exception):
    """Open-Meteo couldn't be asked, or answered with something that isn't a geocoding result."""


def _reason(error: Exception) -> str:
    """Say what went wrong without the error's own text: its type, and an HTTP error's status ("HTTPError 503").

    A failed request's text quotes its URL, with the place's name or id in it, and a place says where a screen, and
    someone's home, is: the logs go to the central log store.
    """
    if isinstance(error, requests.HTTPError) and error.response is not None:
        return f"{type(error).__name__} {error.response.status_code}"
    return type(error).__name__


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

    The first is the one pi-dashboard's weather provider finds for the name too. Raises GeocodingUnavailableError if
    Open-Meteo couldn't be asked.
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
        logger.warning("Couldn't search Open-Meteo for a weather place: %s", _reason(e))  # not which, nor the URL
        raise GeocodingUnavailableError from e


def get_place(place_id: int) -> dict[str, Any] | None:
    """Return the place with Open-Meteo's id, or None if there's no such place.

    Raises GeocodingUnavailableError if Open-Meteo couldn't be asked.
    """
    try:
        response = requests.get(
            OPEN_METEO_PLACE_URL, params={"id": place_id, "language": "en", "format": "json"}, timeout=TIMEOUT
        )
        # An unknown id is a 400 (or 404) with {"error": true, "reason": ...}: not there, rather than unreachable. A 429
        # is unreachable.
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
        logger.warning("Couldn't get a weather place from Open-Meteo: %s", _reason(e))  # not which, nor the URL
        raise GeocodingUnavailableError from e
