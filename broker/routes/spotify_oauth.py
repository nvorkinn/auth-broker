"""Browser-facing Spotify linking: sends the recipient to Spotify to approve access, then
stores the tokens Spotify hands back against the device the signed-in user is paired with."""

import secrets

from flask import Blueprint, redirect, request, session, url_for

from ..auth import paired_device_id, require_paired_user
from ..clients import spotify

bp = Blueprint("spotify_oauth", __name__, url_prefix="/auth/spotify")


@bp.get("/login")
@require_paired_user
def login():
    state = secrets.token_urlsafe(16)
    session["spotify_oauth_state"] = state
    return redirect(spotify.authorize_url(state))


@bp.get("/callback")
def callback():
    error = request.args.get("error")
    if error:
        return f"Spotify authorization failed: {error}", 400

    if request.args.get("state") != session.pop("spotify_oauth_state", None):
        return "Invalid or expired login attempt, please try connecting again.", 400

    device_id = paired_device_id()
    if not device_id:
        return redirect(url_for("pages.pair"))

    spotify.exchange_code(device_id, request.args.get("code"))
    return redirect(url_for("pages.device_config"))
