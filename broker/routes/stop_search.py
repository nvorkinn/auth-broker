"""Stop search for the settings page's stop picker, London's and Norway's stops together.

It spends the broker's TfL key, so only a user paired with a device may use it.
"""

from typing import TYPE_CHECKING

from flask import Blueprint, current_app, jsonify, request

from broker.auth import require_paired_user
from broker.services import stops

if TYPE_CHECKING:
    from flask.typing import ResponseReturnValue

bp = Blueprint("stop_search", __name__)

# Shorter queries match too much to be worth searching.
MIN_QUERY_LENGTH = 2


# /api/tfl/search is its old name, kept so a page loaded before the rename still searches.
@bp.get("/api/stops/search")
@bp.get("/api/tfl/search")
@require_paired_user
def search_stops() -> ResponseReturnValue:
    """Return selectable stations and stops matching ?q=, as JSON, each with its source (tfl or entur)."""
    query = request.args.get("q", "").strip()
    if len(query) < MIN_QUERY_LENGTH:
        return jsonify([])

    try:
        return jsonify(stops.search(query))
    except Exception:
        current_app.logger.exception("Stop search error")
        return jsonify([]), 500
