"""Looks up Norwegian stops (Entur's NSR stop places and quays) for the settings page: naming a device's saved
stops, and searching for new ones. Entur covers every operator in Norway, Ruter in Oslo among them, and needs no key,
only an ET-Client-Name header (https://developer.entur.org/pages-intro-authentication)."""

import re
from typing import Any

import requests
from flask import current_app

GEOCODER_URL = "https://api.entur.io/geocoder/v1/autocomplete"
JOURNEY_PLANNER_URL = "https://api.entur.io/journey-planner/v3/graphql"
HEADERS = {"ET-Client-Name": "nvorkinn-countdown"}
_STOP_ID = re.compile(r"NSR:(StopPlace|Quay):\d+")

# A stop's modes, as the settings page shows them: the panel's icons go by the same ones.
MODE_NAMES = {"metro": "T-bane", "tram": "Tram", "bus": "Bus", "rail": "Train", "water": "Boat", "coach": "Coach"}
# Majorstuen's quays are its platforms; a bus stop's are its stop poles.
PLATFORM_MODES = {"metro", "rail"}

_QUAY_FIELDS = """
      id
      name
      publicCode
      description
      lines { publicCode transportMode }
      estimatedCalls(numberOfDepartures: 6) { destinationDisplay { frontText } }
"""

_STOP_PLACES_QUERY = f"""
query StopPlaces($ids: [String]!) {{
  stopPlaces(ids: $ids) {{
    id
    name
    transportMode
    quays {{ {_QUAY_FIELDS} }}
  }}
}}
"""

_QUAYS_QUERY = f"""
query Quays($ids: [String]!) {{
  quays(ids: $ids) {{
    {_QUAY_FIELDS}
    stopPlace {{ id name transportMode }}
  }}
}}
"""


def is_stop_id(stop_id: str) -> bool:
    return bool(_STOP_ID.fullmatch(stop_id))


def _query(query: str, variables: dict[str, Any], http: requests.Session) -> dict[str, Any]:
    response = http.post(JOURNEY_PLANNER_URL, json={"query": query, "variables": variables}, headers=HEADERS, timeout=6)
    response.raise_for_status()
    payload = response.json()
    if payload.get("errors"):
        raise RuntimeError(", ".join(error.get("message", "") for error in payload["errors"]))
    return payload.get("data") or {}


def _modes(raw: Any) -> list[str]:
    return [m for m in (raw if isinstance(raw, list) else [raw] if raw else []) if isinstance(m, str)]


def _mode(modes: list[str]) -> str:
    """The stop's mode for its icon: its first (Entur lists the main one first), or bus."""
    return modes[0] if modes else "bus"


def _lines(quays: list[dict[str, Any]]) -> list[str]:
    lines: list[str] = []
    for quay in quays:
        for line in quay.get("lines") or []:
            code = line.get("publicCode")
            if code and code not in lines:
                lines.append(code)
    return sorted(lines, key=lambda code: (len(code), code))


def _destinations(quay: dict[str, Any]) -> list[str]:
    destinations: list[str] = []
    for call in quay.get("estimatedCalls") or []:
        text = (call.get("destinationDisplay") or {}).get("frontText")
        if text and text not in destinations:
            destinations.append(text)
    return destinations[:3]


def _quay_label(quay: dict[str, Any], mode: str) -> str:
    code = quay.get("publicCode")
    if not code:
        return "Stop"
    return f"Platform {code}" if mode in PLATFORM_MODES else f"Stop {code}"


def _stop_place_result(place: dict[str, Any], area: str = "") -> dict[str, Any]:
    modes = _modes(place.get("transportMode"))
    mode = _mode(modes)
    quays = place.get("quays") or []
    lines = _lines(quays)
    kinds = " & ".join(MODE_NAMES.get(m, m.title()) for m in modes) or "Stop"
    subtitle = f"{kinds} • All platforms" if len(quays) > 1 else kinds
    if lines:
        subtitle += f" • {', '.join(lines[:6])}" + (f" (+{len(lines) - 6} more)" if len(lines) > 6 else "")
    return {
        "id": place["id"],
        "source": "entur",
        "name": place.get("name") or place["id"],
        "area": area,
        "mode": mode,
        "letter": "",
        "lines": lines,
        "subtitle": subtitle,
        "indent": False,
    }


def _quay_result(quay: dict[str, Any], place_name: str, mode: str) -> dict[str, Any]:
    lines = _lines([quay])
    parts = [_quay_label(quay, mode)]
    destinations = _destinations(quay)
    if destinations:
        parts.append(f"towards {', '.join(destinations)}")
    elif quay.get("description"):
        parts.append(quay["description"])
    if lines:
        parts.append(", ".join(lines[:6]))
    return {
        "id": quay["id"],
        "source": "entur",
        "name": place_name,
        "area": "",
        "mode": mode,
        "letter": quay.get("publicCode") or "",
        "lines": lines,
        "subtitle": " • ".join(parts),
        "indent": True,
    }


def search_stops(query: str) -> list[dict[str, Any]]:
    """Norwegian stations and stops matching the query, each followed by its platforms (or stop poles), indented
    under it when it has more than one. Raises if Entur's search itself fails."""
    http = requests.Session()
    response = http.get(
        GEOCODER_URL,
        params={"text": query, "layers": "venue", "size": 6, "boundary.country": "NOR", "lang": "no"},
        headers=HEADERS,
        timeout=6,
    )
    response.raise_for_status()
    areas: dict[str, str] = {}
    for feature in response.json().get("features", []):
        props = feature.get("properties") or {}
        stop_id = props.get("id") or ""
        if stop_id.startswith("NSR:StopPlace:") and stop_id not in areas:
            areas[stop_id] = props.get("locality") or props.get("county") or ""
    if not areas:
        return []

    places = {p["id"]: p for p in _query(_STOP_PLACES_QUERY, {"ids": list(areas)}, http).get("stopPlaces") or [] if p}
    results = []
    for stop_id, area in areas.items():
        place = places.get(stop_id)
        if place is None:
            continue
        results.append(_stop_place_result(place, area))
        quays = place.get("quays") or []
        mode = _mode(_modes(place.get("transportMode")))
        if len(quays) > 1:
            ordered = sorted(quays, key=lambda q: (len(q.get("publicCode") or ""), q.get("publicCode") or ""))
            results += [_quay_result(quay, place.get("name") or "", mode) for quay in ordered]
    return results


def resolve_stops(stop_ids: list[str]) -> dict[str, dict[str, Any]]:
    """Details for saved stops, by id, formatted for the settings page. An id Entur can't say anything about is
    left out; so are all of them if Entur can't be reached."""
    http = requests.Session()
    resolved: dict[str, dict[str, Any]] = {}
    places = [s for s in stop_ids if s.startswith("NSR:StopPlace:")]
    quays = [s for s in stop_ids if s.startswith("NSR:Quay:")]
    try:
        if places:
            for place in _query(_STOP_PLACES_QUERY, {"ids": places}, http).get("stopPlaces") or []:
                if place:
                    resolved[place["id"]] = _stop_place_result(place)
        if quays:
            for quay in _query(_QUAYS_QUERY, {"ids": quays}, http).get("quays") or []:
                if quay:
                    parent = quay.get("stopPlace") or {}
                    mode = _mode(_modes(parent.get("transportMode")))
                    resolved[quay["id"]] = _quay_result(quay, parent.get("name") or quay.get("name") or "", mode)
    except Exception:
        current_app.logger.exception("Error resolving Entur stops %s", stop_ids)
    return resolved
