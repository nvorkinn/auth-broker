import secrets
import urllib.parse
from datetime import UTC, datetime

import requests
from flask import Blueprint, current_app, redirect, request, session, url_for

from .auth import require_paired_session
from .db import get_db

bp = Blueprint("spotify", __name__, url_prefix="/auth/spotify")

SCOPE = "user-read-currently-playing user-read-playback-state"
TOKEN_URL = "https://accounts.spotify.com/api/token"


@bp.get("/login")
@require_paired_session
def login():
    state = secrets.token_urlsafe(16)
    session["spotify_oauth_state"] = state
    params = {
        "client_id": current_app.config["SPOTIFY_CLIENT_ID"],
        "response_type": "code",
        "redirect_uri": current_app.config["SPOTIFY_REDIRECT_URI"],
        "scope": SCOPE,
        "state": state,
    }
    return redirect(f"https://accounts.spotify.com/authorize?{urllib.parse.urlencode(params)}")


@bp.get("/callback")
def callback():
    error = request.args.get("error")
    if error:
        return f"Spotify authorization failed: {error}", 400

    if request.args.get("state") != session.pop("spotify_oauth_state", None):
        return "Invalid or expired login attempt, please try connecting again.", 400

    device_id: str | None = session.get("device_id")
    if not device_id:
        return redirect(url_for("web.pair"))

    response = requests.post(
        TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "code": request.args.get("code"),
            "redirect_uri": current_app.config["SPOTIFY_REDIRECT_URI"],
        },
        auth=(current_app.config["SPOTIFY_CLIENT_ID"], current_app.config["SPOTIFY_CLIENT_SECRET"]),
        timeout=10,
    )
    response.raise_for_status()
    _store_tokens(device_id, response.json())

    return redirect(url_for("web.device_config"))


def _store_tokens(device_id: str, payload: dict) -> None:
    expires_at = datetime.now(UTC).timestamp() + payload["expires_in"]
    refresh_token = payload.get("refresh_token") or _existing_refresh_token(device_id)

    db = get_db()
    db.execute(
        """
        INSERT INTO spotify_tokens (device_id, refresh_token, access_token, expires_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(device_id) DO UPDATE SET
            refresh_token = excluded.refresh_token,
            access_token = excluded.access_token,
            expires_at = excluded.expires_at
        """,
        (device_id, refresh_token, payload["access_token"], expires_at),
    )
    db.commit()


def _existing_refresh_token(device_id: str) -> str | None:
    row = get_db().execute("SELECT refresh_token FROM spotify_tokens WHERE device_id = ?", (device_id,)).fetchone()
    return row["refresh_token"] if row else None


def _refresh_access_token(device_id: str, refresh_token: str) -> str:
    response = requests.post(
        TOKEN_URL,
        data={"grant_type": "refresh_token", "refresh_token": refresh_token},
        auth=(current_app.config["SPOTIFY_CLIENT_ID"], current_app.config["SPOTIFY_CLIENT_SECRET"]),
        timeout=10,
    )
    response.raise_for_status()
    payload = response.json()
    _store_tokens(device_id, payload)
    return payload["access_token"]


def _get_access_token(device_id: str) -> str | None:
    """Returns a valid access token for the device, refreshing it if needed, or
    None when the device hasn't linked Spotify."""
    row = get_db().execute("SELECT * FROM spotify_tokens WHERE device_id = ?", (device_id,)).fetchone()
    if row is None:
        return None

    if row["expires_at"] is None or float(row["expires_at"]) - 30 < datetime.now(UTC).timestamp():
        return _refresh_access_token(device_id, row["refresh_token"])
    return row["access_token"]


def get_current_track(device_id: str) -> dict[str, object] | None:
    """Mirrors the shape of countdown's SpotifyClient.get_current_track(), so the
    Pi side can eventually swap a direct spotipy call for a call to this broker
    without touching the display code that consumes it."""
    access_token = _get_access_token(device_id)
    if access_token is None:
        return None

    response = requests.get(
        "https://api.spotify.com/v1/me/player/currently-playing",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=10,
    )
    if response.status_code == 204 or not response.content:
        return None
    response.raise_for_status()

    return response.json()


def get_queue(device_id: str) -> list[dict[str, object]]:
    """Upcoming items (not the one currently playing), in play order. Empty when
    Spotify isn't linked or nothing is queued."""
    access_token = _get_access_token(device_id)
    if access_token is None:
        return []

    response = requests.get(
        "https://api.spotify.com/v1/me/player/queue",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=10,
    )
    if response.status_code == 204 or not response.content:
        return []
    response.raise_for_status()
    return response.json()


def get_users_top_items(device_id: str, type_: str, params) -> list[dict[str, object]] | None:
    access_token = _get_access_token(device_id)
    if access_token is None:
        return None
    response = requests.get(
        f"https://api.spotify.com/v1/me/top/{type_}",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=10,
        params=params,
    )
    if response.status_code == 204 or not response.content:
        return []
    response.raise_for_status()
    return response.json()
