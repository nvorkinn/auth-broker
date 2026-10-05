import pytest
from flask import g, jsonify

from broker.auth import require_device_auth


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
        assert view(device_id).get_json() == {"device_id": device_id, "role": role}
