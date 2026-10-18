import json
import re
import threading
from datetime import UTC, datetime, timedelta
from hashlib import blake2b
from unittest.mock import patch

import fakeredis
import pytest
import redis
from sqlalchemy import inspect, select

from broker.db import db
from broker.models import Device, DeviceConfig, Frame
from broker.routes import frames
from broker.routes.frames import FRAME_BYTES
from broker.services import pace
from broker.services.pace import PACE_LEASE_S

SECRET = "a-very-long-device-secret-value"  # register_device's default


def _frame(fill: int = 0xAA) -> bytes:
    return bytes([fill]) * FRAME_BYTES


def _etag(frame: bytes) -> str:
    return blake2b(frame, digest_size=8).hexdigest()


def _url(device_id):
    return "/api/frame"


def _auth(secret=SECRET):
    return {"Authorization": f"Bearer {secret}"}


def _put(client, device_id, frame, headers=None, secret=SECRET):
    return client.put(_url(device_id), data=frame, headers=_auth(secret) if headers is None else headers)


def _get(client, device_id, secret, **headers):
    return client.get(_url(device_id), headers={"Authorization": f"Bearer {secret}", **headers})


def _stored_rows(app, device_id):
    with app.app_context():
        return db.session.scalars(select(Frame).where(Frame.device_id == device_id)).all()


def test_frame_size_is_one_bit_packed_800x480():
    assert FRAME_BYTES == 48_000


# --- PUT: renderer auth ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": SECRET},
        {"Authorization": f"Basic {SECRET}"},
        {"Authorization": f"bearer {SECRET}"},
        {"Authorization": "Bearer "},
        {"Authorization": "Bearer wrong-token"},
        {"Authorization": f"Bearer {SECRET}-extra"},
        {"Authorization": f"Bearer {SECRET[:-1]}"},
    ],
    ids=["missing", "no-scheme", "basic", "lowercase-scheme", "empty", "wrong", "longer", "prefix"],
)
def test_put_rejects_bad_renderer_auth(app, client, register_device, headers):
    device_id, _ = register_device()

    response = _put(client, device_id, _frame(), headers=headers)

    assert response.status_code == 401
    assert response.get_json() == {"error": "unauthorized"}
    assert _stored_rows(app, device_id) == []


def test_put_rejects_the_display_secret(app, client, split_device):
    device_id, _, display_secret = split_device

    response = _put(client, device_id, _frame(), secret=display_secret)

    assert response.status_code == 403
    assert response.get_json() == {"error": "forbidden"}
    assert _stored_rows(app, device_id) == []


def test_put_works_for_an_attached_renderer(app, client, split_device):
    device_id, renderer_secret, _ = split_device

    assert _put(client, device_id, _frame(), secret=renderer_secret).status_code == 204
    assert _stored_rows(app, device_id)[0].frame == _frame()


def test_put_auth_is_checked_before_frame_size(client, register_device):
    device_id, _ = register_device()
    response = _put(client, device_id, b"short", headers={})
    assert response.status_code == 401


# --- PUT: storing frames --------------------------------------------------------------------------


def test_put_stores_frame_with_etag_and_timestamp(app, client, register_device):
    device_id, _ = register_device()
    frame = _frame()

    response = _put(client, device_id, frame)

    assert response.status_code == 204
    assert response.data == b""
    [row] = _stored_rows(app, device_id)
    assert row.frame == frame
    assert row.etag == _etag(frame)
    assert re.fullmatch(r"[0-9a-f]{16}", row.etag)
    assert row.rendered_at.tzinfo == UTC
    assert timedelta(0) <= datetime.now(UTC) - row.rendered_at < timedelta(seconds=5)


def test_put_ignores_request_content_type(app, client, register_device):
    device_id, _ = register_device()
    frame = _frame()

    response = client.put(_url(device_id), data=frame, headers={**_auth(), "Content-Type": "application/json"})

    assert response.status_code == 204
    assert _stored_rows(app, device_id)[0].frame == frame


def test_put_preserves_arbitrary_bytes(app, client, register_device):
    device_id, _ = register_device()
    frame = bytes(i % 256 for i in range(FRAME_BYTES))

    assert _put(client, device_id, frame).status_code == 204

    assert _stored_rows(app, device_id)[0].frame == frame


@pytest.mark.parametrize("size", [0, 1, FRAME_BYTES - 1, FRAME_BYTES + 1, FRAME_BYTES * 2])
def test_put_rejects_wrong_frame_size(app, client, register_device, size):
    device_id, _ = register_device()

    response = _put(client, device_id, b"\x00" * size)

    assert response.status_code == 400
    assert _stored_rows(app, device_id) == []


def test_put_with_wrong_size_keeps_the_previous_frame(app, client, register_device):
    device_id, _ = register_device()
    frame = _frame()
    _put(client, device_id, frame)

    assert _put(client, device_id, b"\x00" * 10).status_code == 400

    assert _stored_rows(app, device_id)[0].frame == frame


def test_put_replaces_existing_frame(app, client, register_device):
    device_id, _ = register_device()
    _put(client, device_id, _frame(0x00))
    [first] = _stored_rows(app, device_id)
    response = _put(client, device_id, _frame(0xFF))

    assert response.status_code == 204
    [row] = _stored_rows(app, device_id)
    assert row.frame == _frame(0xFF)
    assert row.etag == _etag(_frame(0xFF))
    assert row.rendered_at > first.rendered_at


def test_put_same_frame_again_still_moves_rendered_at(app, client, register_device):
    device_id, _ = register_device()
    _put(client, device_id, _frame())
    [first] = _stored_rows(app, device_id)

    assert _put(client, device_id, _frame()).status_code == 204

    [row] = _stored_rows(app, device_id)
    assert row.etag == first.etag
    assert row.rendered_at > first.rendered_at


def test_put_same_frame_twice_keeps_the_same_etag(app, client, register_device):
    device_id, _ = register_device()
    _put(client, device_id, _frame())
    first = _stored_rows(app, device_id)[0].etag

    _put(client, device_id, _frame())

    assert _stored_rows(app, device_id)[0].etag == first


def test_different_frames_get_different_etags(app, client, register_device):
    device_id, _ = register_device()
    _put(client, device_id, _frame(0x00))
    first = _stored_rows(app, device_id)[0].etag

    _put(client, device_id, _frame(0x01))

    assert _stored_rows(app, device_id)[0].etag != first


def test_put_only_touches_the_target_device(app, client, register_device):
    device_a, _ = register_device()
    device_b, secret_b = register_device("another-very-long-device-secret")
    _put(client, device_a, _frame(0x11))
    _put(client, device_b, _frame(0x22), secret=secret_b)

    _put(client, device_a, _frame(0x33))

    assert _stored_rows(app, device_a)[0].frame == _frame(0x33)
    assert _stored_rows(app, device_b)[0].frame == _frame(0x22)


def test_put_for_unknown_device_is_unauthorized_and_stores_nothing(app, client):
    response = _put(client, "does-not-exist", _frame())

    assert response.status_code == 401
    assert _stored_rows(app, "does-not-exist") == []


def test_put_for_forgotten_device_is_unauthorized(app, client, register_device, cli):
    device_id, _ = register_device()
    cli("forget", device_id)

    assert _put(client, device_id, _frame()).status_code == 401
    assert _stored_rows(app, device_id) == []


@pytest.mark.parametrize("method", ["patch", "delete"])
def test_frame_endpoint_rejects_other_methods(client, register_device, method):
    device_id, _ = register_device()
    response = getattr(client, method)(_url(device_id), headers=_auth())
    assert response.status_code == 405


# --- GET: device auth -----------------------------------------------------------------------------


def test_get_requires_auth(client, register_device):
    device_id, _ = register_device()
    _put(client, device_id, _frame())

    response = client.get(_url(device_id))

    assert response.status_code == 401
    assert response.get_json() == {"error": "unauthorized"}


def test_get_rejects_wrong_secret(client, register_device):
    device_id, _ = register_device()
    _put(client, device_id, _frame())

    assert _get(client, device_id, "wrong-secret").status_code == 401


def test_get_works_for_both_roles(client, split_device):
    device_id, renderer_secret, display_secret = split_device
    _put(client, device_id, _frame(), secret=renderer_secret)

    assert _get(client, device_id, display_secret).data == _frame()
    assert _get(client, device_id, renderer_secret).data == _frame()


def test_get_unknown_device_is_unauthorized_not_not_found(client):
    assert _get(client, "does-not-exist", "a-very-long-device-secret-value").status_code == 401


# --- GET: serving frames --------------------------------------------------------------------------


def test_get_returns_404_before_any_frame_is_rendered(client, register_device):
    device_id, secret = register_device()
    assert _get(client, device_id, secret).status_code == 404


def test_get_returns_the_stored_frame(client, register_device):
    device_id, secret = register_device()
    frame = bytes(i % 256 for i in range(FRAME_BYTES))
    _put(client, device_id, frame)

    response = _get(client, device_id, secret)

    assert response.status_code == 200
    assert response.data == frame
    assert response.mimetype == "application/octet-stream"
    assert response.content_length == FRAME_BYTES
    assert response.headers["ETag"] == f'"{_etag(frame)}"'


def test_get_returns_the_latest_frame_after_an_update(client, register_device):
    device_id, secret = register_device()
    _put(client, device_id, _frame(0x00))
    _put(client, device_id, _frame(0xFF))

    response = _get(client, device_id, secret)

    assert response.data == _frame(0xFF)
    assert response.headers["ETag"] == f'"{_etag(_frame(0xFF))}"'


def test_get_only_returns_the_devices_own_frame(client, register_device):
    device_a, secret_a = register_device()
    device_b, secret_b = register_device("another-very-long-device-secret")
    _put(client, device_a, _frame(0x11))

    assert _get(client, device_a, secret_a).data == _frame(0x11)
    assert _get(client, device_b, secret_b).status_code == 404


def test_head_returns_headers_without_body(client, register_device):
    device_id, secret = register_device()
    _put(client, device_id, _frame())

    response = client.head(_url(device_id), headers={"Authorization": f"Bearer {secret}"})

    assert response.status_code == 200
    assert response.data == b""
    assert response.headers["ETag"] == f'"{_etag(_frame())}"'


# --- GET: conditional requests --------------------------------------------------------------------


def test_get_with_matching_etag_returns_304(client, register_device):
    device_id, secret = register_device()
    _put(client, device_id, _frame())
    etag = _get(client, device_id, secret).headers["ETag"]

    response = _get(client, device_id, secret, **{"If-None-Match": etag})

    assert response.status_code == 304
    assert response.data == b""
    assert response.headers["ETag"] == etag


def test_get_with_stale_etag_returns_new_frame(client, register_device):
    device_id, secret = register_device()
    _put(client, device_id, _frame(0x00))
    stale = _get(client, device_id, secret).headers["ETag"]
    _put(client, device_id, _frame(0xFF))

    response = _get(client, device_id, secret, **{"If-None-Match": stale})

    assert response.status_code == 200
    assert response.data == _frame(0xFF)
    assert response.headers["ETag"] != stale


def test_get_with_unchanged_frame_rerendered_still_returns_304(client, register_device):
    device_id, secret = register_device()
    _put(client, device_id, _frame())
    etag = _get(client, device_id, secret).headers["ETag"]
    _put(client, device_id, _frame())

    assert _get(client, device_id, secret, **{"If-None-Match": etag}).status_code == 304


def test_get_with_one_of_several_etags_matching_returns_304(client, register_device):
    device_id, secret = register_device()
    _put(client, device_id, _frame())
    etag = _get(client, device_id, secret).headers["ETag"]

    response = _get(client, device_id, secret, **{"If-None-Match": f'"0000000000000000", {etag}'})

    assert response.status_code == 304


def test_get_with_wildcard_if_none_match_returns_304(client, register_device):
    device_id, secret = register_device()
    _put(client, device_id, _frame())

    assert _get(client, device_id, secret, **{"If-None-Match": "*"}).status_code == 304


def test_conditional_get_still_requires_auth(client, register_device):
    device_id, secret = register_device()
    _put(client, device_id, _frame())
    etag = _get(client, device_id, secret).headers["ETag"]

    response = client.get(_url(device_id), headers={"If-None-Match": etag})

    assert response.status_code == 401


def test_conditional_get_before_any_frame_is_404(client, register_device):
    device_id, secret = register_device()
    assert _get(client, device_id, secret, **{"If-None-Match": "*"}).status_code == 404


# --- schema and admin -----------------------------------------------------------------------------


def test_schema_creates_frames_table(app):
    with app.app_context():
        inspector = inspect(db.engine)
        columns = {column["name"]: column for column in inspector.get_columns("frames")}
        primary_key = inspector.get_pk_constraint("frames")["constrained_columns"]
    assert set(columns) == {"device_id", "frame", "etag", "rendered_at", "pace", "pace_until"}
    assert primary_key == ["device_id"]
    assert str(columns["frame"]["type"]) == "BLOB"
    assert not any(columns[name]["nullable"] for name in ("frame", "etag", "rendered_at"))


def test_cli_forget_deletes_the_devices_frame(app, client, register_device, cli):
    device_id, _ = register_device()
    other_id, other_secret = register_device("another-very-long-device-secret")
    _put(client, device_id, _frame())
    _put(client, other_id, _frame(), secret=other_secret)

    assert cli("forget", device_id).exit_code == 0

    assert _stored_rows(app, device_id) == []
    assert len(_stored_rows(app, other_id)) == 1
    with app.app_context():
        assert db.session.get(Device, device_id) is None


def test_cli_unpair_keeps_the_devices_frame(app, client, register_device, cli):
    device_id, secret = register_device()
    _put(client, device_id, _frame())

    assert cli("unpair", device_id).exit_code == 0

    assert _stored_rows(app, device_id)[0].frame == _frame()
    assert _get(client, device_id, secret).data == _frame()


def test_cli_unpair_with_config_deletes_the_devices_frame(app, client, register_device, cli):
    device_id, secret = register_device()
    other_id, other_secret = register_device("another-very-long-device-secret")
    _put(client, device_id, _frame())
    _put(client, other_id, _frame(), secret=other_secret)

    assert cli("unpair", device_id, "--config").exit_code == 0

    assert _stored_rows(app, device_id) == []
    assert _get(client, device_id, secret).status_code == 404
    assert len(_stored_rows(app, other_id)) == 1


def test_renderer_can_upload_again_after_unpair_with_config(client, register_device, cli):
    device_id, secret = register_device()
    _put(client, device_id, _frame(0x00))
    cli("unpair", device_id, "--config")

    assert _put(client, device_id, _frame(0xFF)).status_code == 204
    assert _get(client, device_id, secret).data == _frame(0xFF)


# --- POST: frame plus telemetry ---------------------------------------------------------------------

LOGS_URL = "http://127.0.0.1:9428/insert/jsonline?_stream_fields=service,host&_msg_field=_msg&_time_field=_time"
METRICS_URL = "http://127.0.0.1:8428/write"
LINES = "esp,device_id={device_id} uptime_s=3600i,rssi=-60i"


class _Response:
    def __init__(self, status=204):
        self.status = status

    def raise_for_status(self):
        if self.status >= 400:
            msg = f"{self.status} error"
            raise frames.requests.HTTPError(msg)


class _Inline:
    """Stands in for the executor, so forwarding has finished by the time the response is back."""

    def submit(self, fn, *args):
        fn(*args)


@pytest.fixture
def shipped(monkeypatch):
    """Run forwarding inline and record each (url, kwargs) the broker posts."""
    calls = []

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        return _Response()

    monkeypatch.setattr(frames, "_executor", _Inline())
    monkeypatch.setattr(frames.requests, "post", fake_post)
    return calls


def _post(client, body=None, secret=SECRET, path="/api/frame", **headers):
    return client.post(path, json=body, headers={"Authorization": f"Bearer {secret}", **headers})


def _metrics_calls(shipped):
    return [kwargs for url, kwargs in shipped if url == METRICS_URL]


def _logs_calls(shipped):
    return [kwargs for url, kwargs in shipped if url == LOGS_URL]


def _log_records(shipped):
    """Return the records of the one logs call, decoded from its JSON lines."""
    [call] = _logs_calls(shipped)
    assert call["headers"] == {"Content-Type": "application/json"}
    return [json.loads(line) for line in call["data"].decode().split("\n")]


def test_post_requires_auth(client, shipped):
    response = client.post("/api/frame", json={"metrics": LINES, "logs": ["x"]})

    assert response.status_code == 401
    assert shipped == []


def test_post_returns_the_frame_like_get(client, register_device, shipped):
    device_id, _ = register_device()
    _put(client, device_id, _frame())

    response = _post(client, {"metrics": "", "logs": []})

    assert response.status_code == 200
    assert response.data == _frame()
    assert response.headers["ETag"] == f'"{_etag(_frame())}"'
    assert "Retry-After" in response.headers


def test_post_before_any_frame_is_404_with_retry_after_but_still_ships(client, register_device, shipped, frozen):
    register_device()

    response = _post(client, {"metrics": LINES, "logs": ["boot"]})

    assert response.status_code == 404
    assert response.headers["Retry-After"] == "23"  # at 12:00:10, until 12:00:33
    assert len(shipped) == 2


def test_post_honours_if_none_match(client, register_device, shipped):
    device_id, _ = register_device()
    _put(client, device_id, _frame())

    response = _post(client, None, **{"If-None-Match": f'"{_etag(_frame())}"'})

    assert response.status_code == 304
    assert response.headers["ETag"] == f'"{_etag(_frame())}"'


# --- POST: metrics, straight to VictoriaMetrics -----------------------------------------------------


def test_post_sends_metrics_to_victoria_metrics_with_the_device_id_filled_in(client, register_device, shipped):
    device_id, _ = register_device()

    _post(client, {"metrics": LINES})

    assert shipped == [
        (
            METRICS_URL,
            {
                "data": f"esp,device_id={device_id} uptime_s=3600i,rssi=-60i".encode(),
                "headers": {"Content-Type": "text/plain"},
                "timeout": 2,
            },
        )
    ]


def test_post_fills_in_every_placeholder_on_every_line(client, register_device, shipped):
    device_id, _ = register_device()
    metrics = "a,device_id={device_id} x=1i\nb,device_id={device_id},peer={device_id} y=2i 1700000000000000000"

    _post(client, {"metrics": metrics})

    [call] = _metrics_calls(shipped)
    assert call["data"] == (
        f"a,device_id={device_id} x=1i\nb,device_id={device_id},peer={device_id} y=2i 1700000000000000000".encode()
    )


def test_post_passes_metrics_without_a_placeholder_through(client, register_device, shipped):
    register_device()

    _post(client, {"metrics": "esp,device_id=fixed x=1i"})

    assert _metrics_calls(shipped)[0]["data"] == b"esp,device_id=fixed x=1i"


@pytest.mark.parametrize("stray", ["{0}", "{}", "{other}", "}{", "{device_id.__class__}", "{device_id!r:>9}"])
def test_post_leaves_other_braces_alone(client, register_device, shipped, stray):
    device_id, _ = register_device()
    metrics = f'esp,device_id={{device_id}} note="{stray}"'

    response = _post(client, {"metrics": metrics})

    assert response.status_code == 404  # no frame yet; the point is it isn't a 500
    assert _metrics_calls(shipped)[0]["data"] == f'esp,device_id={device_id} note="{stray}"'.encode()


def test_post_sends_non_ascii_metrics_as_utf8(client, register_device, shipped):
    device_id, _ = register_device()

    _post(client, {"metrics": 'esp,device_id={device_id} ssid="caf\u00e9"'})

    assert _metrics_calls(shipped)[0]["data"] == f'esp,device_id={device_id} ssid="caf\u00e9"'.encode()


@pytest.mark.parametrize("metrics", [None, {}, {"rssi": -60}, [], ["esp x=1i"], 5, True, "", "  \n\t"])
def test_post_ignores_metrics_that_are_not_a_line_protocol_string(client, register_device, shipped, metrics):
    register_device()

    assert _post(client, {"metrics": metrics}).status_code == 404

    assert shipped == []


# --- POST: logs, to VictoriaLogs --------------------------------------------------------------------


def test_post_sends_logs_to_victoria_logs_as_json_lines_for_the_device(client, register_device, shipped):
    device_id, _ = register_device()

    _post(client, {"logs": [{"_msg": "a", "level": "warn", "uptime_ms": 5}, "plain", 7]})

    records = _log_records(shipped)
    assert [(r["_msg"], r["level"]) for r in records] == [("a", "warn"), ("plain", "info"), ("7", "info")]
    assert {(r["service"], r["host"], r["device_id"]) for r in records} == {("countdown-esp32", device_id, device_id)}
    assert records[0]["uptime_ms"] == 5
    assert _logs_calls(shipped)[0]["timeout"] == 2


def test_post_dates_each_log_by_its_uptime_relative_to_the_newest(client, register_device, shipped):
    register_device()
    before = datetime.now(UTC)

    _post(client, {"logs": [{"_msg": "old", "uptime_ms": 1000}, {"_msg": "new", "uptime_ms": 4000}, "no uptime"]})

    old, new, undated = (datetime.fromisoformat(r["_time"]) for r in _log_records(shipped))
    assert (new - old).total_seconds() == pytest.approx(3.0)
    assert before <= new <= datetime.now(UTC)
    assert before <= undated <= datetime.now(UTC)


@pytest.mark.parametrize(
    ("sent", "expected"),
    [("WARNING", "warn"), ("critical", "fatal"), ("Error", "error"), ("nonsense", "info"), (3, "info")],
)
def test_post_normalises_the_level_of_a_log(client, register_device, shipped, sent, expected):
    register_device()

    _post(client, {"logs": [{"_msg": "x", "level": sent}]})

    assert _log_records(shipped)[0]["level"] == expected


@pytest.mark.parametrize("key", ["log", "message", "msg"])
def test_post_accepts_the_other_names_for_a_log_message(client, register_device, shipped, key):
    register_device()

    _post(client, {"logs": [{key: "hello"}]})

    assert _log_records(shipped)[0]["_msg"] == "hello"


def test_post_overrides_what_a_screen_claims_about_where_a_log_came_from(client, register_device, shipped):
    device_id, _ = register_device()

    _post(
        client,
        {"logs": [{"_msg": "a", "device_id": "x", "host": "y", "service": "auth-broker", "_time": "2000-01-01"}]},
    )

    [record] = _log_records(shipped)
    assert (record["device_id"], record["host"], record["service"]) == (device_id, device_id, "countdown-esp32")
    assert record["_time"] != "2000-01-01"


@pytest.mark.parametrize("logs", [None, [], "text", {"log": "x"}, 5])
def test_post_ignores_logs_that_are_not_a_non_empty_list(client, register_device, shipped, logs):
    register_device()

    assert _post(client, {"logs": logs}).status_code == 404

    assert shipped == []


# --- POST: both, and the body around them -----------------------------------------------------------


def test_post_ships_metrics_and_logs_to_their_own_destinations(client, register_device, shipped):
    register_device()

    _post(client, {"metrics": LINES, "logs": ["x"]})

    assert [url for url, _ in shipped] == [METRICS_URL, LOGS_URL]


def test_post_only_ships_what_it_was_sent(client, register_device, shipped):
    register_device()

    _post(client, {"logs": ["only logs"]})
    assert [url for url, _ in shipped] == [LOGS_URL]

    shipped.clear()
    _post(client, {"metrics": LINES})
    assert [url for url, _ in shipped] == [METRICS_URL]


@pytest.mark.parametrize("body", [None, {}, [], "text", 5], ids=["no-body", "empty", "list", "string", "number"])
def test_post_ignores_a_body_that_is_not_a_report(client, register_device, shipped, body):
    device_id, _ = register_device()
    _put(client, device_id, _frame())

    assert _post(client, body).status_code == 200
    assert shipped == []


def test_post_with_non_json_body_still_serves_the_frame(client, register_device, shipped):
    device_id, secret = register_device()
    _put(client, device_id, _frame())

    response = client.post("/api/frame", data=b"\xff\x00", headers=_auth(secret))

    assert response.status_code == 200
    assert shipped == []


def test_post_uses_destinations_from_the_environment(client, register_device, shipped, monkeypatch):
    monkeypatch.setenv("VICTORIA_METRICS_URL", "http://vm.example:8428/write")
    monkeypatch.setenv("VICTORIA_LOGS_URL", "http://vl.example:9428/insert/jsonline")
    register_device()

    _post(client, {"metrics": LINES, "logs": ["x"]})

    assert [url for url, _ in shipped] == ["http://vm.example:8428/write", "http://vl.example:9428/insert/jsonline"]


# --- POST: a destination that's down or unhappy never touches the screen ----------------------------


@pytest.fixture
def failing(monkeypatch):
    """Make every forward fail the given way; return the list of attempted URLs."""
    attempts = []

    def install(effect):
        def fake_post(url, **kwargs):
            attempts.append(url)
            if isinstance(effect, Exception):
                raise effect
            return _Response(effect)

        monkeypatch.setattr(frames, "_executor", _Inline())
        monkeypatch.setattr(frames.requests, "post", fake_post)
        return attempts

    return install


@pytest.mark.parametrize(
    "effect",
    [
        pytest.param("connection", id="connection-refused"),
        pytest.param("timeout", id="timeout"),
        pytest.param(400, id="rejected-line"),
        pytest.param(503, id="unavailable"),
    ],
)
def test_post_survives_a_failing_destination(client, register_device, failing, caplog, effect):
    effect = {
        "connection": frames.requests.ConnectionError("refused"),
        "timeout": frames.requests.Timeout("slow"),
    }.get(effect, effect)
    attempts = failing(effect)
    device_id, _ = register_device()
    _put(client, device_id, _frame())

    response = _post(client, {"metrics": LINES, "logs": ["x"]})

    assert response.status_code == 200
    assert response.data == _frame()
    assert attempts == [METRICS_URL, LOGS_URL]
    assert f"could not forward to {METRICS_URL}" in caplog.text
    assert f"could not forward to {LOGS_URL}" in caplog.text


def test_one_failing_destination_does_not_stop_the_other(client, register_device, monkeypatch):
    sent = []

    def fake_post(url, **kwargs):
        if url == METRICS_URL:
            msg = "down"
            raise frames.requests.ConnectionError(msg)
        sent.append(url)
        return _Response()

    monkeypatch.setattr(frames, "_executor", _Inline())
    monkeypatch.setattr(frames.requests, "post", fake_post)
    register_device()

    _post(client, {"metrics": LINES, "logs": ["x"]})

    assert sent == [LOGS_URL]


def test_post_does_not_wait_for_a_slow_destination(client, register_device, monkeypatch):
    """The real executor hands the request to a worker thread; the response doesn't depend on it."""
    release, started = threading.Event(), threading.Event()

    def slow(url, **kwargs):
        started.set()
        release.wait(5)
        return _Response()

    monkeypatch.setattr(frames.requests, "post", slow)
    device_id, _ = register_device()
    _put(client, device_id, _frame())

    try:
        response = _post(client, {"metrics": LINES})
        assert response.status_code == 200
        assert started.wait(2)
    finally:
        release.set()


# --- POST /api/frame: the same report on the route a screen uses without knowing its device_id ------


def test_post_on_the_id_less_route_returns_the_frame_and_ships_the_report(client, register_device, shipped):
    device_id, _ = register_device()
    _put(client, device_id, _frame())

    response = _post(client, {"metrics": LINES, "logs": ["x"]}, path="/api/frame")

    assert response.status_code == 200
    assert response.data == _frame()
    assert response.headers["ETag"] == f'"{_etag(_frame())}"'
    assert _metrics_calls(shipped)[0]["data"] == f"esp,device_id={device_id} uptime_s=3600i,rssi=-60i".encode()
    assert _log_records(shipped)[0]["device_id"] == device_id


def test_post_on_the_id_less_route_honours_if_none_match(client, register_device, shipped):
    device_id, _ = register_device()
    _put(client, device_id, _frame())

    response = _post(client, None, path="/api/frame", **{"If-None-Match": f'"{_etag(_frame())}"'})

    assert response.status_code == 304


def test_post_on_the_id_less_route_requires_auth(client, shipped):
    response = client.post("/api/frame", json={"metrics": LINES})

    assert response.status_code == 401
    assert shipped == []


def test_post_on_the_id_less_route_uses_the_secrets_own_device(client, register_device, split_device, shipped):
    register_device("another-very-long-device-secret")
    device_id, _, display_secret = split_device

    _post(client, {"metrics": LINES}, secret=display_secret, path="/api/frame")

    assert _metrics_calls(shipped)[0]["data"] == f"esp,device_id={device_id} uptime_s=3600i,rssi=-60i".encode()


# --- Retry-After: the pacing function's answer (services/pace.py) on every poll ------------------------------------
#
# The clock is frozen at 12:00:10 UTC (`frozen`), so without a hint every answer says 23 (until 12:00:33), and with a
# 20 s hint 13 (until 12:00:23: 12:00:13 is under MIN_SLEEP_S away). tests/services/test_pace.py tests the function.

NOON = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)


@pytest.fixture
def frozen(monkeypatch):
    """Freeze the pacing clock at 12:00:10 UTC; return a function that moves it to `seconds` past 12:00."""

    def _move(seconds: float) -> None:
        monkeypatch.setattr(pace, "clock", lambda: NOON + timedelta(seconds=seconds))

    _move(10)
    return _move


@pytest.fixture
def store(app):
    """Give the app a fakeredis, where the providers' pace keys are."""
    store = fakeredis.FakeRedis(decode_responses=True)
    app.extensions["redis"] = store
    return store


def _developer_panel(app, device_id, *, on=True):
    with app.app_context():
        db.session.get(DeviceConfig, device_id).developer_enabled = on
        db.session.commit()


def _retry_after(client, secret=SECRET):
    return _get(client, None, secret).headers["Retry-After"]


def _answers(client, device_id):
    """Return (status, Retry-After) of each way a screen polls: GET and POST, plain and with the current ETag."""
    plain_get = _get(client, device_id, SECRET)
    etag = plain_get.headers.get("ETag", '"none"')
    answers = [plain_get, _get(client, device_id, SECRET, **{"If-None-Match": etag})]
    answers += [_post(client, {"logs": []}), _post(client, {"logs": []}, **{"If-None-Match": etag})]
    return [(response.status_code, response.headers.get("Retry-After")) for response in answers]


@pytest.mark.usefixtures("frozen", "shipped")
def test_every_answer_says_to_sleep_until_just_after_the_next_half_minute(client, register_device):
    device_id, _ = register_device()
    assert _answers(client, device_id) == [(404, "23")] * 4  # before the first frame: the same function

    _put(client, device_id, _frame())

    assert _answers(client, device_id) == [(200, "23"), (304, "23"), (200, "23"), (304, "23")]


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [(0, "33"), (2.5, "31"), (3, "30"), (10, "23"), (28, "5"), (29, "34"), (33, "30"), (58, "5"), (59, "34")],
)
def test_the_sleep_is_worked_out_from_the_servers_clock(client, register_device, frozen, seconds, expected):
    device_id, _ = register_device()
    _put(client, device_id, _frame())
    frozen(seconds)

    assert _retry_after(client) == expected


@pytest.mark.usefixtures("frozen", "shipped")
def test_a_live_developer_pace_key_makes_every_answer_poll_every_10s(client, register_device, app, store):
    device_id, _ = register_device()
    _developer_panel(app, device_id)
    store.set(f"v1:pace:developer:{device_id}", "20", ex=60)
    assert _answers(client, device_id) == [(404, "13")] * 4

    _put(client, device_id, _frame())

    assert _answers(client, device_id) == [(200, "13"), (304, "13"), (200, "13"), (304, "13")]


@pytest.mark.usefixtures("frozen")
def test_the_pace_key_lapsing_brings_the_screen_back_to_the_half_minute(client, register_device, app, store):
    device_id, _ = register_device()
    _developer_panel(app, device_id)
    _put(client, device_id, _frame())
    store.set(f"v1:pace:developer:{device_id}", "20", ex=60)
    assert _retry_after(client) == "13"

    store.delete(f"v1:pace:developer:{device_id}")

    assert _retry_after(client) == "23"


@pytest.mark.usefixtures("frozen")
def test_the_pace_key_counts_only_while_the_screen_shows_the_developer_panel(client, register_device, app, store):
    device_id, _ = register_device()
    _put(client, device_id, _frame())
    store.set(f"v1:pace:developer:{device_id}", "20", ex=60)
    _developer_panel(app, device_id, on=False)

    assert _retry_after(client) == "23"


@pytest.mark.usefixtures("frozen")
def test_another_screens_pace_key_is_not_this_ones(client, register_device, app, store):
    device_id, _ = register_device()
    other_id, other_secret = register_device("another-very-long-device-secret")
    for screen in (device_id, other_id):
        _developer_panel(app, screen)
        _put(client, screen, _frame(), secret=SECRET if screen == device_id else other_secret)
    store.set(f"v1:pace:developer:{other_id}", "20", ex=60)

    assert (_retry_after(client), _retry_after(client, other_secret)) == ("23", "13")


@pytest.mark.parametrize("value", ["0", "soon", "-5"])
@pytest.mark.usefixtures("frozen")
def test_a_pace_key_that_is_not_seconds_is_ignored(client, register_device, app, store, value):
    device_id, _ = register_device()
    _developer_panel(app, device_id)
    _put(client, device_id, _frame())
    store.set(f"v1:pace:developer:{device_id}", value, ex=60)

    assert _retry_after(client) == "23"


@pytest.mark.usefixtures("frozen")
def test_a_pace_key_that_never_lapses_is_ignored(client, register_device, app, store):
    device_id, _ = register_device()
    _developer_panel(app, device_id)
    _put(client, device_id, _frame())
    store.set(f"v1:pace:developer:{device_id}", "20")

    assert _retry_after(client) == "23"


def test_a_poll_writes_nothing_to_redis(client, register_device, app, store, monkeypatch, frozen):
    device_id, _ = register_device()
    _developer_panel(app, device_id)
    _put(client, device_id, _frame())
    store.set(f"v1:pace:developer:{device_id}", "20", ex=60)
    sent = []
    execute = redis.client.Pipeline.execute
    monkeypatch.setattr(
        redis.client.Pipeline,
        "execute",
        lambda pipe, *args, **kwargs: (
            sent.extend(c[0][0] for c in pipe.command_stack) or execute(pipe, *args, **kwargs)
        ),
    )
    command = redis.Redis.execute_command
    monkeypatch.setattr(
        redis.Redis, "execute_command", lambda r, *args, **kwargs: sent.append(args[0]) or command(r, *args, **kwargs)
    )

    for _ in range(3):
        assert _retry_after(client) == "13"

    assert sorted(set(sent)) == ["GET", "PTTL"]  # reads, in one round trip a poll
    assert sent.count("GET") == 3


@pytest.mark.usefixtures("frozen")
def test_redis_down_is_logged_and_the_screen_keeps_its_usual_pace(client, register_device, app):
    device_id, _ = register_device()
    _developer_panel(app, device_id)
    _put(client, device_id, _frame())
    app.extensions["redis"] = redis.Redis(host="127.0.0.1", port=1, socket_connect_timeout=0.2)  # nothing listens

    with patch.object(pace, "logger") as logger:
        assert _retry_after(client) == "23"

    logger.warning.assert_called_once_with("Couldn't read the screen's pace hints from Redis (%s)", "ConnectionError")


# The legacy hint: today's renderers (pi-dashboard v1.26.0) send their pace with each frame, as X-Refresh-Hint. The
# broker keeps it on the frame's row for PACE_LEASE_S, and it's one more hint for the same function.


@pytest.mark.usefixtures("frozen")
def test_a_renderers_hint_makes_the_screen_poll_at_half_of_it(client, register_device):
    register_device()

    response = client.put("/api/frame", data=_frame(), headers={**_auth(), "X-Refresh-Hint": "20"})

    assert response.status_code == 204
    assert _retry_after(client) == "13"


@pytest.mark.usefixtures("frozen")
def test_a_conditional_get_carries_the_renderers_hint_too(client, register_device):
    device_id, _ = register_device()
    client.put("/api/frame", data=_frame(), headers={**_auth(), "X-Refresh-Hint": "30"})
    etag = _get(client, device_id, SECRET).headers["ETag"]

    response = _get(client, device_id, SECRET, **{"If-None-Match": etag})

    assert (response.status_code, response.headers["Retry-After"]) == (304, "8")  # 15 s polls: :18


@pytest.mark.parametrize(
    ("asked", "granted"),
    [("5", "13"), ("19", "13"), ("20", "13"), ("40", "13"), ("45", "16"), ("60", "23"), ("3600", "23")],
)
@pytest.mark.usefixtures("frozen")
def test_the_renderers_hint_is_floored_and_capped_by_the_function(client, register_device, asked, granted):
    register_device()

    client.put("/api/frame", data=_frame(), headers={**_auth(), "X-Refresh-Hint": asked})

    assert _retry_after(client) == granted


@pytest.mark.parametrize("asked", ["", "soon", "-5", "2.5", "0"])
@pytest.mark.usefixtures("frozen")
def test_junk_hints_are_no_request(client, register_device, asked):
    register_device()

    client.put("/api/frame", data=_frame(), headers={**_auth(), "X-Refresh-Hint": asked})

    assert _retry_after(client) == "23"


@pytest.mark.usefixtures("frozen")
def test_the_next_frame_without_a_hint_ends_the_faster_refresh(client, register_device):
    device_id, _ = register_device()
    client.put("/api/frame", data=_frame(), headers={**_auth(), "X-Refresh-Hint": "20"})

    _put(client, device_id, _frame())

    assert _retry_after(client) == "23"


def test_the_renderers_hint_lapses_after_the_lease_when_it_stops_asking(client, register_device, frozen):
    register_device()
    client.put("/api/frame", data=_frame(), headers={**_auth(), "X-Refresh-Hint": "20"})
    frozen(10 + PACE_LEASE_S - 1)
    assert _retry_after(client) == "14"  # 12:03:09: :13 is too close, so :23

    frozen(10 + PACE_LEASE_S)

    assert _retry_after(client) == "23"  # 12:03:10: back to :33


@pytest.mark.usefixtures("frozen")
def test_the_shortest_of_the_renderers_hint_and_the_pace_key_wins(client, register_device, app, store):
    device_id, _ = register_device()
    _developer_panel(app, device_id)
    client.put("/api/frame", data=_frame(), headers={**_auth(), "X-Refresh-Hint": "40"})
    store.set(f"v1:pace:developer:{device_id}", "20", ex=60)
    assert _retry_after(client) == "13"

    client.put("/api/frame", data=_frame(), headers={**_auth(), "X-Refresh-Hint": "20"})
    store.set(f"v1:pace:developer:{device_id}", "40", ex=60)

    assert _retry_after(client) == "13"
