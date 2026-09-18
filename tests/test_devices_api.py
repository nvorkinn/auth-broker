from unittest.mock import patch


def test_register_creates_device(client):
    response = client.post("/api/devices/register", json={"device_secret": "a-very-long-device-secret-value"})
    assert response.status_code == 201
    assert "device_id" in response.get_json()


def test_register_rejects_short_secret(client):
    response = client.post("/api/devices/register", json={"device_secret": "short"})
    assert response.status_code == 400


def test_register_rejects_missing_body(client):
    response = client.post("/api/devices/register")
    assert response.status_code == 400


def test_pairing_code_requires_auth(client, register_device):
    device_id, _ = register_device()
    response = client.post(f"/api/devices/{device_id}/pairing-code")
    assert response.status_code == 401


def test_pairing_code_rejects_wrong_secret(client, register_device):
    device_id, _ = register_device()
    response = client.post(f"/api/devices/{device_id}/pairing-code", headers={"Authorization": "Bearer wrong-secret"})
    assert response.status_code == 401


def test_pairing_code_rejects_unknown_device(client, register_device):
    _, secret = register_device()
    response = client.post("/api/devices/does-not-exist/pairing-code", headers={"Authorization": f"Bearer {secret}"})
    assert response.status_code == 401


def test_pairing_code_uses_unambiguous_alphabet(client, register_device):
    device_id, secret = register_device()
    response = client.post(f"/api/devices/{device_id}/pairing-code", headers={"Authorization": f"Bearer {secret}"})
    assert response.status_code == 200
    body = response.get_json()
    assert len(body["code"]) == 6
    assert body["expires_in_seconds"] == 600
    for ambiguous_char in "0O1IL":
        assert ambiguous_char not in body["code"]


def test_requesting_new_pairing_code_invalidates_old_one(client, register_device):
    device_id, secret = register_device()
    headers = {"Authorization": f"Bearer {secret}"}

    first = client.post(f"/api/devices/{device_id}/pairing-code", headers=headers).get_json()["code"]
    client.post(f"/api/devices/{device_id}/pairing-code", headers=headers)

    response = client.post("/pair", data={"code": first})
    assert b"invalid or has expired" in response.data


def test_get_config_requires_auth(client, register_device):
    device_id, _ = register_device()
    response = client.get(f"/api/devices/{device_id}/config")
    assert response.status_code == 401


def test_get_config_returns_defaults_and_shared_keys(client, register_device):
    device_id, secret = register_device()
    response = client.get(f"/api/devices/{device_id}/config", headers={"Authorization": f"Bearer {secret}"})
    assert response.status_code == 200
    assert response.get_json() == {
        "interval": 15,
        "weather": {"api_key": "test-weather-key", "location": ""},
        "tfl": {"app_key": "test-tfl-key", "stop_ids": []},
        "spotify": {"enabled": False},
        "glowmarkt": {"username": None, "password": None},
    }


def test_get_config_returns_decrypted_glowmarkt_credentials(paired_client):
    client, device_id, secret = paired_client
    client.post("/device", data={"glowmarkt_username": "someone@example.com", "glowmarkt_password": "hunter2"})

    response = client.get(f"/api/devices/{device_id}/config", headers={"Authorization": f"Bearer {secret}"})
    assert response.get_json()["glowmarkt"] == {"username": "someone@example.com", "password": "hunter2"}


def test_now_playing_returns_null_when_spotify_disabled(client, register_device):
    device_id, secret = register_device()
    response = client.get(f"/api/devices/{device_id}/now-playing", headers={"Authorization": f"Bearer {secret}"})
    assert response.status_code == 200
    assert response.get_json() is None


def test_now_playing_returns_null_when_enabled_but_not_linked(paired_client):
    client, device_id, secret = paired_client
    client.post("/device", data={"interval": "15", "spotify_enabled": "on"})

    response = client.get(f"/api/devices/{device_id}/now-playing", headers={"Authorization": f"Bearer {secret}"})
    assert response.status_code == 200
    assert response.get_json() is None


def test_now_playing_proxies_spotify_module(paired_client):
    client, device_id, secret = paired_client
    client.post("/device", data={"interval": "15", "spotify_enabled": "on"})

    track = {
        "song": "A Song",
        "artist": "An Artist",
        "album": "An Album",
        "album_image": "http://x",
        "is_playing": True,
    }
    with patch("broker.devices_api.spotify_module.get_current_track", return_value=track) as mocked:
        response = client.get(f"/api/devices/{device_id}/now-playing", headers={"Authorization": f"Bearer {secret}"})

    mocked.assert_called_once_with(device_id)
    assert response.get_json() == track


def test_now_playing_unknown_device_rejected(client, register_device):
    _, secret = register_device()
    response = client.get("/api/devices/does-not-exist/now-playing", headers={"Authorization": f"Bearer {secret}"})
    assert response.status_code == 401
