"""The screens' configs in the store: `v1:config:<device id>`, written from the database, and a render job on change."""

import json
import runpy
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import fakeredis
import pytest
import redis
from countdown_api import fixtures
from countdown_api.models import ConfigResponse

from broker.db import db
from broker.models import Device, DeviceConfig, GitHubToken, GlowmarktCredentials, SpotifyToken
from broker.services import config_store, pairing, render_jobs

ROOT = Path(__file__).parent.parent.parent
# The stored shape, which pi-dashboard's renderer reads: both repositories' tests read this file.
FIXTURE = fixtures.load("config_store.json")


@pytest.fixture
def store(app):
    """Give the app a fake Redis that behaves like the real one (pub/sub too), and return it."""
    client = fakeredis.FakeRedis(decode_responses=True)
    app.extensions["redis"] = client
    return client


@pytest.fixture
def nudges(store):
    """Return a function that takes the render jobs queued since its last call and returns their device ids."""

    def queued() -> list[str]:
        found = list(reversed(store.lrange(render_jobs.JOBS, 0, -1)))
        store.delete(render_jobs.JOBS, *(render_jobs.queued_key(device_id) for device_id in found))
        return found

    return queued


def _add_fixture_device(app) -> str:
    """Add the device the store contract's config_store.json is the stored config of, paired; return its id."""
    settings = dict(FIXTURE["settings"])
    glowmarkt = settings.pop("glowmarkt")
    device_id = FIXTURE["device_id"]
    with app.app_context():
        now = datetime.now(UTC)
        device = Device(device_id=device_id, created_at=now, paired_at=now, owner="user-1")
        device.config = DeviceConfig(**settings)
        db.session.add(device)
        db.session.add(GlowmarktCredentials(device_id=device_id, username=glowmarkt["username"]))
        db.session.get_one(GlowmarktCredentials, device_id).password = glowmarkt["password"]
        db.session.commit()
    return device_id


def _as_read(value):
    """Return a stored config as the renderer reads it: the contract's model, with what the example omits filled in.

    The example omits the fields the contract defaults (weather.design), which the broker writes.
    """
    return ConfigResponse.model_validate(value).model_dump(mode="json")


def _stored(store, device_id):
    value = store.get(config_store.key(device_id))
    return None if value is None else json.loads(value)


# --- the stored value ---------------------------------------------------------------------------------------------


def test_the_stored_config_is_the_shape_the_renderer_reads(app, store):
    device_id = _add_fixture_device(app)

    with app.app_context():
        config_store.publish(device_id)

    assert config_store.key(device_id) == FIXTURE["key"]
    assert _stored(store, device_id) == _as_read(FIXTURE["value"])


def test_the_stored_config_is_the_renderers_config_with_no_code(app, store, paired_client, config_of):
    client, device_id, _secret = paired_client
    client.post("/device", data={"stops_order": "940GZZLUEUS,NSR:StopPlace:58366", "postcode": "SE17 3LL"})

    assert _stored(store, device_id) == config_of(device_id)
    assert _stored(store, device_id)["pairing_code"] is None


def test_the_stored_config_carries_no_secret(app, store, paired_client, monkeypatch):
    client, device_id, _ = paired_client
    app.config.update(TFL_APP_KEY="the-tfl-key")
    client.post("/device", data={"glowmarkt_username": "me@example.com", "glowmarkt_password": "glow-password"})
    with app.app_context():
        db.session.add(GitHubToken(device_id=device_id, token="ghp_the-github-token"))
        db.session.add(SpotifyToken(device_id=device_id, refresh_token="spotify-refresh", access_token="spotify-at"))
        db.session.commit()
        config_store.publish_all()

    stored = store.get(config_store.key(device_id))

    assert json.loads(stored)["glowmarkt"] == {"enabled": True}
    for secret in ("the-tfl-key", "glow-password", "ghp_the-github-token", "spotify-"):
        assert secret not in stored
    assert "me@example.com" not in stored  # nor the account's name


def test_the_stored_config_has_no_ttl(app, store):
    device_id = _add_fixture_device(app)

    with app.app_context():
        config_store.publish(device_id)

    assert store.ttl(config_store.key(device_id)) == -1


def test_the_stored_config_is_small(app, store):
    """A thousand screens' would take under 2 MB of Redis's 128: no TTL, so volatile-lru never evicts them."""
    device_id = _add_fixture_device(app)

    with app.app_context():
        config_store.publish(device_id)

    assert len(store.get(config_store.key(device_id))) < 2000


# --- saving -------------------------------------------------------------------------------------------------------


def test_saving_the_config_rewrites_the_key_and_queues_a_render(app, store, nudges, paired_client):
    client, device_id, _ = paired_client
    nudges()  # pairing's

    client.post("/device", data={"stops_order": "940GZZLUEUS", "style": "card"})

    stored = _stored(store, device_id)
    assert stored["departures"]["stops"] == [{"source": "tfl", "id": "940GZZLUEUS"}]
    assert stored["style"] == "card"
    assert nudges() == [device_id]


def test_a_save_queues_only_the_screens_whose_config_changed(app, store, nudges, paired_client, register_device):
    client, device_id, _ = paired_client
    other_id, _ = register_device("another-very-long-device-secret")
    with app.app_context():
        other = db.session.get_one(Device, other_id)
        other.paired_at = datetime.now(UTC)
        other.pairing_codes.clear()  # as pairing does: the code it registered with is spent
        db.session.commit()
        config_store.publish_all()
    nudges()

    client.post("/device", data={"style": "card"})

    # The saved screen's, once for its changed copy (the save's own job finds it queued already).
    assert nudges() == [device_id]
    assert _stored(store, other_id) is not None


def test_a_save_that_changes_nothing_queues_only_the_saved_screen(store, nudges, paired_client):
    client, device_id, _ = paired_client
    client.post("/device", data={"style": "card"})
    nudges()

    client.post("/device", data={"style": "card"})

    # An account replaced changes no copy, but what the screen shows: so the saved screen is drawn regardless.
    assert nudges() == [device_id]


def test_a_rejected_save_writes_nothing(store, nudges, paired_client):
    client, device_id, _ = paired_client
    before = _stored(store, device_id)
    nudges()

    response = client.post("/device", data={"postcode": "not a postcode", "style": "card"})

    assert response.status_code == 400
    assert _stored(store, device_id) == before
    assert nudges() == []


def test_the_config_is_saved_even_when_redis_is_down(app, paired_client):
    client, device_id, _ = paired_client
    app.extensions["redis"].fail = True  # conftest's fake: every call raises a ConnectionError

    with patch.object(config_store, "logger") as logger:
        response = client.post("/device", data={"style": "card"})

    assert response.status_code == 302
    with app.app_context():
        assert db.session.get_one(DeviceConfig, device_id).style == "card"
    logger.warning.assert_called_once_with("Couldn't write the screens' configs to Redis (%s)", "ConnectionError")


# --- pairing ------------------------------------------------------------------------------------------------------


def test_an_unpaired_device_has_no_stored_config(app, store, register_device):
    device_id, _ = register_device()

    with app.app_context():
        config_store.publish_all()

    # Nothing for the pool to draw until it's paired, or has a code to show (its own key).
    assert _stored(store, device_id) is None


def test_pairing_with_a_code_stores_the_config_and_queues_a_render(app, store, nudges, client, register_device):
    device_id, _secret = register_device()
    with app.app_context():
        code = pairing.issue_code(device_id)

    client.post("/pair", data={"code": code})

    assert _stored(store, device_id)["device_id"] == device_id
    assert nudges() == [device_id]


def test_pairing_by_token_stores_the_config(app, store, nudges, client):
    display_secret = "display-secret-for-a-token-pairing"
    device_id = client.post(
        "/api/devices/register",
        json={"secret": display_secret, "width": 800, "height": 480, "format": "mono-1bpp-packed"},
    )
    device_id = device_id.get_json()["device_id"]
    with app.app_context():
        token = db.session.get_one(Device, device_id).pairing_token

    assert client.post("/pair/token", json={"token": token}).status_code == 200

    assert _stored(store, device_id)["pairing_code"] is None
    assert nudges() == [device_id]  # named at pairing, so the pool draws it at once


def test_a_code_takes_the_config_out_of_the_store_until_it_is_used(app, store, nudges, paired_client):
    client, device_id, _secret = paired_client
    nudges()

    with app.app_context():
        code = pairing.issue_code(device_id)
        config_store.publish(device_id)

    # The pool draws the pairing screen from the code's own key meanwhile.
    assert _stored(store, device_id) is None
    assert nudges() == [device_id]

    client.post("/pair", data={"code": code})

    assert _stored(store, device_id)["pairing_code"] is None
    assert nudges() == [device_id]


def test_a_code_that_lapses_unused_leaves_the_config_out_until_the_next_rebuild(app, store, paired_client):
    _client, device_id, _secret = paired_client
    with app.app_context():
        pairing.issue_code(device_id)
        config_store.publish(device_id)
        for code in db.session.get_one(Device, device_id).pairing_codes:
            code.expires_at = datetime(2020, 1, 1, tzinfo=UTC)
        db.session.commit()
        assert pairing.live_code(device_id) is None

        config_store.publish_all()  # any config save, the broker starting, the minute's resync, `flask config publish`

    assert _stored(store, device_id) is not None


# --- the CLI and startup ------------------------------------------------------------------------------------------


def _cli(app, *args):
    return app.test_cli_runner().invoke(args=list(args))


def test_unpair_removes_the_stored_config_and_draws_the_code(app, store, nudges, paired_client):
    _, device_id, _ = paired_client
    nudges()

    assert _cli(app, "devices", "unpair", device_id).exit_code == 0

    assert _stored(store, device_id) is None
    assert nudges() == [device_id]


def test_forget_removes_the_stored_config(app, store, nudges, paired_client):
    _, device_id, _ = paired_client
    nudges()

    assert _cli(app, "devices", "forget", device_id).exit_code == 0

    assert _stored(store, device_id) is None
    assert nudges() == []  # gone from the pool's list first: nothing to draw


def test_a_code_from_the_cli_takes_the_config_out_of_the_store(app, store, paired_client):
    _, device_id, _ = paired_client

    assert _cli(app, "devices", "code", device_id).exit_code == 0

    assert _stored(store, device_id) is None


def test_owner_pairs_the_device_and_stores_its_config(app, store, register_device):
    device_id, _ = register_device()

    assert _cli(app, "devices", "owner", device_id, "user-2").exit_code == 0

    assert _stored(store, device_id)["device_id"] == device_id


def test_config_publish_rebuilds_the_configs_and_the_subscriptions(app, store, nudges, paired_client):
    client, device_id, _ = paired_client
    client.post("/device", data={"stops_order": "940GZZLUEUS"})
    store.flushall()  # as if Redis restarted: it keeps nothing on disk
    nudges()

    result = _cli(app, "config", "publish")

    assert result.exit_code == 0
    assert _stored(store, device_id)["departures"]["stops"] == [{"source": "tfl", "id": "940GZZLUEUS"}]
    assert store.smembers("v1:sub:tfl") == {"940GZZLUEUS"}
    assert nudges() == [device_id]


def test_a_rebuild_removes_the_configs_of_devices_that_are_gone(app, store, nudges):
    store.set(config_store.key("deadbeef0000"), "{}")  # a device forgotten while Redis was away

    with app.app_context():
        config_store.publish_all()

    assert store.get(config_store.key("deadbeef0000")) is None
    assert nudges() == []  # not a screen the pool draws


def test_the_weather_backfill_rewrites_the_configs_with_the_new_weather_key(app, store, paired_client):
    _, device_id, _ = paired_client
    with app.app_context():
        db.session.get_one(DeviceConfig, device_id).weather_location = "London"
        db.session.commit()
    place = {"id": 2643743, "name": "London", "label": "London", "country_code": "GB", "latitude": 51.5, "longitude": 0}

    with patch("broker.clients.weather.search_places", return_value=[place]):
        assert _cli(app, "weather", "backfill").exit_code == 0

    assert _stored(store, device_id)["weather"]["key"] == "london-gb-2643743"


def test_the_broker_writes_the_store_as_it_starts(app, store, paired_client):
    client, device_id, _ = paired_client
    client.post("/device", data={"stops_order": "940GZZLUEUS"})
    store.flushall()  # Redis restarted, or the broker is the first to write the configs
    hooks = runpy.run_path(str(ROOT / "gunicorn.conf.py"))

    with patch.object(config_store, "start_resync") as start_resync:
        hooks["post_worker_init"](SimpleNamespace(wsgi=app))  # the app gunicorn loaded

    start_resync.assert_called_once_with(app)  # and every minute from then on
    assert _stored(store, device_id)["departures"]["stops"] == [{"source": "tfl", "id": "940GZZLUEUS"}]
    assert store.smembers("v1:sub:tfl") == {"940GZZLUEUS"}


def test_the_broker_starts_even_when_redis_is_down(app, paired_client):
    app.extensions["redis"] = redis.Redis(host="127.0.0.1", port=1, socket_connect_timeout=0.1)
    hooks = runpy.run_path(str(ROOT / "gunicorn.conf.py"))

    with patch.object(config_store, "logger") as logger, patch.object(config_store, "start_resync"):
        hooks["post_worker_init"](SimpleNamespace(wsgi=app))

    logger.warning.assert_called_once()
