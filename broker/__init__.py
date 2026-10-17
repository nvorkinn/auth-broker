"""The auth broker: a Flask app that pairs screens with their owners and serves them config and frames."""

import logging

from flask import Flask
from werkzeug.middleware.proxy_fix import ProxyFix

from . import api as api_module
from . import cli, config, request_log
from . import db as db_module
from .auth import current_username
from .routes import (
    developer,
    devices,
    frames,
    orchestrator,
    pages,
    renderers,
    spotify_oauth,
    spotify_proxy,
    status,
    stop_search,
)
from .services.pair_throttle import MAX_REGISTRATION_LOCKOUT_SECONDS, REGISTRATIONS_PER_MINUTE, PairThrottle


def create_app() -> Flask:
    """Build the app: logging, config, database, throttles, blueprints and the CLI."""
    app = Flask(__name__)
    # Route app.logger through gunicorn's handlers so it lands in `docker logs`.
    gunicorn_logger = logging.getLogger("gunicorn.error")
    if gunicorn_logger.handlers:
        app.logger.handlers = gunicorn_logger.handlers
        app.logger.setLevel(gunicorn_logger.level)

    # Caddy, on the same host, is the only thing that reaches the app (the port is published on
    # 127.0.0.1 only), so trust the one X-Forwarded-For hop it adds: request.remote_addr is then the
    # visitor's IP rather than Caddy's, which the /pair throttle depends on.
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1)

    config.load(app)
    db_module.init_app(app)

    request_log.init_app(app)
    app.context_processor(lambda: {"username": current_username()})

    app.extensions["pair_throttle"] = PairThrottle()
    app.extensions["register_throttle"] = PairThrottle(
        max_events=REGISTRATIONS_PER_MINUTE, max_lockout=MAX_REGISTRATION_LOCKOUT_SECONDS
    )

    app.register_blueprint(devices.bp)
    app.register_blueprint(renderers.bp)
    app.register_blueprint(spotify_proxy.bp)
    app.register_blueprint(spotify_oauth.bp)
    app.register_blueprint(developer.bp)
    app.register_blueprint(pages.bp)
    app.register_blueprint(stop_search.bp)
    app.register_blueprint(frames.bp)
    app.register_blueprint(status.bp)
    app.register_blueprint(orchestrator.bp)

    # The routes of the countdown API contract (vendor/countdown-api). This also checks that the broker answers
    # every one of them, and refuses to start if it doesn't.
    api_module.api.register(app)

    app.cli.add_command(cli.devices)

    return app
