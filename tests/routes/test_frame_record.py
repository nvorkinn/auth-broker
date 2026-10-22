"""The screen served from the render pool's frame record in Redis (services/frame_record.py), through the real app.

The record is written as pi-dashboard's render pool does (the shape the store contract's frame_record.json pins, which
pi-dashboard's tests read too), and the screen polls POST /api/frame with If-None-Match as the ESP32 does
(countdown-esp32's frame_client.cpp keeps the ETag header as sent, quotes and all, and draws only a 200).
"""

from datetime import UTC, datetime, timedelta
from hashlib import blake2b
from unittest.mock import MagicMock

import fakeredis
import pytest
import redis
from countdown_api import fixtures

from broker import create_app
from broker.db import db
from broker.models import Device
from broker.services import frame_image, frame_record, pace

FIXTURE = fixtures.load("frame_record.json")
FIXTURE_FRAME = fixtures.read_bytes(FIXTURE["fields"]["frame"]["file"])

SECRET = "a-very-long-device-secret-value"  # register_device's default
LONG_AGO = datetime(2026, 1, 1, tzinfo=UTC)
LATER = datetime(2099, 1, 1, tzinfo=UTC)
# The pacing clock is frozen at 12:00:10 UTC, so every answer says to sleep until 12:00:33 (services/pace.py), but for
# a 404's: the pool's next render lands within the minute, so it asks again sooner (routes/frames.NO_FRAME_RETRY_S).
RETRY_AFTER = "23"
NO_FRAME_RETRY_AFTER = "10"


@pytest.fixture(autouse=True)
def frozen_pace(monkeypatch):
    monkeypatch.setattr(pace, "clock", lambda: datetime(2026, 10, 18, 12, 0, 10, tzinfo=UTC))


@pytest.fixture
def frames_redis(app) -> fakeredis.FakeRedis:
    """Return the Redis the app reads the frames from (as bytes), empty until a test writes a record."""
    return app.extensions["redis_bytes"]


@pytest.fixture
def logged(monkeypatch) -> MagicMock:
    """Replace the record's logger, to see what it logs."""
    logger = MagicMock()
    monkeypatch.setattr(frame_record, "logger", logger)
    return logger


def _frame(fill: int) -> bytes:
    return bytes([fill]) * frame_record.FRAME_BYTES


def _etag(frame: bytes) -> str:
    return blake2b(frame, digest_size=8).hexdigest()


def _write(store, device_id: str, picture: bytes, at: datetime, **fields) -> None:
    """Write the screen's record as pi-dashboard's render pool does (store.frame_record), but for `fields`.

    A field given as None is left out.
    """
    values = {"frame": picture, "etag": _etag(picture), "rendered_at": at.isoformat(timespec="milliseconds")}
    values.update(fields)
    store.delete(frame_record.key(device_id))
    store.hset(frame_record.key(device_id), mapping={k: v for k, v in values.items() if v is not None})
    store.expire(frame_record.key(device_id), FIXTURE["ttl_s"])


def _bare_poll(client, secret=SECRET, **headers):
    return client.post("/api/frame", headers={"Authorization": f"Bearer {secret}", **headers})


def _poll(client, secret=SECRET, **headers):
    """Poll as the ESP32 does: POST, with its metrics and logs, and If-None-Match once it has drawn a frame."""
    body = {"role": "display", "metrics": "", "logs": []}
    return client.post("/api/frame", json=body, headers={"Authorization": f"Bearer {secret}", **headers})


def _quoted(frame: bytes) -> str:
    return f'"{_etag(frame)}"'


# --- the shape, pinned by the fixture both repositories read ------------------------------------------------------


def test_the_key_and_the_frame_are_the_fixtures():
    assert frame_record.key(FIXTURE["device_id"]) == FIXTURE["key"]
    assert len(FIXTURE_FRAME) == FIXTURE["fields"]["frame"]["length"] == frame_record.FRAME_BYTES == 48_000
    assert _etag(FIXTURE_FRAME) == FIXTURE["fields"]["etag"]


@pytest.mark.parametrize("poll", [_bare_poll, _poll], ids=["bare", "with a report"])
def test_the_fixtures_record_is_served_as_it_is(client, register_device, frames_redis, poll):
    device_id, _ = register_device()
    frames_redis.hset(
        frame_record.key(device_id),
        mapping={
            "frame": FIXTURE_FRAME,
            "etag": FIXTURE["fields"]["etag"],
            "rendered_at": FIXTURE["fields"]["rendered_at"],
        },
    )

    response = poll(client)

    assert response.status_code == 200
    assert response.data == FIXTURE_FRAME
    assert response.mimetype == "application/octet-stream"
    assert response.headers["ETag"] == FIXTURE["served_etag"]
    assert response.headers["Retry-After"] == RETRY_AFTER
    again = poll(client, **{"If-None-Match": response.headers["ETag"]})
    assert again.status_code == 304
    assert again.data == b""
    assert again.headers["ETag"] == FIXTURE["served_etag"]
    assert again.headers["Retry-After"] == RETRY_AFTER


@pytest.mark.usefixtures("app")  # for its environment
def test_the_app_reads_the_frames_with_a_client_that_doesnt_decode():
    # create_app's own client, not the one the tests swap in: a decoding one couldn't read a frame.
    client = create_app().extensions["redis_bytes"]

    assert isinstance(client, redis.Redis)
    assert not client.connection_pool.connection_kwargs.get("decode_responses", False)


# --- serving the record ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("poll", [_bare_poll, _poll], ids=["bare", "with a report"])
def test_a_new_record_replaces_the_last(client, register_device, frames_redis, poll):
    device_id, _ = register_device()
    _write(frames_redis, device_id, _frame(0x11), LONG_AGO)
    _write(frames_redis, device_id, _frame(0x22), LATER)

    response = poll(client, **{"If-None-Match": _quoted(_frame(0x11))})

    assert response.status_code == 200
    assert response.data == _frame(0x22)
    assert response.headers["ETag"] == _quoted(_frame(0x22))


def test_a_record_is_served_with_the_usual_interval(client, register_device, frames_redis):
    device_id, _ = register_device()
    _write(frames_redis, device_id, _frame(0x22), LONG_AGO)

    response = _poll(client)

    assert response.status_code == 200
    assert response.data == _frame(0x22)
    assert response.headers["Retry-After"] == RETRY_AFTER


def test_a_new_picture_reaches_the_screen_once_and_then_it_gets_304s(client, register_device, frames_redis):
    device_id, _ = register_device()
    _write(frames_redis, device_id, _frame(0x11), LONG_AGO)
    etag = _poll(client).headers["ETag"]
    _write(frames_redis, device_id, _frame(0x22), LATER)

    drawn = _poll(client, **{"If-None-Match": etag})
    again = _poll(client, **{"If-None-Match": drawn.headers["ETag"]})

    assert (drawn.status_code, drawn.data) == (200, _frame(0x22))
    assert (again.status_code, again.data) == (304, b"")


def test_a_304_from_the_record_never_reads_its_frame(client, register_device, frames_redis, monkeypatch):
    device_id, _ = register_device()
    _write(frames_redis, device_id, _frame(0x22), LATER)
    asked: list[list[str]] = []
    hmget = frames_redis.hmget

    def recording_hmget(name, fields):
        asked.append(list(fields))
        return hmget(name, fields)

    monkeypatch.setattr(frames_redis, "hmget", recording_hmget)

    assert _poll(client, **{"If-None-Match": _quoted(_frame(0x22))}).status_code == 304
    assert _bare_poll(client, **{"If-None-Match": _quoted(_frame(0x22))}).status_code == 304
    assert asked == [["etag", "rendered_at"], ["etag", "rendered_at"]]

    asked.clear()
    assert _poll(client).status_code == 200
    assert asked == [["etag", "rendered_at"], ["frame"]]


@pytest.mark.parametrize("poll", [_bare_poll, _poll], ids=["bare", "with a report"])
def test_a_wildcard_if_none_match_is_a_304_from_the_record(client, register_device, frames_redis, poll):
    device_id, _ = register_device()
    _write(frames_redis, device_id, _frame(0x22), LATER)

    assert poll(client, **{"If-None-Match": "*"}).status_code == 304


def test_each_screen_is_served_its_own_record(client, register_device, frames_redis):
    device_a, secret_a = register_device()
    _, secret_b = register_device("another-very-long-device-secret")
    _write(frames_redis, device_a, _frame(0x22), LATER)

    assert _poll(client, secret_a).data == _frame(0x22)
    assert _poll(client, secret_b).status_code == 404


# --- a record that can't be served ----------------------------------------------------------------------------------

CORRUPT = {
    "no etag": {"etag": None},
    "no time": {"rendered_at": None},
    "no frame": {"frame": None},
    "an etag that isn't hex": {"etag": "not-a-blake2b-ok"},
    "an etag that isn't text": {"etag": b"\xff" * 16},
    "a time with no offset": {"rendered_at": "2099-01-01T00:00:00"},
    "a time that isn't one": {"rendered_at": "yesterday"},
    "a short frame": {"frame": b"\x00" * 100},
    "a frame of another picture": {"frame": b"\x33" * 48_000},
}


@pytest.mark.parametrize("fields", CORRUPT.values(), ids=CORRUPT.keys())
@pytest.mark.parametrize("poll", [_bare_poll, _poll], ids=["bare", "with a report"])
def test_a_record_that_cant_be_read_is_no_frame_yet(client, register_device, frames_redis, fields, poll):
    device_id, _ = register_device()
    _write(frames_redis, device_id, _frame(0x22), LATER, **fields)

    response = poll(client)

    assert response.status_code == 404
    assert response.get_json() == {"error": "no frame yet"}
    assert response.headers["Retry-After"] == NO_FRAME_RETRY_AFTER


def test_a_record_replaced_while_it_was_read_gives_the_new_one(client, register_device, frames_redis, monkeypatch):
    # The pool wrote a new picture between the broker reading the record's etag and its frame.
    device_id, _ = register_device()
    _write(frames_redis, device_id, _frame(0x22), LATER)
    head = frame_record.head

    def head_then_replaced(device):
        found = head(device)
        _write(frames_redis, device, _frame(0x33), LATER + timedelta(minutes=1))
        return found

    monkeypatch.setattr(frame_record, "head", head_then_replaced)

    response = _poll(client)

    assert response.status_code == 200
    assert response.data == _frame(0x33)
    assert response.headers["ETag"] == _quoted(_frame(0x33))


def test_a_record_deleted_while_it_was_read_is_no_frame_yet(client, register_device, frames_redis, monkeypatch):
    device_id, _ = register_device()
    _write(frames_redis, device_id, _frame(0x22), LATER)
    head = frame_record.head

    def head_then_deleted(device):
        found = head(device)
        frames_redis.delete(frame_record.key(device))
        return found

    monkeypatch.setattr(frame_record, "head", head_then_deleted)

    response = _poll(client)

    assert (response.status_code, response.headers["Retry-After"]) == (404, NO_FRAME_RETRY_AFTER)


def test_an_expired_record_is_no_frame_yet(client, register_device, frames_redis):
    device_id, _ = register_device()
    _write(frames_redis, device_id, _frame(0x22), LATER)
    frames_redis.delete(frame_record.key(device_id))  # lapsed, or evicted

    response = _poll(client)

    assert (response.status_code, response.headers["Retry-After"]) == (404, NO_FRAME_RETRY_AFTER)


@pytest.mark.parametrize("poll", [_bare_poll, _poll], ids=["bare", "with a report"])
def test_redis_down_is_no_frame_yet_and_logs_only_why(app, client, register_device, logged, poll):
    register_device()
    server = fakeredis.FakeServer()
    server.connected = False  # every command raises redis.ConnectionError
    app.extensions["redis_bytes"] = fakeredis.FakeRedis(server=server)

    response = poll(client)

    # The screen keeps the picture it has, and asks again soon.
    assert (response.status_code, response.headers["Retry-After"]) == (404, NO_FRAME_RETRY_AFTER)
    assert logged.warning.call_args.args == ("Couldn't read a screen's frame from Redis (%s)", "ConnectionError")


# --- the config page shows what the screen is served ----------------------------------------------------------------


def test_the_screen_page_says_when_the_records_picture_was_drawn(paired_client, frames_redis):
    client, device_id, _secret = paired_client
    drawn = datetime(2099, 1, 1, 12, 34, 56, tzinfo=UTC)
    _write(frames_redis, device_id, _frame(0x22), drawn)

    html = client.get(f"/device/{device_id}/screen").get_data(as_text=True)

    assert 'datetime="2099-01-01T12:34:56+00:00"' in html
    assert "12:34:56 UTC" in html


def test_the_screen_page_shows_the_record(paired_client, frames_redis):
    client, device_id, _ = paired_client
    _write(frames_redis, device_id, _frame(0x22), LONG_AGO)

    html = client.get(f"/device/{device_id}/screen").get_data(as_text=True)

    assert f'src="/device/{device_id}/screen/frame.png"' in html  # the picture, not "Nothing drawn yet"
    assert client.get(f"/device/{device_id}/screen/frame.png").status_code == 200


def test_the_preview_is_the_records_picture(app, paired_client, frames_redis):
    client, device_id, _secret = paired_client
    _write(frames_redis, device_id, _frame(0xFF), LATER)  # all black

    response = client.get(f"/device/{device_id}/screen/frame.png")

    assert response.status_code == 200
    assert response.data == frame_image.to_png(_frame(0xFF), upright=True)
    assert response.headers["X-Rendered-At"] == LATER.isoformat(timespec="seconds")
    assert response.headers["ETag"] == f'"{_etag(_frame(0xFF))}-v"'
    again = client.get(f"/device/{device_id}/screen/frame.png", headers={"If-None-Match": response.headers["ETag"]})
    assert again.status_code == 304


def test_a_record_that_cant_be_read_is_no_preview(paired_client, frames_redis):
    client, device_id, _secret = paired_client
    _write(frames_redis, device_id, _frame(0xFF), LATER, frame=b"\x00" * 100)

    assert client.get(f"/device/{device_id}/screen/frame.png").status_code == 404
    assert "Nothing drawn yet" in client.get(f"/device/{device_id}/screen").get_data(as_text=True)


# --- forgetting a device forgets its record -------------------------------------------------------------------------


def test_forgetting_a_device_deletes_its_record(app, client, register_device, frames_redis, cli):
    device_id, _ = register_device()
    other_id, _ = register_device("another-very-long-device-secret")
    _write(frames_redis, device_id, _frame(0x22), LATER)
    _write(frames_redis, other_id, _frame(0x22), LATER)

    assert cli("forget", device_id).exit_code == 0

    assert not frames_redis.exists(frame_record.key(device_id))
    assert frames_redis.exists(frame_record.key(other_id))
    with app.app_context():
        assert db.session.get(Device, device_id) is None


def test_unpairing_with_the_config_deletes_the_record(client, register_device, frames_redis, cli):
    device_id, _ = register_device()
    _write(frames_redis, device_id, _frame(0x22), LATER)

    assert cli("unpair", device_id, "--config").exit_code == 0

    assert not frames_redis.exists(frame_record.key(device_id))
    assert _poll(client).status_code == 404  # nothing of the last owner's left to serve


def test_unpairing_alone_keeps_the_record(client, register_device, frames_redis, cli):
    device_id, _ = register_device()
    _write(frames_redis, device_id, _frame(0x22), LATER)

    assert cli("unpair", device_id).exit_code == 0

    assert frames_redis.exists(frame_record.key(device_id))


def test_forgetting_a_device_while_redis_is_down_still_forgets_it(app, register_device, cli, logged):
    device_id, _ = register_device()
    server = fakeredis.FakeServer()
    server.connected = False
    app.extensions["redis_bytes"] = fakeredis.FakeRedis(server=server)

    assert cli("forget", device_id).exit_code == 0

    with app.app_context():
        assert db.session.get(Device, device_id) is None
    assert logged.warning.call_args.args == ("Couldn't delete a screen's frame from Redis (%s)", "ConnectionError")


# --- frame_record.head on its own ------------------------------------------------------------------------------------


def test_a_time_in_another_zone_is_read_as_utc(app, frames_redis):
    _write(frames_redis, "d", _frame(0x22), LATER, rendered_at="2099-01-01T01:00:00.000+01:00")

    with app.app_context():
        assert frame_record.head("d") == frame_record.StoredFrame(_etag(_frame(0x22)), LATER)
        head = frame_record.head("d")
        assert head is not None
        assert head.rendered_at.tzinfo == UTC
