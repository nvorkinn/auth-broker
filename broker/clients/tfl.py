"""Looks up TfL stops (NaPTAN StopPoints) for the settings page.

Both for naming a device's saved stops, and for searching for new ones.
"""

import re
from http import HTTPStatus
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

import requests
from flask import current_app

if TYPE_CHECKING:
    from collections.abc import Iterator

API_BASE = "https://api.tfl.gov.uk"
_STOP_ID = re.compile(r"[A-Za-z0-9_-]+")
# A search ending in what could be a bus stop's letter, as "Westmoreland Road K" or "Westmoreland Road stop K".
_LETTERED_QUERY = re.compile(r"(?P<name>.*?\S)\s+(?:stop\s+)?(?P<letter>[A-Za-z]{1,2}[0-9]?)", re.IGNORECASE)
# How many of a bus stop's routes its subtitle names before "(+n more)".
SUBTITLE_LINES = 6


def _params() -> dict[str, str]:
    app_key = current_app.config["TFL_APP_KEY"]
    return {"app_key": app_key} if app_key else {}


def _get_stop_point(stop_id: str, http: requests.Session) -> dict[str, Any] | None:
    if not _STOP_ID.fullmatch(stop_id):
        current_app.logger.warning("Ignoring invalid TfL stop ID %r", stop_id)
        return None
    response = http.get(f"{API_BASE}/StopPoint/{quote(stop_id, safe='')}", params=_params(), timeout=5)
    return response.json() if response.status_code == HTTPStatus.OK else None


def _walk(node: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """Yield a StopPoint and every stop nested under it (a station's platforms, a hub's bus stops)."""
    yield node
    for child in node.get("children", []):
        yield from _walk(child)


def _node_id(node: dict[str, Any]) -> str | None:
    return node.get("id") or node.get("naptanId")


def _lines(node: dict[str, Any]) -> list[str]:
    return [line.get("name") for line in node.get("lines", []) if line.get("name")]


def _letter(node: dict[str, Any]) -> str:
    return node.get("stopLetter") or node.get("indicator") or ""


def _plain_letter(letter: str) -> str:
    """Reduce a stop letter as typed or as TfL writes it ("K", "->K", "Stop K") to "K", to compare them."""
    return re.sub(r"[^A-Z0-9]", "", letter.upper()).removeprefix("STOP")


def _display_name(node: dict[str, Any], default: str = "") -> str:
    return node.get("commonName", default).removesuffix(" Underground Station").strip()


def _unresolved(stop_id: str) -> dict[str, Any]:
    return {"id": stop_id, "name": stop_id, "mode": "unknown", "letter": "", "lines": [], "line_count": 0}


def resolve_stop(stop_id: str, http: requests.Session) -> dict[str, Any] | None:
    """Details for one NaPTAN ID, formatted for the settings page; None if TfL can't say."""
    try:
        data = _get_stop_point(stop_id, http)
        if data is None:
            return None
        target = next((node for node in _walk(data) if _node_id(node) == stop_id), data)
        lines = _lines(target)
        is_tube = "tube" in target.get("modes", []) or target.get("stopType") == "NaptanMetroStation"
        return {
            "id": stop_id,
            "name": _display_name(target, stop_id),
            "mode": "tube" if is_tube else "bus",
            "letter": _letter(target),
            "lines": lines[:8],
            "line_count": len(lines),
        }
    except Exception:
        current_app.logger.exception("Error resolving stop %s", stop_id)
        return None


def resolve_stops(stop_ids: list[str]) -> list[dict[str, Any]]:
    """resolve_stop for each ID, in order, with a placeholder for any TfL couldn't resolve."""
    http = requests.Session()
    return [resolve_stop(stop_id, http) or _unresolved(stop_id) for stop_id in stop_ids]


def _search_result(node: dict[str, Any]) -> dict[str, Any] | None:
    """Return a selectable Tube station or bus stop as a search result, or None for any other kind of stop."""
    lines = _lines(node)
    if node.get("stopType") == "NaptanMetroStation":
        return {
            "id": _node_id(node),
            "name": _display_name(node),
            "mode": "tube",
            "letter": "",
            "lines": lines,
            "subtitle": f"Underground • {', '.join(lines) if lines else 'All lines'}",
        }
    if node.get("stopType") == "NaptanPublicBusCoachTram":
        letter = _letter(node)
        shown = ", ".join(lines[:SUBTITLE_LINES])
        subtitle = f"Stop {letter} • {shown}" if letter else f"Bus • {shown}"
        if len(lines) > SUBTITLE_LINES:
            subtitle += f" (+{len(lines) - SUBTITLE_LINES} more)"
        return {
            "id": _node_id(node),
            "name": _display_name(node),
            "mode": "bus",
            "letter": letter,
            "lines": lines,
            "subtitle": subtitle,
        }
    return None


def search_stops(query: str) -> list[dict[str, Any]]:
    """Tube stations and bus stops matching the query.

    A query ending in a stop letter ("Westmoreland Road K") lists the stops of that name with that letter, or, if
    there are none, whatever matches the query as typed. Raises if TfL's search itself fails; a match whose details
    can't be fetched is skipped.
    """
    http = requests.Session()
    lettered = _LETTERED_QUERY.fullmatch(query.strip())
    if lettered:
        letter = _plain_letter(lettered["letter"])
        named = _search(lettered["name"], http)
        with_letter = [result for result in named if result["letter"] and _plain_letter(result["letter"]) == letter]
        if with_letter:
            return with_letter
    return _search(query, http)


def _search(query: str, http: requests.Session) -> list[dict[str, Any]]:
    params = {"query": query, "modes": "tube,bus", "maxResults": "15", **_params()}
    response = http.get(f"{API_BASE}/StopPoint/Search", params=params, timeout=6)
    response.raise_for_status()

    results = []
    seen: set[str] = set()
    for match in response.json().get("matches", [])[:8]:
        match_id = match.get("id")
        try:
            data = _get_stop_point(match_id, http)
            if data is None:
                continue
            for node in _walk(data):
                node_id = _node_id(node)
                if not node_id or node_id in seen:
                    continue
                result = _search_result(node)
                if result is not None:
                    seen.add(node_id)
                    results.append(result)
        except Exception:
            current_app.logger.exception("Error parsing search result %s", match_id)
    return results
