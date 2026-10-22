"""Test a screen registering (POST /api/devices/register), and its polls once registered."""

import threading
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import fakeredis
import pytest
from redis import RedisError
from sqlalchemy import select

from broker.auth import hash_secret
from broker.db import db
from broker.models import Device, DeviceConfig
from broker.services import device_keys, pace, pairing, pairing_key, registration, render_screens
from broker.services.frame_record import FRAME_BYTES
from broker.services.pair_throttle import MAX_REGISTRATION_LOCKOUT_SECONDS, REGISTRATIONS_PER_MINUTE, PairThrottle

USER = "user-1"
DISPLAY = "display-secret-0123456789"


def _auth(secret):
    return {"Authorization": f"Bearer {secret}"}


def _device(app, device_id):
    with app.app_context():
        return db.session.get(Device, device_id)


def _register(client, secret):
    return client.post("/api/devices/register", json={"secret": secret, "width": 480, "height": 800})


def test_register_rejects_a_non_string_secret(client):
    assert client.post("/api/devices/register", json={"secret": ["x"] * 20}).status_code == 400


def test_register_ignores_a_role(client):
    response = client.post(
        "/api/devices/register", json={"role": "renderer", "secret": DISPLAY, "width": 480, "height": 800}
    )

    assert response.status_code == 201


# --- screens --------------------------------------------------------------------------------------


def test_a_screen_becomes_a_device_at_once(app, client):
    response = _register(client, DISPLAY)

    assert response.status_code == 201
    device = _device(app, response.get_json()["device_id"])
    assert device.display_secret_hash == hash_secret(DISPLAY)
    with app.app_context():
        assert db.session.get(DeviceConfig, device.device_id) is not None


def test_registering_a_screen_again_returns_its_device(client):
    device_id = _register(client, DISPLAY).get_json()["device_id"]

    response = _register(client, DISPLAY)

    assert (response.status_code, response.get_json()) == (200, {"device_id": device_id})


def test_screens_nobody_has_paired_are_capped_and_expire(app, client, monkeypatch):
    monkeypatch.setattr(registration, "MAX_UNPAIRED_SCREENS", 2)
    first = _register(client, "display-one-0123456789").get_json()["device_id"]
    _register(client, "display-two-0123456789")

    full = _register(client, "display-three-0123456789")

    assert full.status_code == 503
    assert full.headers["Retry-After"] == "60"
    with app.app_context():
        db.session.get_one(Device, first).created_at = (
            datetime.now(UTC) - registration.UNPAIRED_TTL - timedelta(seconds=1)
        )
        db.session.commit()
    # The stale one is forgotten to make room, and a screen's next call is a 401 so it registers again.
    assert _register(client, "display-three-0123456789").status_code == 201
    assert _device(app, first) is None


def test_a_dropped_stale_screen_leaves_nothing_in_the_store(app, client, monkeypatch):
    store = fakeredis.FakeRedis(decode_responses=True)
    app.extensions["redis"] = store
    monkeypatch.setattr(registration, "MAX_UNPAIRED_SCREENS", 2)
    stale = _register(client, "display-one-0123456789").get_json()["device_id"]
    kept = _register(client, "display-two-0123456789").get_json()["device_id"]
    own = [pairing_key.key(stale), *device_keys.energy(stale), *device_keys.music(stale), *device_keys.developer(stale)]
    for key in own:
        store.set(key, "{}", ex=3600)
    store.set(pairing_key.key(kept), "{}", ex=3600)
    assert store.hexists(render_screens.SCREENS, stale)
    with app.app_context():
        db.session.get_one(Device, stale).created_at = (
            datetime.now(UTC) - registration.UNPAIRED_TTL - timedelta(seconds=1)
        )
        db.session.commit()

    assert _register(client, "display-three-0123456789").status_code == 201

    assert not any(store.exists(key) for key in own)
    assert not store.hexists(render_screens.SCREENS, stale)
    assert store.exists(pairing_key.key(kept))
    assert store.hexists(render_screens.SCREENS, kept)


def test_a_screen_is_dropped_from_the_store_when_the_cap_still_refuses_a_new_one(app, client, monkeypatch):
    store = fakeredis.FakeRedis(decode_responses=True)
    app.extensions["redis"] = store
    monkeypatch.setattr(registration, "MAX_UNPAIRED_SCREENS", 1)
    stale = _register(client, "display-one-0123456789").get_json()["device_id"]
    with app.app_context():
        db.session.get_one(Device, stale).created_at = (
            datetime.now(UTC) - registration.UNPAIRED_TTL - timedelta(seconds=1)
        )
        db.session.commit()
    store.set(pairing_key.key(stale), "{}", ex=3600)

    with patch.object(registration, "_unpaired_screens", return_value=1):
        assert _register(client, "display-two-0123456789").status_code == 503

    assert _device(app, stale) is None
    assert not store.exists(pairing_key.key(stale))


def test_a_redis_failure_never_blocks_dropping_a_stale_screen(app, client, monkeypatch):
    monkeypatch.setattr(registration, "MAX_UNPAIRED_SCREENS", 1)
    stale = _register(client, "display-one-0123456789").get_json()["device_id"]
    with app.app_context():
        db.session.get_one(Device, stale).created_at = (
            datetime.now(UTC) - registration.UNPAIRED_TTL - timedelta(seconds=1)
        )
        db.session.commit()
    broken = fakeredis.FakeRedis(decode_responses=True)
    app.extensions["redis"] = broken
    with patch.object(broken, "pipeline", side_effect=RedisError("secret command text")):
        assert _register(client, "display-two-0123456789").status_code == 201

    assert _device(app, stale) is None


def test_a_paired_screen_never_expires(app, client):
    device_id = _register(client, DISPLAY).get_json()["device_id"]
    with app.app_context():
        device = db.session.get_one(Device, device_id)
        device.owner, device.paired_at = USER, datetime.now(UTC)
        device.created_at = datetime.now(UTC) - registration.UNPAIRED_TTL * 10
        db.session.commit()

    _register(client, "another-display-0123456789")

    assert _device(app, device_id) is not None


def test_concurrent_registrations_never_share_a_secret(app):
    """Threads register screens at once against the real database file, some with the same secret."""
    count = 8
    errors = []

    def register(secret):
        try:
            response = app.test_client().post(
                "/api/devices/register", json={"secret": secret, "width": 480, "height": 800}
            )
            assert response.status_code in (200, 201), response.status_code
        except Exception as e:  # noqa: BLE001 -- collected and reported below
            errors.append(e)

    threads = [
        threading.Thread(target=register, args=(f"display-concurrent-secret-{i % count:02d}",))
        for i in range(count * 2)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    with app.app_context():
        devices = db.session.scalars(select(Device)).all()
        assert len(devices) == count
        assert len({d.display_secret_hash for d in devices}) == count


def test_a_simultaneous_repeat_of_the_same_secret_is_answered_as_a_repeat(client, monkeypatch):
    """Check two requests that both pass the "already registered?" check before either writes.

    The second one's insert then hits the unique secret column.
    """
    real_device_with = registration._device_with
    calls = []

    def device_with_that_misses_once(secret_hash):
        calls.append(secret_hash)
        return None if len(calls) == 1 else real_device_with(secret_hash)

    device_id = _register(client, DISPLAY).get_json()["device_id"]
    monkeypatch.setattr(registration, "_device_with", device_with_that_misses_once)

    response = _register(client, DISPLAY)

    assert (response.status_code, response.get_json()) == (200, {"device_id": device_id})


@pytest.fixture
def register_throttle(app):
    """Put back the real /register limit the app fixture lifts."""
    app.extensions["register_throttle"] = PairThrottle(
        max_events=REGISTRATIONS_PER_MINUTE, max_lockout=MAX_REGISTRATION_LOCKOUT_SECONDS
    )


@pytest.mark.usefixtures("register_throttle")
def test_register_is_throttled_per_ip_counting_every_call(client):
    for _ in range(REGISTRATIONS_PER_MINUTE):
        # The same secret each time: repeats are no-ops but still count.
        assert _register(client, DISPLAY).status_code in (200, 201)

    response = _register(client, DISPLAY)

    assert response.status_code == 429
    assert int(response.headers["Retry-After"]) > 0


@pytest.mark.usefixtures("register_throttle")
def test_polling_is_not_throttled(client, register_device):
    _, secret = register_device()
    for _ in range(REGISTRATIONS_PER_MINUTE * 2):
        assert client.post("/api/frame", headers=_auth(secret)).status_code == 404  # authenticated: no frame yet


# --- polling: the secret says which screen --------------------------------------------------------


def test_a_screen_polls_frames_without_knowing_its_device_id(client, register_device, draw):
    device_id, secret = register_device()
    assert client.post("/api/frame", headers=_auth(secret)).status_code == 404

    frame = bytes([0x5A]) * FRAME_BYTES
    draw(device_id, frame)

    response = client.post("/api/frame", headers=_auth(secret))
    assert response.status_code == 200
    assert response.data == frame
    again = client.post("/api/frame", headers={**_auth(secret), "If-None-Match": response.headers["ETag"]})
    assert again.status_code == 304


def test_id_less_endpoints_reject_unknown_and_missing_secrets(client, register_device):
    register_device()
    assert client.post("/api/frame").status_code == 401
    assert client.post("/api/frame", headers=_auth("not-a-registered-secret")).status_code == 401
    assert client.post("/api/frame", headers={"Authorization": "Bearer "}).status_code == 401


def test_id_less_requests_are_logged_with_the_device_id(app, client, register_device, caplog):
    device_id, secret = register_device()
    with app.app_context():
        assignment = db.session.get_one(Device, device_id).assignment
        assert assignment is not None
        slug = assignment.slug

    with caplog.at_level("INFO"):
        client.post("/api/frame", headers=_auth(secret))

    assert f"POST /api/frame device={slug} ({device_id}) role=display" in caplog.messages


# --- Retry-After: how often to poll (services/pace.py; tests/routes/test_frames.py has the rest) ---------------------


@pytest.fixture
def frozen(monkeypatch):
    monkeypatch.setattr(pace, "clock", lambda: datetime(2026, 10, 18, 12, 0, 10, tzinfo=UTC))


@pytest.mark.usefixtures("frozen")
def test_no_frame_yet_is_a_json_404_asking_again_soon(client, register_device):
    _, secret = register_device()

    response = client.post("/api/frame", headers=_auth(secret))

    assert response.status_code == 404
    assert response.get_json() == {"error": "no frame yet"}
    # At most NO_FRAME_RETRY_S, not until 12:00:33.
    assert response.headers["Retry-After"] == "10"


@pytest.mark.usefixtures("frozen")
def test_a_frame_says_to_poll_again_just_after_the_next_half_minute(app, client, register_device, draw):
    device_id, display_secret = register_device()
    draw(device_id, bytes(FRAME_BYTES))

    first = client.post("/api/frame", headers=_auth(display_secret))
    unchanged = client.post("/api/frame", headers={**_auth(display_secret), "If-None-Match": first.headers["ETag"]})

    assert (first.status_code, first.headers["Retry-After"]) == (200, "23")
    assert (unchanged.status_code, unchanged.headers["Retry-After"]) == (304, "23")


def test_a_new_screen_gets_the_weather_location_its_ip_suggests(app, client):
    with patch("broker.services.geoip.suggest_weather_location", return_value="London") as suggest:
        response = client.post(
            "/api/devices/register",
            json={"secret": DISPLAY, "width": 480, "height": 800},
            environ_base={"REMOTE_ADDR": "81.2.69.1"},
        )
    assert response.status_code == 201
    suggest.assert_called_once_with("81.2.69.1")
    with app.app_context():
        config = db.session.get_one(DeviceConfig, response.get_json()["device_id"])
        assert config.weather_location == "London"
        assert config.weather_place_id is None


def test_a_new_screen_whose_ip_cant_be_placed_has_no_weather_location(app, client):
    with patch("broker.services.geoip.suggest_weather_location", return_value=None):
        response = _register(client, DISPLAY)
    with app.app_context():
        assert db.session.get_one(DeviceConfig, response.get_json()["device_id"]).weather_location == ""


def test_a_repeat_registration_leaves_the_weather_location_alone(app, client):
    device_id = _register(client, DISPLAY).get_json()["device_id"]
    with app.app_context():
        db.session.get_one(DeviceConfig, device_id).weather_location = "Leeds"
        db.session.commit()

    with patch("broker.services.geoip.suggest_weather_location", return_value="London") as suggest:
        assert _register(client, DISPLAY).status_code == 200
    suggest.assert_not_called()
    with app.app_context():
        assert db.session.get_one(DeviceConfig, device_id).weather_location == "Leeds"


@pytest.mark.parametrize("by_token", [False, True])
def test_pairing_subscribes_to_the_weather_registration_gave(app, client, fake_redis, by_token):
    with patch("broker.services.geoip.suggest_weather_location", return_value="London"):
        device_id = _register(client, DISPLAY).get_json()["device_id"]

    if by_token:
        with app.app_context():
            token = db.session.get_one(Device, device_id).pairing_token
        assert client.post("/pair/token", json={"token": token}).status_code == 200
    else:
        with app.app_context():
            code = pairing.issue_code(device_id)
        assert client.post("/pair", data={"code": code}).status_code == 302

    assert fake_redis.sets["v1:sub:weather"] == {"london"}
