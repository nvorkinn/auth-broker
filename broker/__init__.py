import logging
import os
from datetime import timedelta

from flask import Flask

from . import db as db_module


def create_app() -> Flask:
    app = Flask(__name__)
    # Route app.logger through gunicorn's handlers so it lands in `docker logs`.
    gunicorn_logger = logging.getLogger("gunicorn.error")
    if gunicorn_logger.handlers:
        app.logger.handlers = gunicorn_logger.handlers
        app.logger.setLevel(gunicorn_logger.level)

    app.secret_key = os.environ["FLASK_SECRET_KEY"]
    app.permanent_session_lifetime = timedelta(days=30)

    app.config["SPOTIFY_CLIENT_ID"] = os.environ["SPOTIFY_CLIENT_ID"]
    app.config["SPOTIFY_CLIENT_SECRET"] = os.environ["SPOTIFY_CLIENT_SECRET"]
    app.config["SPOTIFY_REDIRECT_URI"] = os.environ["SPOTIFY_REDIRECT_URI"]
    app.config["TFL_APP_KEY"] = os.environ.get("TFL_APP_KEY", "")
    app.config["WEATHER_API_KEY"] = os.environ.get("WEATHER_API_KEY", "")
    app.config["CREDENTIAL_ENCRYPTION_KEY"] = os.environ["CREDENTIAL_ENCRYPTION_KEY"]

    db_module.init_app(app)

    from . import devices_api, spotify, web

    app.register_blueprint(devices_api.bp)
    app.register_blueprint(spotify.bp)
    app.register_blueprint(web.bp)

    return app
