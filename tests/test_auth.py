from datetime import UTC, datetime

import pytest
from flask import g, jsonify
from werkzeug.security import generate_password_hash

from broker.auth import hash_secret, require_device_auth
from broker.db import db
from broker.models import Device, DeviceConfig


def test_require_device_auth_rejects_missing_header(client, register_device):
    device_id, _ = register_device()
    response = client.get(f"/api/devices/{device_id}/config")
    assert response.status_code == 401


def test_require_device_auth_rejects_non_bearer_header(client, register_device):
    device_id, secret = register_device()
    response = client.get(f"/api/devices/{device_id}/config", headers={"Authorization": secret})
    assert response.status_code == 401


def test_require_device_auth_accepts_correct_secret(client, register_device):
    device_id, secret = register_device()
    response = client.get(f"/api/devices/{device_id}/config", headers={"Authorization": f"Bearer {secret}"})
    assert response.status_code == 200


def test_require_paired_session_redirects_when_unpaired(client):
    response = client.get("/device")
    assert response.status_code == 302
    assert response.headers["Location"] == "/pair"


def test_require_paired_session_allows_paired_browser(paired_client):
    client, _, _ = paired_client
    response = client.get("/device")
    assert response.status_code == 200


def test_require_device_auth_rejects_an_empty_bearer_secret(client, register_device):
    device_id, _ = register_device()
    response = client.get(f"/api/devices/{device_id}/config", headers={"Authorization": "Bearer "})
    assert response.status_code == 401


def test_require_device_auth_forbids_a_valid_secret_for_the_wrong_role(client, split_device):
    device_id, _, display_secret = split_device
    response = client.get(f"/api/devices/{device_id}/config", headers={"Authorization": f"Bearer {display_secret}"})
    assert response.status_code == 403
    assert response.get_json() == {"error": "forbidden"}


def test_require_device_auth_rejects_a_secret_on_another_devices_path(client, register_device, split_device):
    other_id, _ = register_device("another-very-long-device-secret")
    _, renderer_secret, _ = split_device
    response = client.get(f"/api/devices/{other_id}/config", headers={"Authorization": f"Bearer {renderer_secret}"})
    assert response.status_code == 401


@pytest.mark.parametrize("role", ["renderer", "display"])
def test_require_device_auth_sets_device_id_and_role(app, split_device, role):
    device_id, renderer_secret, display_secret = split_device
    secret = renderer_secret if role == "renderer" else display_secret

    @require_device_auth()
    def view(device_id):
        return jsonify(device_id=g.device_id, role=g.role)

    with app.test_request_context(headers={"Authorization": f"Bearer {secret}"}):
        assert view(device_id=device_id).get_json() == {"device_id": device_id, "role": role}


# --- lazy rehash of werkzeug hashes ---------------------------------------------------------------


def _legacy_device(app, secret):
    """A device as registered before secrets were SHA-256: a salted werkzeug hash."""
    with app.app_context():
        db.session.add(
            Device(
                device_id="legacy",
                renderer_secret_hash=generate_password_hash(secret),
                created_at=datetime.now(UTC),
                config=DeviceConfig(),
            )
        )
        db.session.commit()


def _stored_hash(app):
    with app.app_context():
        return db.session.get(Device, "legacy").renderer_secret_hash


def test_a_legacy_hash_is_rewritten_as_sha256_when_it_verifies(app, client):
    secret = "a-pi-secret-from-before-roles"
    _legacy_device(app, secret)
    assert client.get("/api/config", headers={"Authorization": f"Bearer {secret}"}).status_code == 401

    response = client.get("/api/devices/legacy/config", headers={"Authorization": f"Bearer {secret}"})

    assert response.status_code == 200
    assert _stored_hash(app) == hash_secret(secret)
    # Now the secret alone finds the device.
    assert client.get("/api/config", headers={"Authorization": f"Bearer {secret}"}).get_json()["device_id"] == "legacy"


def test_a_wrong_secret_leaves_a_legacy_hash_alone(app, client):
    _legacy_device(app, "a-pi-secret-from-before-roles")
    before = _stored_hash(app)

    response = client.get("/api/devices/legacy/config", headers={"Authorization": "Bearer wrong-secret-value"})

    assert response.status_code == 401
    assert _stored_hash(app) == before


def test_session_cookie_is_host_only_by_default(paired_client):
    client, _, _ = paired_client

    assert client.get_cookie("session").domain is None or not client.get_cookie("session").domain.startswith(".")


def test_session_cookie_domain_comes_from_the_environment(monkeypatch, app):
    monkeypatch.setenv("SESSION_COOKIE_DOMAIN", ".example.com")
    from broker import create_app

    assert create_app().config["SESSION_COOKIE_DOMAIN"] == ".example.com"
