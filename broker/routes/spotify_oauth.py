"""Browser-facing Spotify linking."""

import secrets
from typing import TYPE_CHECKING

from flask import Blueprint, redirect, request, session, url_for

from broker.auth import current_user, user_devices
from broker.clients import spotify

if TYPE_CHECKING:
    from flask.typing import ResponseReturnValue

bp = Blueprint("spotify_oauth", __name__, url_prefix="/auth/spotify")


def _owned(device_id: str | None) -> str | None:
    """Return the device to link: the one asked for if it's the signed-in user's, else their most recently paired."""
    owned = [device.device_id for device in user_devices()]
    if device_id in owned:
        return device_id
    return None if device_id or not owned else owned[0]


@bp.get("/login")
def login() -> ResponseReturnValue:
    """Send the signed-in user to Spotify to approve access for one of their screens."""
    if current_user() is None:
        return "Sign in required.", 401
    device_id = _owned(request.args.get("device"))
    if device_id is None:
        return redirect(url_for("pages.pair"))
    state = secrets.token_urlsafe(16)
    session["spotify_oauth_state"] = state
    # Which screen this is for survives the round trip to Spotify here, not in the URL Spotify gets back.
    session["spotify_oauth_device"] = device_id
    return redirect(spotify.authorize_url(state))


@bp.get("/callback")
def callback() -> ResponseReturnValue:
    """Store the tokens Spotify hands back, for the screen the login was started for."""
    error = request.args.get("error")
    if error:
        return f"Spotify authorization failed: {error}", 400

    if request.args.get("state") != session.pop("spotify_oauth_state", None):
        return "Invalid or expired login attempt, please try connecting again.", 400

    # Checked again: the user may have lost the screen, or signed in as someone else, since they left.
    device_id = _owned(session.pop("spotify_oauth_device", None))
    if not device_id:
        return redirect(url_for("pages.pair"))

    spotify.exchange_code(device_id, request.args.get("code"))
    return redirect(url_for("pages.device_config", device_id=device_id))
