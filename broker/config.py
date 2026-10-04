import os
from datetime import timedelta

from flask import Flask


def load(app: Flask) -> None:
    """Reads the broker's settings from the environment. A missing required one fails startup."""
    app.secret_key = os.environ["FLASK_SECRET_KEY"]
    app.permanent_session_lifetime = timedelta(days=30)

    app.config["SPOTIFY_CLIENT_ID"] = os.environ["SPOTIFY_CLIENT_ID"]
    app.config["SPOTIFY_CLIENT_SECRET"] = os.environ["SPOTIFY_CLIENT_SECRET"]
    app.config["SPOTIFY_REDIRECT_URI"] = os.environ["SPOTIFY_REDIRECT_URI"]
    app.config["TFL_APP_KEY"] = os.environ.get("TFL_APP_KEY", "")
    app.config["WEATHER_API_KEY"] = os.environ.get("WEATHER_API_KEY", "")
    app.config["CREDENTIAL_ENCRYPTION_KEY"] = os.environ["CREDENTIAL_ENCRYPTION_KEY"]
