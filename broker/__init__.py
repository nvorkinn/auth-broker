"""The auth broker: a Flask app that pairs screens with their owners, keeps their settings and serves their frames."""

import os

import redis
from flask import Flask
from werkzeug.middleware.proxy_fix import ProxyFix

from . import cli, config, log_format, request_log
from . import db as db_module
from .auth import current_username
from .routes import (
    apple_music_link,
    devices,
    frames,
    github_dashboard,
    pages,
    place_search,
    spotify_oauth,
    status,
    stop_search,
)
from .services.pair_throttle import MAX_REGISTRATION_LOCKOUT_SECONDS, REGISTRATIONS_PER_MINUTE, PairThrottle


def create_app() -> Flask:
    """Build the app: logging, config, database, throttles, blueprints and the CLI."""
    # Before app.logger is first used, as Flask gives it a plain-text handler of its own if nothing else would write
    # its records.
    log_format.configure_logging()
    app = Flask(__name__)

    # Caddy, on the same host, is the only thing that reaches the app (gunicorn binds 127.0.0.1 only), so trust the
    # one X-Forwarded-For hop it adds: request.remote_addr is then the visitor's IP rather than Caddy's, which the
    # /pair and /register throttles depend on.
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1)

    config.load(app)
    db_module.init_app(app)

    request_log.init_app(app)
    app.context_processor(lambda: {"username": current_username()})

    app.extensions["pair_throttle"] = PairThrottle()
    app.extensions["register_throttle"] = PairThrottle(
        max_events=REGISTRATIONS_PER_MINUTE, max_lockout=MAX_REGISTRATION_LOCKOUT_SECONDS
    )

    app.extensions["redis"] = redis.Redis.from_url(
        os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0"),
        password=os.environ.get("REDIS_PASSWORD"),
        decode_responses=True,
        socket_timeout=1,  # a dead Redis can't hang a config save
        socket_connect_timeout=1,
    )
    # The same Redis for the screens' frames, which are bytes (services/frame_record.py): redis-py decodes per client,
    # not per call.
    app.extensions["redis_bytes"] = redis.Redis.from_url(
        os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0"),
        password=os.environ.get("REDIS_PASSWORD"),
        socket_timeout=1,  # a dead Redis can't hang a screen's poll for long: it's told to ask again
        socket_connect_timeout=1,
    )

    app.register_blueprint(devices.bp)
    app.register_blueprint(spotify_oauth.bp)
    app.register_blueprint(apple_music_link.bp)
    app.register_blueprint(pages.bp)
    app.register_blueprint(github_dashboard.bp)
    app.register_blueprint(stop_search.bp)
    app.register_blueprint(place_search.bp)
    app.register_blueprint(frames.bp)
    app.register_blueprint(status.bp)

    app.cli.add_command(cli.devices)
    app.cli.add_command(cli.weather)
    app.cli.add_command(cli.config)

    return app
