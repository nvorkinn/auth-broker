"""Talks to Spotify on a device's behalf: the OAuth token exchange, keeping each device's tokens
fresh, and the Web API calls the device-facing endpoints proxy."""

import urllib.parse
from datetime import UTC, datetime

import requests
from flask import current_app

from ..db import db
from ..models import SpotifyToken

SCOPE = "user-read-currently-playing user-read-playback-state user-top-read"
AUTHORIZE_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"


def authorize_url(state: str) -> str:
    params = {
        "client_id": current_app.config["SPOTIFY_CLIENT_ID"],
        "response_type": "code",
        "redirect_uri": current_app.config["SPOTIFY_REDIRECT_URI"],
        "scope": SCOPE,
        "state": state,
    }
    return f"{AUTHORIZE_URL}?{urllib.parse.urlencode(params)}"


def exchange_code(device_id: str, code: str | None) -> None:
    """Swaps the code from Spotify's OAuth callback for tokens and stores them against the device."""
    response = requests.post(
        TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": current_app.config["SPOTIFY_REDIRECT_URI"],
        },
        auth=_client_credentials(),
        timeout=10,
    )
    response.raise_for_status()
    _store_tokens(device_id, response.json())


def _client_credentials() -> tuple[str, str]:
    return current_app.config["SPOTIFY_CLIENT_ID"], current_app.config["SPOTIFY_CLIENT_SECRET"]


def _store_tokens(device_id: str, payload: dict) -> None:
    expires_at = datetime.now(UTC).timestamp() + payload["expires_in"]
    refresh_token = payload.get("refresh_token") or _existing_refresh_token(device_id)

    token = db.session.get(SpotifyToken, device_id) or SpotifyToken(device_id=device_id)
    token.refresh_token = refresh_token
    token.access_token = payload["access_token"]
    token.expires_at = expires_at
    db.session.add(token)
    db.session.commit()


def _existing_refresh_token(device_id: str) -> str | None:
    token = db.session.get(SpotifyToken, device_id)
    return token.refresh_token if token else None


def _refresh_access_token(device_id: str, refresh_token: str) -> str:
    response = requests.post(
        TOKEN_URL,
        data={"grant_type": "refresh_token", "refresh_token": refresh_token},
        auth=_client_credentials(),
        timeout=10,
    )
    response.raise_for_status()
    payload = response.json()
    _store_tokens(device_id, payload)
    return payload["access_token"]


def _get_access_token(device_id: str) -> str | None:
    """Returns a valid access token for the device, refreshing it if needed, or
    None when the device hasn't linked Spotify."""
    token = db.session.get(SpotifyToken, device_id)
    if token is None:
        return None

    if token.expires_at is None or token.expires_at - 30 < datetime.now(UTC).timestamp():
        return _refresh_access_token(device_id, token.refresh_token)
    return token.access_token


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
