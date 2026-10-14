import logging

from flask import Flask
from werkzeug.middleware.proxy_fix import ProxyFix

from . import config
from . import db as db_module


def create_app() -> Flask:
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

    from . import request_log
    from .auth import current_username

    request_log.init_app(app)
    app.context_processor(lambda: {"username": current_username()})

    from .services.pair_throttle import REGISTRATIONS_PER_MINUTE, PairThrottle

    app.extensions["pair_throttle"] = PairThrottle()
    app.extensions["register_throttle"] = PairThrottle(max_events=REGISTRATIONS_PER_MINUTE)

    from .routes import (
        devices,
        frames,
        orchestrator,
        pages,
        renderers,
        spotify_oauth,
        spotify_proxy,
        status,
        tfl_search,
    )

    app.register_blueprint(devices.bp)
    app.register_blueprint(renderers.bp)
    app.register_blueprint(spotify_proxy.bp)
    app.register_blueprint(spotify_oauth.bp)
    app.register_blueprint(pages.bp)
    app.register_blueprint(tfl_search.bp)
    app.register_blueprint(frames.bp)
    app.register_blueprint(status.bp)
    app.register_blueprint(orchestrator.bp)

    # The routes of the countdown API contract (vendor/countdown-api). This also checks that the broker answers
    # every one of them, and refuses to start if it doesn't.
    from .api import api

    api.register(app)

    from . import cli

    app.cli.add_command(cli.devices)

    return app
