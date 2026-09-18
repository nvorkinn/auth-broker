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
