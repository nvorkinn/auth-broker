"""The broker's logs as one JSON object per line.

The field names are the ones VictoriaLogs reads: `level` (debug, info, warn, error, fatal) and `_msg`. Fluent
Bit ships the line as it is (see the README's Logs section), so what is written here is what the dashboards see.
Used by gunicorn.conf.py.
"""

import json
import logging
from http import HTTPStatus
from typing import TYPE_CHECKING, override

from gunicorn.glogging import Logger

if TYPE_CHECKING:
    from collections.abc import Mapping
    from datetime import timedelta

    from gunicorn.http.message import Request
    from gunicorn.http.wsgi import Response

LEVELS = {"WARNING": "warn", "CRITICAL": "fatal"}
SOURCE = "auth-broker"


class JsonFormatter(logging.Formatter):
    """A record as a JSON line: its level, message and logger, and any `fields` passed in `extra`."""

    @override
    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "level": LEVELS.get(record.levelname, record.levelname.lower()),
            "_msg": record.getMessage(),
            "logger": record.name,
            **getattr(record, "fields", {}),
        }
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str)


class GunicornLogger(Logger):
    """Gunicorn's access log with the request as fields instead of a formatted line.

    The query string is left out, as it can carry tokens (the OAuth callback's code, for one).
    """

    @override
    def access(self, resp: Response, req: Request, environ: Mapping[str, object], request_time: timedelta) -> None:
        status = int(str(resp.status).split()[0])
        fields = {
            "method": req.method,
            "path": req.path,
            "status": status,
            "bytes": getattr(resp, "sent", 0),
            "duration_ms": round(request_time.total_seconds() * 1000, 1),
            "remote": environ.get("REMOTE_ADDR"),
        }
        message = f"{req.method} {req.path} {status}"
        level = logging.ERROR if status >= HTTPStatus.INTERNAL_SERVER_ERROR else logging.INFO
        self.access_log.log(level, message, extra={"fields": fields})
