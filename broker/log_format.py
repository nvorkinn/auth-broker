"""The broker's logs as one JSON object per line, with the field names VictoriaLogs reads: `level`
(debug, info, warn, error, fatal) and `_msg`. Fluent Bit ships the line as it is (see the README's
Logs section), so what is written here is what the dashboards see. Used by gunicorn.conf.py."""

import json
import logging

from gunicorn.glogging import Logger

LEVELS = {"WARNING": "warn", "CRITICAL": "fatal"}
SOURCE = "auth-broker"


class JsonFormatter(logging.Formatter):
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
    """Gunicorn's access log with the request as fields instead of a formatted line: the query string
    is left out, as it can carry tokens (the OAuth callback's code, for one)."""

    def access(self, resp, req, environ, request_time):
        if not self.cfg.accesslog and not self.cfg.logconfig_dict:
            return
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
        level = logging.ERROR if status >= 500 else logging.INFO
        self.access_log.log(level, message, extra={"fields": fields})
