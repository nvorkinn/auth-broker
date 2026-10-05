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

    from cryptography.fernet import Fernet

    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", Fernet.generate_key().decode())

    from broker import create_app

    application = create_app()
    application.config.update(TESTING=True)
    # Tests register far more devices a minute than the real limit allows; the throttle's own
    # tests put the real one back (see register_throttle).
    from broker.services.pair_throttle import PairThrottle

    application.extensions["register_throttle"] = PairThrottle(max_events=10_000)
    yield application

    from broker.db import db

    with application.app_context():
        db.engine.dispose()


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def cli(app):
    """Runs a `flask devices ...` command against the test app: cli("list"), cli("unpair", device_id)."""
    runner = app.test_cli_runner()

    def _invoke(*args: str):
        return runner.invoke(args=["devices", *args])

    return _invoke


@pytest.fixture
def register_device(client):
    """Registers a standalone device and returns (device_id, secret)."""

    def _register(secret: str = "a-very-long-device-secret-value"):
        response = client.post("/api/devices/register", json={"role": "renderer", "standalone": True, "secret": secret})
        assert response.status_code == 201
        return response.get_json()["device_id"], secret

    return _register


@pytest.fixture
def register_pending(client):
    """Registers a renderer or display through the pending pool and returns the response."""

    def _register(role: str, secret: str):
        return client.post("/api/devices/register", json={"role": role, "secret": secret})

    return _register


@pytest.fixture
def split_device(register_pending):
    """A device matched through the pool, screen first: returns (device_id, renderer_secret, display_secret)."""
    renderer_secret, display_secret = "renderer-secret-0123456789", "display-secret-0123456789"
    assert register_pending("display", display_secret).status_code == 202
    response = register_pending("renderer", renderer_secret)
    assert response.status_code == 201
    return response.get_json()["device_id"], renderer_secret, display_secret


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
