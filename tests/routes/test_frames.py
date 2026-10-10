import re
from datetime import UTC, datetime, timedelta
from hashlib import blake2b

import pytest
from sqlalchemy import inspect, select

from broker.db import db
from broker.models import Device, Frame
from broker.routes.frames import FRAME_BYTES

SECRET = "a-very-long-device-secret-value"  # register_device's default


def _frame(fill: int = 0xAA) -> bytes:
    return bytes([fill]) * FRAME_BYTES


def _etag(frame: bytes) -> str:
    return blake2b(frame, digest_size=8).hexdigest()


def _url(device_id):
    return f"/api/frames/{device_id}/frame"


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


def test_put_rejects_another_devices_renderer_secret(app, client, register_device):
    device_a, _ = register_device()
    _, secret_b = register_device("another-very-long-device-secret")

    assert _put(client, device_a, _frame(), secret=secret_b).status_code == 401
    assert _stored_rows(app, device_a) == []


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


@pytest.mark.parametrize("method", ["post", "patch", "delete"])
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


def test_get_rejects_another_devices_secret(client, register_device):
    device_a, _ = register_device()
    _, secret_b = register_device("another-very-long-device-secret")
    _put(client, device_a, _frame())

    assert _get(client, device_a, secret_b).status_code == 401


def test_get_works_for_both_roles(client, split_device):
    device_id, renderer_secret, display_secret = split_device
    _put(client, device_id, _frame(), secret=renderer_secret)

    assert _get(client, device_id, display_secret).data == _frame()
    assert _get(client, device_id, renderer_secret).data == _frame()


def test_get_rejects_a_display_secret_on_another_devices_path(client, register_device, split_device):
    other_id, _ = register_device("another-very-long-device-secret")
    _, _, display_secret = split_device

    assert _get(client, other_id, display_secret).status_code == 401


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
    assert set(columns) == {"device_id", "frame", "etag", "rendered_at"}
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

LOGS_URL = "http://127.0.0.1:9880/logs"
METRICS_URL = "http://127.0.0.1:8428/write"
LINES = "esp,device_id={device_id} uptime_s=3600i,rssi=-60i"


class _Response:
    def __init__(self, status=204):
        self.status = status

    def raise_for_status(self):
        from broker.routes import frames

        if self.status >= 400:
            raise frames.requests.HTTPError(f"{self.status} error")


class _Inline:
    """Stands in for the executor, so forwarding has finished by the time the response is back."""

    def submit(self, fn, *args):
        fn(*args)


@pytest.fixture
def shipped(monkeypatch):
    """Runs forwarding inline and records each (url, kwargs) the broker posts."""
    from broker.routes import frames

    calls = []

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        return _Response()

    monkeypatch.setattr(frames, "_executor", _Inline())
    monkeypatch.setattr(frames.requests, "post", fake_post)
    return calls


def _post(client, body=None, secret=SECRET, **headers):
    return client.post("/api/frames/frame", json=body, headers={"Authorization": f"Bearer {secret}", **headers})


def _metrics_calls(shipped):
    return [kwargs for url, kwargs in shipped if url == METRICS_URL]


def _logs_calls(shipped):
    return [kwargs for url, kwargs in shipped if url == LOGS_URL]


def test_post_requires_auth(client, shipped):
    response = client.post("/api/frames/frame", json={"metrics": LINES, "logs": ["x"]})

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


def test_post_before_any_frame_is_404_with_retry_after_but_still_ships(client, register_device, shipped):
    register_device()

    response = _post(client, {"metrics": LINES, "logs": ["boot"]})

    assert response.status_code == 404
    assert response.headers["Retry-After"] == "5"
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


# --- POST: logs, to Fluent Bit ----------------------------------------------------------------------


def test_post_sends_logs_to_fluent_bit_tagged_with_the_device(client, register_device, shipped):
    device_id, _ = register_device()

    _post(client, {"logs": [{"log": "a", "level": "warn"}, "plain", 7]})

    assert shipped == [
        (
            LOGS_URL,
            {
                "json": [
                    {"log": "a", "level": "warn", "device_id": device_id},
                    {"log": "plain", "device_id": device_id},
                    {"log": "7", "device_id": device_id},
                ],
                "timeout": 2,
            },
        )
    ]


def test_post_overrides_a_device_id_the_screen_claims_in_a_log(client, register_device, shipped):
    device_id, _ = register_device()

    _post(client, {"logs": [{"log": "a", "device_id": "someone-else"}]})

    assert _logs_calls(shipped)[0]["json"] == [{"log": "a", "device_id": device_id}]


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

    response = client.post("/api/frames/frame", data=b"\xff\x00", headers=_auth(secret))

    assert response.status_code == 200
    assert shipped == []


def test_post_uses_destinations_from_the_environment(client, register_device, shipped, monkeypatch):
    monkeypatch.setenv("VICTORIA_METRICS_URL", "http://vm.example:8428/write")
    monkeypatch.setenv("FLUENT_BIT_LOGS_URL", "http://fb.example:9880/logs")
    register_device()

    _post(client, {"metrics": LINES, "logs": ["x"]})

    assert [url for url, _ in shipped] == ["http://vm.example:8428/write", "http://fb.example:9880/logs"]


# --- POST: a destination that's down or unhappy never touches the screen ----------------------------


@pytest.fixture
def failing(monkeypatch):
    """Makes every forward fail the given way; returns the list of attempted URLs."""
    from broker.routes import frames

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
    from broker.routes import frames

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
    from broker.routes import frames

    sent = []

    def fake_post(url, **kwargs):
        if url == METRICS_URL:
            raise frames.requests.ConnectionError("down")
        sent.append(url)
        return _Response()

    monkeypatch.setattr(frames, "_executor", _Inline())
    monkeypatch.setattr(frames.requests, "post", fake_post)
    register_device()

    _post(client, {"metrics": LINES, "logs": ["x"]})

    assert sent == [LOGS_URL]


def test_post_does_not_wait_for_a_slow_destination(client, register_device, monkeypatch):
    """The real executor hands the request to a worker thread; the response doesn't depend on it."""
    import threading

    from broker.routes import frames

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
