import json
import logging

import pytest

from broker import request_log


class _Records(logging.Handler):
    def __init__(self):
        super().__init__(logging.INFO)
        self.messages = []

    def emit(self, record):
        self.messages.append(record.getMessage())


@pytest.fixture
def caplog(app):
    """Capture the app logger's own records.

    The migration tests leave that logger disabled (alembic's fileConfig does), so it's switched back
    on here.
    """
    handler = _Records()
    previous = app.logger.level, app.logger.disabled
    app.logger.addHandler(handler)
    app.logger.setLevel(logging.INFO)
    app.logger.disabled = False
    yield handler
    app.logger.removeHandler(handler)
    app.logger.level, app.logger.disabled = previous


def _logged(caplog):
    return [json.loads(m.removeprefix("request ")) for m in caplog.messages if m.startswith("request ")]


def test_nothing_is_logged_by_default(client, caplog):
    client.get("/pair")
    assert _logged(caplog) == []


def test_every_request_is_logged_with_its_headers_and_the_redirect(app, client, caplog):
    app.config["LOG_REQUEST_HEADERS"] = True
    request_log.init_app(app)

    client.set_cookie("a", "b")
    client.get("/?x=1", headers={"Authorization": "Bearer s3cret", "X-Test": "yes"})

    [entry] = _logged(caplog)
    assert (entry["method"], entry["path"], entry["query"], entry["status"]) == ("GET", "/", "x=1", 302)
    assert entry["location"] == "/pair"
    assert entry["headers"]["X-Authentik-Uid"] == "user-1"
    assert entry["headers"]["X-Test"] == "yes"
    assert entry["headers"]["Authorization"] == "REDACTED"
    assert entry["headers"]["Cookie"] == "REDACTED"
