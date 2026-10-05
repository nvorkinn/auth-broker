"""POST /api/devices/register: standalone devices, the pending pool that matches a renderer with a
screen, and the ID-less endpoints a matched client polls."""

import threading
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from sqlalchemy import select

from broker.auth import hash_secret
from broker.db import db
from broker.models import Device, DeviceConfig, PendingRegistration
from broker.routes.frames import FRAME_BYTES
from broker.services import registration
from broker.services.pair_throttle import REGISTRATIONS_PER_MINUTE, PairThrottle

RENDERER = "renderer-secret-0123456789"
DISPLAY = "display-secret-0123456789"


def _auth(secret):
    return {"Authorization": f"Bearer {secret}"}


def _device(app, device_id):
    with app.app_context():
        return db.session.get(Device, device_id)


def _pending(app):
    with app.app_context():
        return [(row.role, row.secret_hash) for row in registration.pending()]


# --- standalone -----------------------------------------------------------------------------------


def test_standalone_creates_a_device_with_only_a_renderer_secret(app, client):
    response = client.post("/api/devices/register", json={"role": "renderer", "standalone": True, "secret": RENDERER})

    assert response.status_code == 201
    device = _device(app, response.get_json()["device_id"])
    assert device.renderer_secret_hash == hash_secret(RENDERER)
    assert device.display_secret_hash is None
    assert _pending(app) == []


def test_the_legacy_body_registers_a_standalone_device(app, client):
    response = client.post("/api/devices/register", json={"device_secret": RENDERER})

    assert response.status_code == 201
    assert _device(app, response.get_json()["device_id"]).renderer_secret_hash == hash_secret(RENDERER)


def test_a_display_cannot_be_standalone(client):
    response = client.post("/api/devices/register", json={"role": "display", "standalone": True, "secret": DISPLAY})
    assert response.status_code == 400


def test_register_rejects_an_unknown_role(client):
    response = client.post("/api/devices/register", json={"role": "admin", "secret": RENDERER})
    assert response.status_code == 400


def test_register_rejects_a_non_string_secret(client):
    response = client.post("/api/devices/register", json={"role": "display", "secret": ["x"] * 20})
    assert response.status_code == 400


# --- the pending pool -----------------------------------------------------------------------------


@pytest.mark.parametrize("first", ["renderer", "display"])
def test_a_renderer_and_a_display_are_matched_in_either_order(app, register_pending, first):
    secrets_by_role = {"renderer": RENDERER, "display": DISPLAY}
    second = "display" if first == "renderer" else "renderer"

    waiting = register_pending(first, secrets_by_role[first])
    matched = register_pending(second, secrets_by_role[second])

    assert waiting.status_code == 202
    assert waiting.get_json() == {"status": "waiting"}
    assert matched.status_code == 201
    device = _device(app, matched.get_json()["device_id"])
    assert device.renderer_secret_hash == hash_secret(RENDERER)
    assert device.display_secret_hash == hash_secret(DISPLAY)
    with app.app_context():
        assert db.session.get(DeviceConfig, device.device_id) is not None
    assert _pending(app) == []


def test_clients_of_the_same_role_wait_for_the_other(app, register_pending):
    assert register_pending("display", "display-one-0123456789").status_code == 202
    assert register_pending("display", "display-two-0123456789").status_code == 202

    assert [role for role, _ in _pending(app)] == ["display", "display"]


def test_the_longest_waiting_client_is_matched_first(app, register_pending):
    register_pending("display", "display-one-0123456789")
    register_pending("display", "display-two-0123456789")

    device_id = register_pending("renderer", RENDERER).get_json()["device_id"]

    assert _device(app, device_id).display_secret_hash == hash_secret("display-one-0123456789")
    assert _pending(app) == [("display", hash_secret("display-two-0123456789"))]


def test_registering_again_while_waiting_is_a_no_op(app, register_pending):
    register_pending("display", DISPLAY)

    response = register_pending("display", DISPLAY)

    assert response.status_code == 202
    assert len(_pending(app)) == 1


def test_registering_again_after_matching_returns_the_same_device(split_device, register_pending):
    device_id, _, _ = split_device

    for role, secret in [("display", DISPLAY), ("renderer", RENDERER)]:
        response = register_pending(role, secret)
        assert response.status_code == 200
        assert response.get_json() == {"device_id": device_id}


def test_registering_a_standalone_secret_again_returns_its_device(client, register_device):
    device_id, secret = register_device()

    response = client.post("/api/devices/register", json={"role": "renderer", "standalone": True, "secret": secret})

    assert (response.status_code, response.get_json()) == (200, {"device_id": device_id})


def test_a_secret_cannot_be_reused_for_the_other_role(app, split_device, register_pending):
    assert register_pending("renderer", DISPLAY).status_code == 409
    assert register_pending("display", "waiting-renderer-0123456789").status_code == 202
    assert register_pending("renderer", "waiting-renderer-0123456789").status_code == 409


def test_unmatched_registrations_expire(app, client, register_pending):
    register_pending("display", DISPLAY)
    with app.app_context():
        row = db.session.get(PendingRegistration, hash_secret(DISPLAY))
        row.created_at = datetime.now(UTC) - registration.PENDING_TTL - timedelta(seconds=1)
        db.session.commit()

    # The next registration clears it out, after which the expired screen is a stranger again.
    register_pending("display", "another-display-0123456789")

    assert hash_secret(DISPLAY) not in [h for _, h in _pending(app)]
    assert client.get("/api/frame", headers=_auth(DISPLAY)).status_code == 401
    assert register_pending("display", DISPLAY).status_code == 202


def test_a_full_pool_refuses_new_clients_but_still_matches(app, register_pending, monkeypatch):
    monkeypatch.setattr(registration, "MAX_PENDING", 2)
    register_pending("display", "display-one-0123456789")
    register_pending("display", "display-two-0123456789")

    full = register_pending("display", "display-three-0123456789")

    assert full.status_code == 503
    assert full.headers["Retry-After"] == "60"
    assert len(_pending(app)) == 2
    # A renderer is refused too while the pool is full: the cap counts everyone waiting.
    assert register_pending("renderer", RENDERER).status_code == 503


def test_concurrent_registrations_never_share_a_partner(app, tmp_path):
    """Threads register renderers and displays at once against the real database file. Each device
    must end up with exactly one secret of each role, and no secret in two devices."""
    pairs = 8
    errors = []

    def register(role, secret):
        try:
            response = app.test_client().post("/api/devices/register", json={"role": role, "secret": secret})
            assert response.status_code in (201, 202), response.status_code
        except Exception as e:  # noqa: BLE001 -- collected and reported below
            errors.append(e)

    threads = [
        threading.Thread(target=register, args=(role, f"{role}-concurrent-secret-{i:02d}"))
        for i in range(pairs)
        for role in ("renderer", "display")
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    with app.app_context():
        devices = db.session.scalars(select(Device)).all()
        assert len(devices) == pairs
        assert all(d.renderer_secret_hash and d.display_secret_hash for d in devices)
        all_hashes = [h for d in devices for h in (d.renderer_secret_hash, d.display_secret_hash)]
        assert len(set(all_hashes)) == 2 * pairs
    assert _pending(app) == []


def test_a_simultaneous_repeat_of_the_same_secret_is_answered_as_a_repeat(app, client, monkeypatch):
    """Both requests pass the "already registered?" check before either writes; the second one's
    insert then hits the primary key."""
    real_existing = registration.existing
    calls = []

    def existing_that_misses_once(role, secret_hash):
        calls.append(role)
        return None if len(calls) == 1 else real_existing(role, secret_hash)

    client.post("/api/devices/register", json={"role": "display", "secret": DISPLAY})
    monkeypatch.setattr(registration, "existing", existing_that_misses_once)

    response = client.post("/api/devices/register", json={"role": "display", "secret": DISPLAY})

    assert response.status_code == 202
    assert len(_pending(app)) == 1


# --- throttling -----------------------------------------------------------------------------------


@pytest.fixture
def register_throttle(app):
    """Puts back the real /register limit the app fixture lifts."""
    app.extensions["register_throttle"] = PairThrottle(max_events=REGISTRATIONS_PER_MINUTE)


@pytest.mark.usefixtures("register_throttle")
def test_register_is_throttled_per_ip_counting_every_call(client, register_pending):
    for _ in range(REGISTRATIONS_PER_MINUTE):
        # The same secret each time: repeats are no-ops but still count.
        assert register_pending("display", DISPLAY).status_code == 202

    response = register_pending("display", DISPLAY)

    assert response.status_code == 429
    assert int(response.headers["Retry-After"]) > 0


@pytest.mark.usefixtures("register_throttle")
def test_polling_is_not_throttled(client, register_pending):
    register_pending("display", DISPLAY)
    for _ in range(REGISTRATIONS_PER_MINUTE * 2):
        assert client.get("/api/frame", headers=_auth(DISPLAY)).status_code == 202


# --- the ID-less endpoints ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("role", "secret", "path"), [("display", DISPLAY, "/api/frame"), ("renderer", RENDERER, "/api/config")]
)
def test_a_waiting_client_is_told_to_wait(client, register_pending, role, secret, path):
    register_pending(role, secret)

    response = client.get(path, headers=_auth(secret))

    assert response.status_code == 202
    assert response.get_json() == {"status": "waiting"}


def test_a_matched_display_polls_frames_without_knowing_its_device_id(client, split_device):
    device_id, renderer_secret, display_secret = split_device
    assert client.get("/api/frame", headers=_auth(display_secret)).status_code == 404

    frame = bytes([0x5A]) * FRAME_BYTES
    put = client.put(f"/api/frames/{device_id}/frame", data=frame, headers=_auth(renderer_secret))
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


def test_a_standalone_pi_can_use_the_id_less_config(client, register_device):
    device_id, secret = register_device()
    assert client.get("/api/config", headers=_auth(secret)).get_json()["device_id"] == device_id


# --- CLI ------------------------------------------------------------------------------------------


def test_cli_pending_lists_waiting_clients_without_their_hashes(cli, register_pending):
    assert cli("pending").output == "Nothing waiting.\n"
    register_pending("display", DISPLAY)

    output = cli("pending").output

    assert output.startswith("display  waiting 0 min")
    assert hash_secret(DISPLAY) not in output


# --- the renderer's ID-less routes ----------------------------------------------------------------


def test_a_renderer_puts_frames_without_its_device_id(client, split_device):
    device_id, renderer_secret, display_secret = split_device
    frame = bytes([0x3C]) * FRAME_BYTES

    assert client.put("/api/frame", data=frame, headers=_auth(renderer_secret)).status_code == 204

    assert client.get("/api/frame", headers=_auth(display_secret)).data == frame
    assert client.get(f"/api/frames/{device_id}/frame", headers=_auth(display_secret)).data == frame


def test_an_id_less_put_is_forbidden_to_the_display(app, client, split_device):
    device_id, _, display_secret = split_device

    response = client.put("/api/frame", data=bytes(FRAME_BYTES), headers=_auth(display_secret))

    assert response.status_code == 403
    assert client.get("/api/frame", headers=_auth(display_secret)).status_code == 404


def test_an_id_less_put_checks_the_frame_size(client, split_device):
    _, renderer_secret, _ = split_device
    assert client.put("/api/frame", data=b"short", headers=_auth(renderer_secret)).status_code == 400


def test_an_id_less_put_from_a_waiting_renderer_is_told_to_wait(client, register_pending):
    register_pending("renderer", RENDERER)
    response = client.put("/api/frame", data=bytes(FRAME_BYTES), headers=_auth(RENDERER))
    assert response.status_code == 202


@pytest.mark.parametrize("path", ["/api/spotify/now-playing", "/api/spotify/queue", "/api/spotify/top/tracks"])
def test_id_less_spotify_routes_answer_the_renderer(client, split_device, path):
    _, renderer_secret, display_secret = split_device

    response = client.get(path, headers=_auth(renderer_secret))

    assert response.status_code == 200  # Spotify is off by default: the empty answer, not an error
    assert client.get(path, headers=_auth(display_secret)).status_code == 403
    assert client.get(path, headers=_auth("not-a-registered-secret")).status_code == 401


def test_id_less_spotify_routes_proxy_for_the_secrets_device(app, client, split_device):
    device_id, renderer_secret, _ = split_device
    with app.app_context():
        db.session.get(DeviceConfig, device_id).spotify_enabled = True
        db.session.commit()

    with patch("broker.clients.spotify.get_current_track", return_value={"song": "A Song"}) as mocked:
        response = client.get("/api/spotify/now-playing", headers=_auth(renderer_secret))

    mocked.assert_called_once_with(device_id)
    assert response.get_json() == {"song": "A Song"}


def test_an_unknown_spotify_top_type_is_not_found(client, split_device):
    _, renderer_secret, _ = split_device
    assert client.get("/api/spotify/top/albums", headers=_auth(renderer_secret)).status_code == 404


@pytest.mark.parametrize(
    ("method", "path"),
    [("get", "/api/config"), ("put", "/api/frame"), ("get", "/api/spotify/now-playing")],
)
def test_id_less_requests_are_logged_with_the_device_id(client, split_device, caplog, method, path):
    device_id, renderer_secret, _ = split_device

    with caplog.at_level("INFO"):
        getattr(client, method)(path, data=bytes(FRAME_BYTES), headers=_auth(renderer_secret))

    assert f"{method.upper()} {path} device=unnamed ({device_id}) role=renderer" in caplog.messages
