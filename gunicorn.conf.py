"""Gunicorn's settings, read from the working directory (/app in the image).

Logs go to stdout as JSON, whatever the command line says about --access-logfile / --error-logfile.
"""

# At run time, not under TYPE_CHECKING: gunicorn checks each hook's arity with inspect.signature, which on Python 3.14
# evaluates the hook's annotations, so a name only imported for type checkers makes the broker fail to start.
from gunicorn.workers.base import Worker  # noqa: TC002

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
    """Once the broker has loaded, write the store from the database, then start the minute's resync.

    Redis keeps nothing on disk, so this puts back what the render pool and providers read, as `flask config publish`
    does; Redis being down is logged and the broker serves regardless. The resync thread (config_store.start_resync)
    brings back a Redis that restarts while the broker runs.
    """
    # Here, not at the top: gunicorn reads this file before it loads the app, and the app's own modules with it.
    from broker.cli import publish_store  # noqa: PLC0415
    from broker.services import config_store  # noqa: PLC0415

    with worker.wsgi.app_context():  # the app gunicorn loaded (wsgi:app)
        publish_store()
    config_store.start_resync(worker.wsgi)
