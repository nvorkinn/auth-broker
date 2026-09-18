import os

import pytest


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("FLASK_SECRET_KEY", "test-secret-key")
    monkeypatch.setenv("SPOTIFY_CLIENT_ID", "test-client-id")
    monkeypatch.setenv("SPOTIFY_CLIENT_SECRET", "test-client-secret")
    monkeypatch.setenv("SPOTIFY_REDIRECT_URI", "https://auth.example.com/auth/spotify/callback")
    monkeypatch.setenv("TFL_APP_KEY", "test-tfl-key")
    monkeypatch.setenv("WEATHER_API_KEY", "test-weather-key")
    monkeypatch.setenv("BROKER_DB_PATH", str(tmp_path / "broker.db"))

    from broker import create_app

    application = create_app()
    application.config.update(TESTING=True)
    yield application


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def register_device(client):
    """Registers a device and returns (device_id, device_secret)."""

    def _register(secret: str = "a-very-long-device-secret-value"):
        response = client.post("/api/devices/register", json={"device_secret": secret})
        assert response.status_code == 201
        return response.get_json()["device_id"], secret

    return _register


@pytest.fixture
def paired_client(client, register_device):
    """A test client whose session is already paired with a freshly-registered device."""
    device_id, device_secret = register_device()

    code_response = client.post(
        f"/api/devices/{device_id}/pairing-code",
        headers={"Authorization": f"Bearer {device_secret}"},
    )
    code = code_response.get_json()["code"]

    pair_response = client.post("/pair", data={"code": code})
    assert pair_response.status_code == 302

    return client, device_id, device_secret
