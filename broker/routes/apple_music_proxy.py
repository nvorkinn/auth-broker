"""Apple Music data for the Pi, in the shapes of /api/spotify's, so the screen draws it with the same panels.

The broker holds each device's Apple Music token and makes the calls itself, so a device only ever presents its own
secret, never an Apple token.
"""

import logging
from typing import TYPE_CHECKING

import requests
from flask import Blueprint, jsonify, request

from broker.auth import require_device_auth
from broker.db import db
from broker.models import DeviceConfig
from broker.services import music

if TYPE_CHECKING:
    from flask.typing import ResponseReturnValue

logger = logging.getLogger(__name__)

bp = Blueprint("apple_music_proxy", __name__, url_prefix="/api/apple-music")

# The most the top lists hand over, like Spotify's.
MAX_TOP = 50


def _switched_on(device_id: str) -> bool | None:
    """Whether the device shows Apple Music; None if it has no config."""
    config = db.session.get(DeviceConfig, device_id)
    if config is None:
        return None
    return config.apple_music_enabled


@bp.get("/queue")
@require_device_auth(roles={"renderer"})
def queue(device_id: str) -> ResponseReturnValue:
    """Return the last played track and the ones before it, as Spotify's queue: currently_playing and queue.

    Apple Music has no now playing or queue, so this is the nearest it has. currently_playing is null when it's
    switched off, not connected, or nothing has been played. 502 if Apple can't be reached, so the screen keeps what
    it last showed.
    """
    switched_on = _switched_on(device_id)
    if switched_on is None:
        return jsonify(error="not found"), 404
    if not switched_on:
        return jsonify(music.nothing_playing())
    try:
        return jsonify(music.apple_music_queue(device_id))
    except requests.RequestException as e:
        logger.warning("Apple Music for device %s: %s", device_id, e)
        return jsonify(error="Apple Music could not be reached"), 502


@bp.get("/top/<any(artists, tracks):type_>")
@require_device_auth(roles={"renderer"})
def top(device_id: str, type_: str) -> ResponseReturnValue:
    """Return the most played artists or tracks over time_range, as Spotify's /me/top, counted by the broker.

    Takes Spotify's time_range (short_term, medium_term, long_term) and limit. Empty when switched off or not
    connected.
    """
    switched_on = _switched_on(device_id)
    if switched_on is None:
        return jsonify(error="not found"), 404
    limit = min(max(request.args.get("limit", 20, type=int), 1), MAX_TOP)
    if not switched_on:
        return jsonify(items=[])
    return jsonify(music.apple_music_top(device_id, type_, request.args.get("time_range", "medium_term"), limit))
