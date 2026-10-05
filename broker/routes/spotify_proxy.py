"""Spotify data for the Pi. The broker holds each device's Spotify tokens and makes the calls
itself, so a device only ever presents its own secret, never a Spotify token."""

import functools

from flask import Blueprint, jsonify, request

from ..auth import require_device_auth
from ..clients import spotify
from ..db import db
from ..models import DeviceConfig

bp = Blueprint("spotify_proxy", __name__, url_prefix="/api/devices")


def require_spotify_enabled(default):
    """Answers with `default` (as JSON) instead of running the view when the device has
    Spotify switched off. Apply it below @require_device_auth so unauthenticated callers
    can't probe device settings."""

    def decorator(view):
        @functools.wraps(view)
        def wrapped(device_id, *args, **kwargs):
            config = db.session.get(DeviceConfig, device_id)
            if config is None:
                return jsonify(error="not found"), 404
            if not config.spotify_enabled:
                return jsonify(default)
            return view(device_id, *args, **kwargs)

        return wrapped

    return decorator


@bp.get("/<device_id>/now-playing")
@require_device_auth(roles={"renderer"})
@require_spotify_enabled(default=None)
def now_playing(device_id):
    """Polled by the Pi in place of talking to Spotify directly."""
    return jsonify(spotify.get_current_track(device_id))


@bp.get("/<device_id>/queue")
@require_device_auth(roles={"renderer"})
@require_spotify_enabled(default=[])
def queue(device_id):
    """Upcoming Spotify queue, same auth and token handling as now-playing.
    Always a JSON list; empty when Spotify is disabled, unlinked or nothing is queued."""
    return jsonify(spotify.get_queue(device_id))


@bp.get("/<device_id>/top/<any(artists, tracks):type_>")
@require_device_auth(roles={"renderer"})
@require_spotify_enabled(default=None)
def top(device_id: str, type_: str):
    """The user's top artists or tracks; any other type is a 404 rather than an arbitrary Spotify path."""
    return jsonify(spotify.get_users_top_items(device_id, type_, request.args.to_dict()))
