"""Test registration: POST /api/devices/register (screens) and POST /api/renderers/claim (renderers).

Also the ID-less endpoints a registered client polls.
"""

import threading
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from broker.auth import hash_secret
from broker.db import db
from broker.models import Device, DeviceConfig
from broker.routes.frames import FRAME_BYTES
from broker.services import pace, registration
from broker.services.pair_throttle import MAX_REGISTRATION_LOCKOUT_SECONDS, REGISTRATIONS_PER_MINUTE, PairThrottle

USER = "user-1"
RENDERER = "renderer-secret-0123456789"
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
    assert device.renderer_secret_hash is None
    with app.app_context():
        assert db.session.get(DeviceConfig, device.device_id) is not None


def test_registering_a_screen_again_returns_its_device(client):
    device_id = _register(client, DISPLAY).get_json()["device_id"]

    response = _register(client, DISPLAY)

    assert (response.status_code, response.get_json()) == (200, {"device_id": device_id})


def test_a_renderers_secret_cannot_register_as_a_screen(client, split_device):
    _, renderer_secret, _ = split_device

    assert _register(client, renderer_secret).status_code == 409


def test_screens_nobody_has_paired_are_capped_and_expire(app, client, monkeypatch):
    monkeypatch.setattr(registration, "MAX_UNPAIRED_SCREENS", 2)
    first = _register(client, "display-one-0123456789").get_json()["device_id"]
    _register(client, "display-two-0123456789")

    full = _register(client, "display-three-0123456789")

    assert full.status_code == 503
    assert full.headers["Retry-After"] == "60"
    with app.app_context():
        db.session.get(Device, first).created_at = datetime.now(UTC) - registration.UNPAIRED_TTL - timedelta(seconds=1)
        db.session.commit()
    # The stale one is forgotten to make room, and a screen's next call is a 401 so it registers again.
    assert _register(client, "display-three-0123456789").status_code == 201
    assert _device(app, first) is None


def test_a_paired_screen_never_expires(app, client):
    device_id = _register(client, DISPLAY).get_json()["device_id"]
    with app.app_context():
        device = db.session.get(Device, device_id)
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


# --- renderers ------------------------------------------------------------------------------------


def test_a_renderer_claims_its_screens_device(app, split_device):
    device_id, renderer_secret, display_secret = split_device

    device = _device(app, device_id)
    assert (device.renderer_secret_hash, device.display_secret_hash) == (
        hash_secret(renderer_secret),
        hash_secret(display_secret),
    )


def test_a_claim_names_the_device_after_its_renderer(app, split_device):
    """Its slug: the DEVICE_NAME the orchestrator starts it with, which a renderer reading the store never sends."""
    device_id, _, _ = split_device

    with app.app_context():
        device = db.session.get(Device, device_id)
        assert device.device_name == device.assignment.slug


def test_a_claim_keeps_a_name_the_device_already_has(app, client, register_device, claim_renderer):
    screen = client.post("/api/devices/register", json={"secret": "display-secret-0123456789", "width": 1, "height": 1})
    device_id = screen.get_json()["device_id"]
    with app.app_context():
        db.session.get(Device, device_id).device_name = "camilla"
        db.session.commit()

    claim_renderer("display-secret-0123456789", "renderer-secret-0123456789")

    assert _device(app, device_id).device_name == "camilla"


def test_claiming_again_returns_the_same_device_without_a_token(client, split_device):
    device_id, renderer_secret, _ = split_device

    response = client.post("/api/renderers/claim", json={"secret": renderer_secret, "claim_token": "spent"})

    assert (response.status_code, response.get_json()) == (200, {"device_id": device_id})


def test_a_screens_secret_cannot_claim(client, split_device):
    _, _, display_secret = split_device

    assert client.post("/api/renderers/claim", json={"secret": display_secret, "claim_token": "x"}).status_code == 409


@pytest.mark.parametrize(
    "body",
    [{"secret": "short", "claim_token": "x"}, {"secret": RENDERER}, {"secret": RENDERER, "claim_token": 5}],
)
def test_a_claim_needs_a_secret_and_a_token(client, body):
    assert client.post("/api/renderers/claim", json=body).status_code == 400


def test_an_unknown_claim_token_is_refused(client):
    assert client.post("/api/renderers/claim", json={"secret": RENDERER, "claim_token": "nonsense"}).status_code == 403


def test_a_claim_is_not_throttled(app, client):
    app.extensions["register_throttle"] = PairThrottle(max_events=1)

    for _ in range(5):
        assert client.post("/api/renderers/claim", json={"secret": RENDERER, "claim_token": "x"}).status_code == 403


# --- throttling -----------------------------------------------------------------------------------


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
def test_polling_is_not_throttled(client, split_device):
    _, renderer_secret, _ = split_device
    for _ in range(REGISTRATIONS_PER_MINUTE * 2):
        assert client.get("/api/config", headers=_auth(renderer_secret)).status_code == 200


# --- the ID-less endpoints ------------------------------------------------------------------------


def test_a_matched_display_polls_frames_without_knowing_its_device_id(client, split_device):
    _device_id, renderer_secret, display_secret = split_device
    assert client.get("/api/frame", headers=_auth(display_secret)).status_code == 404

    frame = bytes([0x5A]) * FRAME_BYTES
    put = client.put("/api/frame", data=frame, headers=_auth(renderer_secret))
    assert put.status_code == 204

    response = client.get("/api/frame", headers=_auth(display_secret))
    assert response.status_code == 200
    assert response.data == frame
    again = client.get("/api/frame", headers={**_auth(display_secret), "If-None-Match": response.headers["ETag"]})
    assert again.status_code == 304


def test_a_matched_renderer_learns_its_device_id_and_pairing_code_from_config(client, split_device):
    device_id, renderer_secret, _ = split_device

    body = client.get("/api/config", headers=_auth(renderer_secret)).get_json()

    assert body["device_id"] == device_id
    assert len(body["pairing_code"]) == 6


def test_config_is_forbidden_to_the_display(client, split_device):
    _, _, display_secret = split_device
    assert client.get("/api/config", headers=_auth(display_secret)).status_code == 403


@pytest.mark.parametrize("path", ["/api/frame", "/api/config"])
def test_id_less_endpoints_reject_unknown_and_missing_secrets(client, split_device, path):
    assert client.get(path).status_code == 401
    assert client.get(path, headers=_auth("not-a-registered-secret")).status_code == 401
    assert client.get(path, headers={"Authorization": "Bearer "}).status_code == 401


def test_a_renderer_can_use_the_id_less_config(client, register_device):
    device_id, secret = register_device()
    assert client.get("/api/config", headers=_auth(secret)).get_json()["device_id"] == device_id


# --- the renderer's ID-less routes ----------------------------------------------------------------


def test_a_renderer_puts_frames_without_its_device_id(client, split_device):
    _device_id, renderer_secret, display_secret = split_device
    frame = bytes([0x3C]) * FRAME_BYTES

    assert client.put("/api/frame", data=frame, headers=_auth(renderer_secret)).status_code == 204

    assert client.get("/api/frame", headers=_auth(display_secret)).data == frame
    assert client.get("/api/frame", headers=_auth(display_secret)).data == frame


def test_an_id_less_put_is_forbidden_to_the_display(app, client, split_device):
    _device_id, _, display_secret = split_device

    response = client.put("/api/frame", data=bytes(FRAME_BYTES), headers=_auth(display_secret))

    assert response.status_code == 403
    assert client.get("/api/frame", headers=_auth(display_secret)).status_code == 404


def test_an_id_less_put_checks_the_frame_size(client, split_device):
    _, renderer_secret, _ = split_device
    assert client.put("/api/frame", data=b"short", headers=_auth(renderer_secret)).status_code == 400


@pytest.mark.parametrize(("method", "path"), [("get", "/api/config"), ("put", "/api/frame")])
def test_id_less_requests_are_logged_with_the_device_id(app, client, split_device, caplog, method, path):
    device_id, renderer_secret, _ = split_device
    with app.app_context():
        slug = db.session.get(Device, device_id).assignment.slug

    with caplog.at_level("INFO"):
        getattr(client, method)(path, data=bytes(FRAME_BYTES), headers=_auth(renderer_secret))

    assert f"{method.upper()} {path} device={slug} ({device_id}) role=renderer" in caplog.messages


# --- Retry-After: how often to poll (services/pace.py; tests/routes/test_frames.py has the rest) ---------------------


@pytest.fixture
def frozen(monkeypatch):
    monkeypatch.setattr(pace, "clock", lambda: datetime(2026, 10, 18, 12, 0, 10, tzinfo=UTC))


@pytest.mark.usefixtures("frozen")
def test_no_frame_yet_is_a_json_404_with_the_usual_retry(client, split_device):
    _, _, display_secret = split_device

    response = client.get("/api/frame", headers=_auth(display_secret))

    assert response.status_code == 404
    assert response.get_json() == {"error": "no frame yet"}
    assert response.headers["Retry-After"] == "23"  # until 12:00:33, just after the next half-minute


@pytest.mark.usefixtures("frozen")
def test_a_frame_says_to_poll_again_just_after_the_next_half_minute(app, client, split_device):
    _device_id, renderer_secret, display_secret = split_device
    client.put("/api/frame", data=bytes(FRAME_BYTES), headers=_auth(renderer_secret))

    first = client.get("/api/frame", headers=_auth(display_secret))
    unchanged = client.get("/api/frame", headers={**_auth(display_secret), "If-None-Match": first.headers["ETag"]})

    assert (first.status_code, first.headers["Retry-After"]) == (200, "23")
    assert (unchanged.status_code, unchanged.headers["Retry-After"]) == (304, "23")
