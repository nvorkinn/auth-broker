import os
from datetime import timedelta

from flask import Flask


def load(app: Flask) -> None:
    """Reads the broker's settings from the environment. A missing required one fails startup."""
    app.secret_key = os.environ["FLASK_SECRET_KEY"]
    app.permanent_session_lifetime = timedelta(days=30)
    # Unset (the default) keeps the session cookie host-only. Set it to a parent domain, e.g.
    # ".nikolaivorkinn.com", to share the cookie (it carries only the Spotify OAuth state) with sibling subdomains.
    app.config["SESSION_COOKIE_DOMAIN"] = os.environ.get("SESSION_COOKIE_DOMAIN") or None

    # The request header the login proxy (Caddy forward auth to Authentik) puts the signed-in user's
    # id in. Caddy overwrites it on every browser-facing request; see caddy/Caddyfile.
    app.config["USER_HEADER"] = os.environ.get("AUTH_USER_HEADER", "X-Authentik-Uid")

    app.config["SPOTIFY_CLIENT_ID"] = os.environ["SPOTIFY_CLIENT_ID"]
    app.config["SPOTIFY_CLIENT_SECRET"] = os.environ["SPOTIFY_CLIENT_SECRET"]
    app.config["SPOTIFY_REDIRECT_URI"] = os.environ["SPOTIFY_REDIRECT_URI"]
    app.config["TFL_APP_KEY"] = os.environ.get("TFL_APP_KEY", "")
    app.config["WEATHER_API_KEY"] = os.environ.get("WEATHER_API_KEY", "")
    app.config["CREDENTIAL_ENCRYPTION_KEY"] = os.environ["CREDENTIAL_ENCRYPTION_KEY"]
    # Unset (the default) turns GET /api/status/devices off.
    app.config["STATUS_TOKEN"] = os.environ.get("STATUS_TOKEN", "")
    # Bearer token for the orchestrator that starts renderers (/api/orchestrator/...). Unset (the
    # default) turns those endpoints off.
    app.config["ORCHESTRATOR_TOKEN"] = os.environ.get("ORCHESTRATOR_TOKEN", "")

    # QA only: logs every request's headers and the response's status and Location (see request_log.py).
    app.config["LOG_REQUEST_HEADERS"] = os.environ.get("LOG_REQUEST_HEADERS", "").lower() in ("1", "true")
