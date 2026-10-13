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


USER = "user-1"
USERNAME = "tester"


@pytest.fixture
def client(app):
    """A browser signed in as USER: the header the login proxy sets (see auth.current_user). Device API
    calls ignore it."""
    client = app.test_client()
    client.environ_base["HTTP_X_AUTHENTIK_UID"] = USER
    client.environ_base["HTTP_X_AUTHENTIK_USERNAME"] = USERNAME
    return client


@pytest.fixture
def anonymous_client(app):
    """A request that didn't come through the login proxy."""
    return app.test_client()


@pytest.fixture
def cli(app):
    """Runs a `flask devices ...` command against the test app: cli("list"), cli("unpair", device_id)."""
    runner = app.test_cli_runner()

    def _invoke(*args: str):
        return runner.invoke(args=["devices", *args])

    return _invoke


@pytest.fixture
def claim_renderer(app, client):
    """Gives a registered screen a renderer the way the orchestrator does: its owner names it (here the
    assignment is made directly), then the renderer claims its device with the token. Returns the claim response."""

    def _claim(display_secret: str, renderer_secret: str):
        import secrets

        from sqlalchemy import select

        from broker.auth import hash_secret
        from broker.db import db
        from broker.models import Device
        from broker.services import assignments

        with app.app_context():
            device = db.session.scalar(select(Device).where(Device.display_secret_hash == hash_secret(display_secret)))
            token = assignments.create(device, secrets.token_hex(3), None).claim_token
        return client.post("/api/renderers/claim", json={"secret": renderer_secret, "claim_token": token})

    return _claim


@pytest.fixture
def register_device(client, claim_renderer):
    """Registers a screen (whose secret is "display-" plus the given one) and then a renderer with the given secret,
    returning (device_id, secret)."""

    def _register(secret: str = "a-very-long-device-secret-value"):
        screen = client.post("/api/devices/register", json={"secret": "display-" + secret})
        assert screen.status_code == 201
        response = claim_renderer("display-" + secret, secret)
        assert response.status_code == 201
        return response.get_json()["device_id"], secret

    return _register


@pytest.fixture
def split_device(client, claim_renderer):
    """A device whose screen registered and then had a renderer claim it: returns (device_id,
    renderer_secret, display_secret)."""
    renderer_secret, display_secret = "renderer-secret-0123456789", "display-secret-0123456789"
    screen = client.post("/api/devices/register", json={"secret": display_secret})
    assert screen.status_code == 201
    response = claim_renderer(display_secret, renderer_secret)
    assert response.status_code == 201
    assert response.get_json() == screen.get_json()
    return response.get_json()["device_id"], renderer_secret, display_secret


@pytest.fixture
def paired_client(client, register_device):
    """A test client whose user is already paired with a freshly-registered device."""
    device_id, device_secret = register_device()

    code_response = client.post(
        "/api/devices/pairing-code",
        headers={"Authorization": f"Bearer {device_secret}"},
    )
    code = code_response.get_json()["code"]

    pair_response = client.post("/pair", data={"code": code})
    assert pair_response.status_code == 302

    return client, device_id, device_secret
