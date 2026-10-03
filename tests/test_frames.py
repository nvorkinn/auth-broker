import re
from hashlib import blake2b
from unittest.mock import patch

import pytest

from broker import admin
from broker.db import get_db
from broker.frames import FRAME_BYTES

RENDERER_HEADERS = {"Authorization": "Bearer test-renderer-token"}


def _frame(fill: int = 0xAA) -> bytes:
    return bytes([fill]) * FRAME_BYTES


def _etag(frame: bytes) -> str:
    return blake2b(frame, digest_size=8).hexdigest()


def _url(device_id):
    return f"/api/frames/{device_id}/frame"


def _put(client, device_id, frame, headers=RENDERER_HEADERS):
    return client.put(_url(device_id), data=frame, headers=headers)


def _get(client, device_id, secret, **headers):
    return client.get(_url(device_id), headers={"Authorization": f"Bearer {secret}", **headers})


def _stored_rows(app, device_id):
    with app.app_context():
        return get_db().execute("SELECT * FROM frames WHERE device_id = ?", (device_id,)).fetchall()


def test_frame_size_is_one_bit_packed_800x480():
    assert FRAME_BYTES == 48_000


# --- PUT: renderer auth ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "test-renderer-token"},
        {"Authorization": "Basic test-renderer-token"},
        {"Authorization": "bearer test-renderer-token"},
        {"Authorization": "Bearer "},
        {"Authorization": "Bearer wrong-token"},
        {"Authorization": "Bearer test-renderer-token-extra"},
        {"Authorization": "Bearer test-renderer-toke"},
    ],
    ids=["missing", "no-scheme", "basic", "lowercase-scheme", "empty", "wrong", "longer", "prefix"],
)
def test_put_rejects_bad_renderer_auth(app, client, register_device, headers):
    device_id, _ = register_device()

    response = _put(client, device_id, _frame(), headers=headers)

    assert response.status_code == 401
    assert response.get_json() == {"error": "unauthorized"}
    assert _stored_rows(app, device_id) == []


def test_put_rejects_the_devices_own_secret(app, client, register_device):
    device_id, secret = register_device()

    response = _put(client, device_id, _frame(), headers={"Authorization": f"Bearer {secret}"})

    assert response.status_code == 401
    assert _stored_rows(app, device_id) == []


def test_put_fails_closed_when_renderer_token_is_not_configured(app, client, register_device, monkeypatch):
    device_id, _ = register_device()
    monkeypatch.delenv("COUNTDOWN_RENDERER_TOKEN")
    app.config["PROPAGATE_EXCEPTIONS"] = False

    response = _put(client, device_id, _frame())

    assert response.status_code == 500
    assert _stored_rows(app, device_id) == []


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
    assert row["frame"] == frame
    assert row["etag"] == _etag(frame)
    assert re.fullmatch(r"[0-9a-f]{16}", row["etag"])
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", row["rendered_at"])


def test_put_ignores_request_content_type(app, client, register_device):
    device_id, _ = register_device()
    frame = _frame()

    response = client.put(_url(device_id), data=frame, headers={**RENDERER_HEADERS, "Content-Type": "application/json"})

    assert response.status_code == 204
    assert _stored_rows(app, device_id)[0]["frame"] == frame


def test_put_preserves_arbitrary_bytes(app, client, register_device):
    device_id, _ = register_device()
    frame = bytes(i % 256 for i in range(FRAME_BYTES))

    assert _put(client, device_id, frame).status_code == 204

    assert _stored_rows(app, device_id)[0]["frame"] == frame


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

    assert _stored_rows(app, device_id)[0]["frame"] == frame


def test_put_replaces_existing_frame(app, client, register_device):
    device_id, _ = register_device()
    with patch("broker.frames.now_iso", return_value="2026-01-01T00:00:00Z"):
        _put(client, device_id, _frame(0x00))
    with patch("broker.frames.now_iso", return_value="2026-01-01T00:05:00Z"):
        response = _put(client, device_id, _frame(0xFF))

    assert response.status_code == 204
    [row] = _stored_rows(app, device_id)
    assert row["frame"] == _frame(0xFF)
    assert row["etag"] == _etag(_frame(0xFF))
    assert row["rendered_at"] == "2026-01-01T00:05:00Z"


def test_put_same_frame_twice_keeps_the_same_etag(app, client, register_device):
    device_id, _ = register_device()
    _put(client, device_id, _frame())
    first = _stored_rows(app, device_id)[0]["etag"]

    _put(client, device_id, _frame())

    assert _stored_rows(app, device_id)[0]["etag"] == first


def test_different_frames_get_different_etags(app, client, register_device):
    device_id, _ = register_device()
    _put(client, device_id, _frame(0x00))
    first = _stored_rows(app, device_id)[0]["etag"]

    _put(client, device_id, _frame(0x01))

    assert _stored_rows(app, device_id)[0]["etag"] != first


def test_put_only_touches_the_target_device(app, client, register_device):
    device_a, _ = register_device()
    device_b, _ = register_device("another-very-long-device-secret")
    _put(client, device_a, _frame(0x11))
    _put(client, device_b, _frame(0x22))

    _put(client, device_a, _frame(0x33))

    assert _stored_rows(app, device_a)[0]["frame"] == _frame(0x33)
    assert _stored_rows(app, device_b)[0]["frame"] == _frame(0x22)


def test_put_for_unknown_device_returns_404_and_stores_nothing(app, client):
    response = _put(client, "does-not-exist", _frame())

    assert response.status_code == 404
    assert _stored_rows(app, "does-not-exist") == []


def test_put_for_forgotten_device_returns_404(app, client, register_device):
    device_id, _ = register_device()
    admin.main(["forget", device_id])

    assert _put(client, device_id, _frame()).status_code == 404
    assert _stored_rows(app, device_id) == []


@pytest.mark.parametrize("method", ["post", "patch", "delete"])
def test_frame_endpoint_rejects_other_methods(client, register_device, method):
    device_id, _ = register_device()
    response = getattr(client, method)(_url(device_id), headers=RENDERER_HEADERS)
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


def test_get_rejects_the_renderer_token(client, register_device):
    device_id, _ = register_device()
    _put(client, device_id, _frame())

    assert _get(client, device_id, "test-renderer-token").status_code == 401


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
        columns = {row["name"]: row for row in get_db().execute("PRAGMA table_info(frames)")}
    assert set(columns) == {"device_id", "frame", "etag", "rendered_at"}
    assert columns["device_id"]["pk"] == 1
    assert columns["frame"]["type"] == "BLOB"
    assert all(columns[name]["notnull"] for name in ("frame", "etag", "rendered_at"))


def test_admin_forget_deletes_the_devices_frame(app, client, register_device):
    device_id, _ = register_device()
    other_id, _ = register_device("another-very-long-device-secret")
    _put(client, device_id, _frame())
    _put(client, other_id, _frame())

    assert admin.main(["forget", device_id]) == 0

    assert _stored_rows(app, device_id) == []
    assert len(_stored_rows(app, other_id)) == 1
    with app.app_context():
        assert get_db().execute("SELECT 1 FROM devices WHERE device_id = ?", (device_id,)).fetchone() is None


def test_admin_unpair_keeps_the_devices_frame(app, client, register_device):
    device_id, secret = register_device()
    _put(client, device_id, _frame())

    assert admin.main(["unpair", device_id]) == 0

    assert _stored_rows(app, device_id)[0]["frame"] == _frame()
    assert _get(client, device_id, secret).data == _frame()


def test_admin_unpair_with_config_deletes_the_devices_frame(app, client, register_device):
    device_id, secret = register_device()
    other_id, _ = register_device("another-very-long-device-secret")
    _put(client, device_id, _frame())
    _put(client, other_id, _frame())

    assert admin.main(["unpair", device_id, "--config"]) == 0

    assert _stored_rows(app, device_id) == []
    assert _get(client, device_id, secret).status_code == 404
    assert len(_stored_rows(app, other_id)) == 1


def test_renderer_can_upload_again_after_unpair_with_config(client, register_device):
    device_id, secret = register_device()
    _put(client, device_id, _frame(0x00))
    admin.main(["unpair", device_id, "--config"])

    assert _put(client, device_id, _frame(0xFF)).status_code == 204
    assert _get(client, device_id, secret).data == _frame(0xFF)
