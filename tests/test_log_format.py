import json
import logging
import runpy
import sys
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from gunicorn.config import Config

from broker import create_app, provider_loop
from broker.log_format import GunicornLogger, JsonFormatter
from broker.routes import frames

ROOT = Path(__file__).resolve().parent.parent


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


# --- Each record once: logging as each process sets it up, then create_app() -------------------------------------


def _loggers():
    return [logging.root, *(lg for lg in logging.Logger.manager.loggerDict.values() if isinstance(lg, logging.Logger))]


@contextmanager
def _logging_as_a_new_process():
    """Make logging as a process has it before anything sets it up, then put it back as it was.

    pytest's handlers come off the root logger, and every logger is enabled again: the migration tests' alembic
    fileConfig disables each logger it doesn't name, which the broker's own start doesn't do (db.upgrade_db).
    """
    saved = {lg: (list(lg.handlers), lg.level, lg.propagate, lg.disabled) for lg in _loggers()}
    logging.root.handlers = []
    for lg in _loggers():
        lg.disabled = False
    try:
        yield
    finally:
        for lg in _loggers():
            handlers, level, propagate, disabled = saved.get(lg, ([], logging.NOTSET, True, False))
            lg.handlers, lg.propagate, lg.disabled = handlers, propagate, disabled
            lg.setLevel(level)  # which also empties the loggers' cached levels


def _gunicorn():
    """Set logging up as gunicorn does before it loads the app.

    That is gunicorn.conf.py's logger_class, which applies the file's logconfig_dict with dictConfig.
    """
    config = Config()
    for name, value in runpy.run_path(str(ROOT / "gunicorn.conf.py")).items():
        if name in config.settings:
            config.set(name, value)
    config.logger_class(config)


def _flask_command():
    """`flask --app wsgi ...`: nothing sets logging up before wsgi.py calls create_app()."""


@pytest.mark.parametrize(
    ("set_up", "stream"),
    [(_gunicorn, "out"), (_flask_command, "err"), (provider_loop.configure_logging, "out")],
    ids=["gunicorn", "flask-command", "provider"],
)
def test_each_broker_record_is_written_once_as_json_at_info(app, capsys, monkeypatch, set_up, stream):
    # `app` only sets up the environment and database that this second create_app() reads.
    monkeypatch.delenv("LOG_LEVEL", raising=False)
    with _logging_as_a_new_process():
        set_up()
        started = create_app()
        started.logger.debug("below the level")
        started.logger.info("from app.logger")
        frames.logger.info("from a module's logger")

    out, err = capsys.readouterr()
    written, other = (out, err) if stream == "out" else (err, out)
    entries = [json.loads(line) for line in written.splitlines()]  # every line JSON, alembic's too
    assert [entry for entry in entries if entry["logger"].startswith("broker")] == [
        {"level": "info", "_msg": "from app.logger", "logger": "broker"},
        {"level": "info", "_msg": "from a module's logger", "logger": "broker.routes.frames"},
    ]
    assert other == ""
