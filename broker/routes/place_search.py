"""Town and city search for the settings page's weather place picker, from Open-Meteo's geocoding.

Open-Meteo needs no key, but the search is still only for a user paired with a device, like the stop search.
"""

from typing import TYPE_CHECKING

from flask import Blueprint, jsonify, request

from broker.auth import require_paired_user
from broker.clients import weather

if TYPE_CHECKING:
    from flask.typing import ResponseReturnValue

bp = Blueprint("place_search", __name__)

# Shorter queries match too much to be worth searching.
MIN_QUERY_LENGTH = 2


@bp.get("/api/places/search")
@require_paired_user
def search_places() -> ResponseReturnValue:
    """Return the towns and cities matching ?q=, best first, as JSON.

    Each is {id, name, admin1, country, country_code, latitude, longitude, label}. Open-Meteo being unreachable
    gives [] like no match does: the picker has nothing to show either way, and the logs say which it was.
    """
    query = request.args.get("q", "").strip()
    if len(query) < MIN_QUERY_LENGTH:
        return jsonify([])
    try:
        return jsonify(weather.search_places(query))
    except weather.GeocodingUnavailableError:
        return jsonify([])
