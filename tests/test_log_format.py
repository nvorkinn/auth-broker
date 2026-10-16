import json
import logging
import sys
from datetime import timedelta
from types import SimpleNamespace

import pytest

from broker.log_format import GunicornLogger, JsonFormatter


def _format(level=logging.INFO, msg="hello %s", args=("world",), exc_info=None, **extra):
    record = logging.LogRecord("some.logger", level, __file__, 1, msg, args, exc_info)
    record.__dict__.update(extra)
    return json.loads(JsonFormatter().format(record))


def test_a_record_is_one_json_object_with_level_and_msg():
    assert _format() == {"level": "info", "_msg": "hello world", "logger": "some.logger"}


@pytest.mark.parametrize(
    ("level", "expected"),
    [
        (logging.DEBUG, "debug"),
        (logging.INFO, "info"),
        (logging.WARNING, "warn"),
        (logging.ERROR, "error"),
        (logging.CRITICAL, "fatal"),
    ],
)
def test_levels_are_the_ones_victoria_logs_dashboards_use(level, expected):
    assert _format(level)["level"] == expected


def _boom():
    msg = "boom"
    raise ValueError(msg)


def test_a_traceback_stays_in_one_record():
    try:
        _boom()
    except ValueError:
        entry = _format(logging.ERROR, "failed", (), exc_info=sys.exc_info())

    assert "Traceback" in entry["exception"]
    assert entry["exception"].endswith("ValueError: boom")
    assert entry["_msg"] == "failed"


def test_fields_are_added_to_the_record():
    assert _format(fields={"status": 200})["status"] == 200


def _access(status, query=b"", sent=5):
    class Logger(GunicornLogger):
        def __init__(self):
            self.cfg = SimpleNamespace(accesslog="-", logconfig_dict={})
            self.access_log = logging.getLogger("test.access")

    records = []
    handler = logging.Handler()
    handler.emit = records.append
    handler.setLevel(logging.DEBUG)
    logging.getLogger("test.access").addHandler(handler)
    logging.getLogger("test.access").setLevel(logging.DEBUG)
    try:
        req = SimpleNamespace(method="GET", path="/auth/spotify/callback", query=query.decode())
        resp = SimpleNamespace(status=f"{status} X", sent=sent)
        Logger().access(resp, req, {"REMOTE_ADDR": "1.2.3.4"}, timedelta(milliseconds=12.34))
    finally:
        logging.getLogger("test.access").removeHandler(handler)
    [record] = records
    return record, json.loads(JsonFormatter().format(record))


def test_the_access_log_has_the_request_as_fields_without_the_query_string():
    _record, entry = _access(200, query=b"code=SECRET")

    assert entry == {
        "level": "info",
        "_msg": "GET /auth/spotify/callback 200",
        "logger": "test.access",
        "method": "GET",
        "path": "/auth/spotify/callback",
        "status": 200,
        "bytes": 5,
        "duration_ms": 12.3,
        "remote": "1.2.3.4",
    }
    assert "SECRET" not in json.dumps(entry)


def test_a_server_error_in_the_access_log_is_an_error():
    assert _access(503)[1]["level"] == "error"
