"""The broker's settings, read from the environment into the app's config."""

import os
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from flask import Flask


def load(app: Flask) -> None:
    """Read the broker's settings from the environment. A missing required one fails startup."""
    app.secret_key = os.environ["FLASK_SECRET_KEY"]
    app.permanent_session_lifetime = timedelta(days=30)
    # Unset (the default) keeps the session cookie host-only. Set it to a parent domain, e.g.
    # ".nikolaivorkinn.com", to share the cookie (it carries only the Spotify OAuth state) with sibling subdomains.
    app.config["SESSION_COOKIE_DOMAIN"] = os.environ.get("SESSION_COOKIE_DOMAIN") or None

    # The request header the login proxy (Caddy forward auth to Authentik) puts the signed-in user's
    # id in. Caddy overwrites it on every browser-facing request; see caddy/Caddyfile.
    app.config["USER_HEADER"] = os.environ.get("AUTH_USER_HEADER", "X-Authentik-Uid")
    # The header with the signed-in user's username, which is only shown and used to prefix a screen's name.
    app.config["USERNAME_HEADER"] = os.environ.get("AUTH_USERNAME_HEADER", "X-Authentik-Username")

    app.config["SPOTIFY_CLIENT_ID"] = os.environ["SPOTIFY_CLIENT_ID"]
    app.config["SPOTIFY_CLIENT_SECRET"] = os.environ["SPOTIFY_CLIENT_SECRET"]
    app.config["SPOTIFY_REDIRECT_URI"] = os.environ["SPOTIFY_REDIRECT_URI"]
    # Apple Music, from a MusicKit key in the owner's Apple Developer account: its Team ID, Key ID and the key itself
    # (the .p8 file's PEM text). Unset (the default) leaves Apple Music off the config page.
    app.config["APPLE_MUSIC_TEAM_ID"] = os.environ.get("APPLE_MUSIC_TEAM_ID", "")
    app.config["APPLE_MUSIC_KEY_ID"] = os.environ.get("APPLE_MUSIC_KEY_ID", "")
    app.config["APPLE_MUSIC_PRIVATE_KEY"] = os.environ.get("APPLE_MUSIC_PRIVATE_KEY", "")
    # The name Apple's sign-in shows the listener.
    app.config["APPLE_MUSIC_APP_NAME"] = os.environ.get("APPLE_MUSIC_APP_NAME", "Countdown")
    app.config["TFL_APP_KEY"] = os.environ.get("TFL_APP_KEY", "")
    app.config["WEATHER_API_KEY"] = os.environ.get("WEATHER_API_KEY", "")
    # Where the IP location data for suggesting a weather location is (see services/geoip.py). The image
    # has it in /app/geodata, which is the default.
    app.config["GEODATA_DIR"] = os.environ.get("GEODATA_DIR", str(Path(__file__).resolve().parent.parent / "geodata"))
    app.config["CREDENTIAL_ENCRYPTION_KEY"] = os.environ["CREDENTIAL_ENCRYPTION_KEY"]
    # Unset (the default) turns GET /api/status/devices off.
    app.config["STATUS_TOKEN"] = os.environ.get("STATUS_TOKEN", "")
    # Bearer token for the orchestrator that starts renderers (/api/orchestrator/...). Unset (the
    # default) turns those endpoints off.
    app.config["ORCHESTRATOR_TOKEN"] = os.environ.get("ORCHESTRATOR_TOKEN", "")

    # QA only: logs every request's headers and the response's status and Location (see request_log.py).
    app.config["LOG_REQUEST_HEADERS"] = os.environ.get("LOG_REQUEST_HEADERS", "").lower() in ("1", "true")
