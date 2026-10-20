import fnmatch
import shutil
from datetime import UTC, datetime
from hashlib import blake2b

import fakeredis
import pytest
import redis
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from broker import create_app
from broker.clients import apple_music
from broker.db import db
from broker.models import Device, DeviceConfig
from broker.services import assignments, frame_record, pairing, render_jobs, renderer_config
from broker.services.pair_throttle import PairThrottle


def _set_app_env(monkeypatch, db_path):
    monkeypatch.setenv("FLASK_SECRET_KEY", "test-secret-key")
    monkeypatch.setenv("SPOTIFY_CLIENT_ID", "test-client-id")
    monkeypatch.setenv("SPOTIFY_CLIENT_SECRET", "test-client-secret")
    monkeypatch.setenv("SPOTIFY_REDIRECT_URI", "https://auth.example.com/auth/spotify/callback")
    monkeypatch.setenv("TFL_APP_KEY", "test-tfl-key")
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
    application.extensions["redis"] = FakeRedis()
    # The frame records' client (services/frame_record.py), which doesn't decode: empty until a test draws a frame.
    application.extensions["redis_bytes"] = fakeredis.FakeRedis()
    # Tests register far more devices a minute than the real limit allows; the throttle's own
    # tests put the real one back (see register_throttle).
    application.extensions["register_throttle"] = PairThrottle(max_events=10_000)
    yield application

    with application.app_context():
        db.engine.dispose()


class FakeRedis:
    """Just enough of Redis to see what a config save writes: sets, strings (configs, pairing keys), what's published.

    `jobs` are the render jobs queued (services/render_jobs.py), oldest first and each device once, as the real script
    queues them (tests/services/test_render_jobs.py runs it), for the screens in `pooled` (`v1:render:screens`). TTLs
    are ignored, so a job's flag never lapses.
    """

    def __init__(self):
        self.sets: dict[str, set[str]] = {}
        self.strings: dict[str, str] = {}
        self.published: list[tuple[str, str]] = []
        self.jobs: list[str] = []
        self.pooled: dict[str, str] = {}
        self.fail = False

    def _check(self):
        if self.fail:
            msg = "redis is down"
            raise redis.ConnectionError(msg)

    def pipeline(self, *, transaction=True):  # noqa: ARG002
        return FakePipeline(self)

    def get(self, key):
        self._check()
        return self.strings.get(key)

    def mget(self, keys):
        self._check()
        return [self.strings.get(key) for key in keys]

    def set(self, key, value, **_expiry):
        self._check()
        self.strings[key] = value

    def delete(self, *keys):
        self._check()
        for key in keys:
            self.sets.pop(key, None)
            self.strings.pop(key, None)

    def scan_iter(self, match, count=None):  # noqa: ARG002
        self._check()
        return [key for key in [*self.strings, *self.sets] if fnmatch.fnmatchcase(key, match)]

    def hmget(self, key, fields):
        self._check()
        assert key == render_jobs.SCREENS
        return [self.pooled.get(field) for field in fields]

    def hgetall(self, key):
        self._check()
        assert key == render_jobs.SCREENS
        return dict(self.pooled)

    def register_script(self, script):
        assert script == render_jobs._ENQUEUE, "only the render queue's enqueue is faked"
        return FakeEnqueue(self)

    def _enqueue(self, device_id) -> int:
        if device_id in self.jobs:
            return 0
        self.jobs.append(device_id)
        return 1


class FakeEnqueue:
    """The render queue's enqueue script, on a FakeRedis or in its pipeline."""

    def __init__(self, server: FakeRedis):
        self.server = server

    def __call__(self, keys, args, client=None):
        assert keys == [render_jobs.queued_key(args[0]), render_jobs.JOBS]
        if isinstance(client, FakePipeline):
            client.ops.append(("enqueue", args[0]))
            return client
        self.server._check()
        return self.server._enqueue(args[0])


class FakePipeline:
    def __init__(self, server: FakeRedis):
        self.server = server
        self.ops = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def delete(self, key):
        self.ops.append(("delete", key))

    def set(self, key, value):
        self.ops.append(("set", key, value))

    def sadd(self, key, *members):
        self.ops.append(("sadd", key, members))

    def publish(self, channel, message):
        self.ops.append(("publish", channel, message))

    def hset(self, key, mapping):
        self.ops.append(("hset", key, mapping))

    def hdel(self, key, *fields):
        self.ops.append(("hdel", key, fields))

    def execute(self):
        self.server._check()
        results = []
        for op, key, *rest in self.ops:
            results.append(True)
            if op == "enqueue":
                results[-1] = self.server._enqueue(key)
            elif op == "delete":
                self.server.sets.pop(key, None)
                self.server.strings.pop(key, None)
            elif op == "set":
                self.server.strings[key] = rest[0]
            elif op == "sadd":
                self.server.sets.setdefault(key, set()).update(rest[0])
            elif op in {"hset", "hdel"}:
                assert key == render_jobs.SCREENS
                if op == "hset":
                    self.server.pooled.update(rest[0])
                else:
                    for field in rest[0]:
                        self.server.pooled.pop(field, None)
            else:
                self.server.published.append((key, rest[0]))
        self.ops = []
        return results


@pytest.fixture
def fake_redis(app):
    """Replace the app's Redis with an in-memory one."""
    fake = FakeRedis()
    app.extensions["redis"] = fake
    return fake


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


def name_screen(device_id: str) -> None:
    """Give a registered screen its stable name, as pairing does, so the render pool can list it (app context)."""
    assignments.auto_name(db.session.get(Device, device_id))
    db.session.commit()


@pytest.fixture
def register_device(app, client):
    """Register a screen with the given secret and name it, returning (device_id, secret). It isn't paired."""

    def _register(secret: str = DEVICE_SECRET):
        screen = client.post("/api/devices/register", json={"secret": secret, "width": 480, "height": 800})
        assert screen.status_code == 201
        device_id = screen.get_json()["device_id"]
        with app.app_context():
            name_screen(device_id)
        return device_id, secret

    return _register


@pytest.fixture
def paired_client(app, client, register_device):
    """Return (client, device_id, the screen's secret): the client's user paired by a code with a new named screen."""
    device_id, device_secret = register_device()
    with app.app_context():
        code = pairing.issue_code(device_id)

    pair_response = client.post("/pair", data={"code": code})
    assert pair_response.status_code == 302

    return client, device_id, device_secret


@pytest.fixture
def config_of(app):
    """Return a function giving a device's config as the render pool reads it (renderer_config.build), as JSON."""

    def _config_of(device_id: str) -> dict:
        with app.app_context():
            config = db.session.get(DeviceConfig, device_id)
            return renderer_config.build(config, pairing_code=None).model_dump(mode="json")

    return _config_of


@pytest.fixture
def draw(app):
    """Return a function that draws a screen's frame as the render pool does: its record in Redis.

    draw(device_id, frame, at=None) writes `v1:frame:<device id>` (the shape the contract's frame_record.json pins) to
    the app's bytes Redis, with `at` (default now) as when it was drawn.
    """

    def _draw(device_id: str, frame: bytes, at: datetime | None = None) -> None:
        redis_bytes = app.extensions["redis_bytes"]
        values = {
            "frame": frame,
            "etag": blake2b(frame, digest_size=8).hexdigest(),
            "rendered_at": (at or datetime.now(UTC)).isoformat(timespec="milliseconds"),
        }
        redis_bytes.delete(frame_record.key(device_id))
        redis_bytes.hset(frame_record.key(device_id), mapping=values)
        redis_bytes.expire(frame_record.key(device_id), 86_400)

    return _draw


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
