import logging

from flask import Flask

from . import config
from . import db as db_module


def create_app() -> Flask:
    app = Flask(__name__)
    # Route app.logger through gunicorn's handlers so it lands in `docker logs`.
    gunicorn_logger = logging.getLogger("gunicorn.error")
    if gunicorn_logger.handlers:
        app.logger.handlers = gunicorn_logger.handlers
        app.logger.setLevel(gunicorn_logger.level)

    config.load(app)
    db_module.init_app(app)

    from .routes import devices, frames, pages, spotify_oauth, spotify_proxy, tfl_search

    app.register_blueprint(devices.bp)
    app.register_blueprint(spotify_proxy.bp)
    app.register_blueprint(spotify_oauth.bp)
    app.register_blueprint(pages.bp)
    app.register_blueprint(tfl_search.bp)
    app.register_blueprint(frames.bp)

    from . import cli

    app.cli.add_command(cli.devices)

    return app
