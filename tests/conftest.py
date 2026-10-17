import secrets
import shutil

import pytest
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from sqlalchemy import select

from broker import create_app
from broker.auth import hash_secret
from broker.clients import apple_music
from broker.db import db
from broker.models import Device
from broker.services import assignments
from broker.services.pair_throttle import PairThrottle


def _set_app_env(monkeypatch, db_path):
    monkeypatch.setenv("FLASK_SECRET_KEY", "test-secret-key")
    monkeypatch.setenv("SPOTIFY_CLIENT_ID", "test-client-id")
    monkeypatch.setenv("SPOTIFY_CLIENT_SECRET", "test-client-secret")
    monkeypatch.setenv("SPOTIFY_REDIRECT_URI", "https://auth.example.com/auth/spotify/callback")
    monkeypatch.setenv("TFL_APP_KEY", "test-tfl-key")
    monkeypatch.setenv("WEATHER_API_KEY", "test-weather-key")
    monkeypatch.setenv("BROKER_DB_PATH", str(db_path))

    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", Fernet.generate_key().decode())


@pytest.fixture(scope="session")
def migrated_db(tmp_path_factory):
    """Return a DB file migrated to head, built once per test session.

    Running every migration takes most of a second, and nearly every test starts an app; starting
    each on a copy of this one leaves its own upgrade with nothing to do.
    """
    path = tmp_path_factory.mktemp("migrated") / "broker.db"
    with pytest.MonkeyPatch.context() as monkeypatch:
        _set_app_env(monkeypatch, path)
        with create_app().app_context():
            db.engine.dispose()
    return path


@pytest.fixture
def app(tmp_path, monkeypatch, migrated_db):
    db_path = tmp_path / "broker.db"
    shutil.copyfile(migrated_db, db_path)
    _set_app_env(monkeypatch, db_path)

    application = create_app()
    application.config.update(TESTING=True)
    # Tests register far more devices a minute than the real limit allows; the throttle's own
    # tests put the real one back (see register_throttle).
    application.extensions["register_throttle"] = PairThrottle(max_events=10_000)
    yield application

    with application.app_context():
        db.engine.dispose()


USER = "user-1"
USERNAME = "tester"
DEVICE_SECRET = "a-very-long-device-secret-value"


@pytest.fixture
def client(app):
    """Return a browser signed in as USER.

    It sends the header the login proxy sets (see auth.current_user); device API calls ignore it.
    """
    client = app.test_client()
    client.environ_base["HTTP_X_AUTHENTIK_UID"] = USER
    client.environ_base["HTTP_X_AUTHENTIK_USERNAME"] = USERNAME
    return client


@pytest.fixture
def anonymous_client(app):
    """Return a client whose requests didn't come through the login proxy."""
    return app.test_client()


@pytest.fixture
def cli(app):
    """Run a `flask devices ...` command against the test app.

    For example: cli("list"), cli("unpair", device_id).
    """
    runner = app.test_cli_runner()

    def _invoke(*args: str):
        return runner.invoke(args=["devices", *args])

    return _invoke


@pytest.fixture
def claim_renderer(app, client):
    """Give a registered screen a renderer the way the orchestrator does; return the claim response.

    Its owner names it (here the assignment is made directly), then the renderer claims its device with
    the token.
    """

    def _claim(display_secret: str, renderer_secret: str):
        with app.app_context():
            device = db.session.scalar(select(Device).where(Device.display_secret_hash == hash_secret(display_secret)))
            token = assignments.create(device, secrets.token_hex(3), None).claim_token
        return client.post("/api/renderers/claim", json={"secret": renderer_secret, "claim_token": token})

    return _claim


@pytest.fixture
def register_device(client, claim_renderer):
    """Register a screen and then a renderer with the given secret, returning (device_id, secret).

    The screen's secret is "display-" plus the given one.
    """

    def _register(secret: str = DEVICE_SECRET):
        screen = client.post("/api/devices/register", json={"secret": "display-" + secret, "width": 480, "height": 800})
        assert screen.status_code == 201
        response = claim_renderer("display-" + secret, secret)
        assert response.status_code == 201
        return response.get_json()["device_id"], secret

    return _register


@pytest.fixture
def split_device(client, claim_renderer):
    """Register a screen and have a renderer claim it.

    Returns (device_id, renderer_secret, display_secret).
    """
    renderer_secret, display_secret = "renderer-secret-0123456789", "display-secret-0123456789"
    screen = client.post("/api/devices/register", json={"secret": display_secret, "width": 480, "height": 800})
    assert screen.status_code == 201
    response = claim_renderer(display_secret, renderer_secret)
    assert response.status_code == 201
    assert response.get_json() == screen.get_json()
    return response.get_json()["device_id"], renderer_secret, display_secret


@pytest.fixture
def paired_client(client, register_device):
    """Return a test client whose user is already paired with a freshly-registered device."""
    device_id, device_secret = register_device()

    code_response = client.post(
        "/api/devices/pairing-code",
        headers={"Authorization": f"Bearer {device_secret}"},
    )
    code = code_response.get_json()["code"]

    pair_response = client.post("/pair", data={"code": code})
    assert pair_response.status_code == 302

    return client, device_id, device_secret


def apple_song(song_id, name="Efecto", artist="Bad Bunny", album="Un Verano Sin Ti"):
    """Return an Apple Music song resource, as /me/recent/played/tracks hands it over."""
    return {
        "id": song_id,
        "type": "songs",
        "attributes": {
            "name": name,
            "artistName": artist,
            "albumName": album,
            "durationInMillis": 213061,
            "artwork": {"width": 3000, "height": 3000, "url": "https://is5-ssl.mzstatic.com/x/{w}x{h}bb.jpg"},
        },
    }


@pytest.fixture
def musickit(app, monkeypatch):
    """Give the app a MusicKit key, and return its public half."""
    key = ec.generate_private_key(ec.SECP256R1())
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    app.config.update(APPLE_MUSIC_TEAM_ID="TEAM123", APPLE_MUSIC_KEY_ID="KEY456", APPLE_MUSIC_PRIVATE_KEY=pem.decode())
    monkeypatch.setattr(apple_music, "_developer_token", None)
    return key.public_key()
