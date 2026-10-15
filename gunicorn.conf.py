# Read by gunicorn from the working directory (/app in the image). Logs go to stdout as JSON, whatever
# the command line says about --access-logfile / --error-logfile.
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
