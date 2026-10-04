"""Stop search for the settings page's stop picker. It spends the broker's TfL key, so only a
browser paired with a device may use it."""

from flask import Blueprint, current_app, jsonify, request

from ..auth import require_paired_session
from ..clients import tfl

bp = Blueprint("tfl_search", __name__, url_prefix="/api/tfl")


@bp.get("/search")
@require_paired_session
def search_stops():
    """Selectable Tube stations and bus stops matching ?q=, as JSON."""
    query = request.args.get("q", "").strip()
    if len(query) < 2:
        return jsonify([])

    try:
        return jsonify(tfl.search_stops(query))
    except Exception:
        current_app.logger.exception("TfL Search API error")
        return jsonify([]), 500
