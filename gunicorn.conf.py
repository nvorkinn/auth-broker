"""Gunicorn's settings, read from the working directory (/app in the image).

Logs go to stdout as JSON, whatever the command line says about --access-logfile / --error-logfile.
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from gunicorn.workers.base import Worker

logger_class = "broker.log_format.GunicornLogger"
logconfig_dict = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"json": {"()": "broker.log_format.JsonFormatter"}},
    "handlers": {
        "stdout": {"class": "logging.StreamHandler", "stream": "ext://sys.stdout", "formatter": "json"},
    },
    "root": {"level": "INFO", "handlers": ["stdout"]},
    "loggers": {
        "gunicorn.error": {"level": "INFO", "handlers": ["stdout"], "propagate": False, "qualname": "gunicorn.error"},
        "gunicorn.access": {"level": "INFO", "handlers": ["stdout"], "propagate": False, "qualname": "gunicorn.access"},
    },
}


def post_worker_init(worker: Worker) -> None:
    """Once the broker has loaded, write the subscriptions and every screen's config to the store from the database.

    Redis keeps nothing on disk, so after it restarts, or the first time a broker that writes the configs starts, this
    puts back what the renderers and providers read; the same as `flask config publish`. Redis being down is logged,
    and the broker serves regardless (the renderers then ask GET /api/config).
    """
    # Here, not at the top: gunicorn reads this file before it loads the app, and the app's own modules with it.
    from broker.cli import publish_store  # noqa: PLC0415

    with worker.wsgi.app_context():  # the app gunicorn loaded (wsgi:app)
        publish_store()
