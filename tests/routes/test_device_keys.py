"""A device's data in the store goes when its accounts do, or when it changes hands (broker/services/device_keys.py).

Through the real routes and `flask devices` commands, with the broker's Redis a fakeredis: every per-device key the
providers write is put there as they would, and each test checks which are left once the database has changed.
"""

from unittest.mock import patch

import fakeredis
import pytest

from broker import spotify_provider
from broker.db import db
from broker.developer_provider import pace_key, stored_key
from broker.models import Device, GitHubToken, SpotifyToken
from broker.providers.glow import energy_key
from broker.services import config_store, device_keys, frame_record

GLOWMARKT = {"interval": "15", "glowmarkt_username": "someone@example.com", "glowmarkt_password": "hunter2"}


@pytest.fixture
def store(app):
    """Make the broker's Redis (both its clients) one fakeredis, and return the client that decodes."""
    server = fakeredis.FakeServer()
    app.extensions["redis"] = fakeredis.FakeRedis(server=server, decode_responses=True)
    app.extensions["redis_bytes"] = fakeredis.FakeRedis(server=server)
    return app.extensions["redis"]


def _keys(device_id: str) -> dict[str, str]:
    """Return every key the providers write for the device, by what it holds, as the providers name them."""
    return {
        "energy": energy_key(device_id),
        "player": spotify_provider.player_key(device_id),
        "top": spotify_provider.top_key(device_id),
        "developer": stored_key(device_id),
        "pace": pace_key(device_id),
    }


def _provide(store, device_id: str) -> None:
    """Write every key the providers keep for the device, as they would, with their TTLs."""
    for key in _keys(device_id).values():
        store.set(key, '{"fetched_at": "2026-10-18T07:00:00+00:00"}', ex=3600)


def _left(store, device_id: str) -> set[str]:
    """Return what's still in the store for the device, by what it holds."""
    return {name for name, key in _keys(device_id).items() if store.exists(key)}


EVERYTHING = {"energy", "player", "top", "developer", "pace"}


def test_the_keys_are_the_ones_the_providers_write():
    assert device_keys.energy("d") == [energy_key("d")]
    assert device_keys.music("d") == [spotify_provider.player_key("d"), spotify_provider.top_key("d")]
    assert device_keys.developer("d") == [stored_key("d"), pace_key("d")]


# --- Changing hands: unpaired, re-paired, given an owner, forgotten -------------------------------------------------


def test_forgetting_a_device_deletes_all_its_data_and_no_one_elses(store, paired_client, register_device, cli):
    _client, device_id, _ = paired_client
    other_id, _ = register_device("another-very-long-device-secret")
    _provide(store, device_id)
    _provide(store, other_id)

    assert cli("forget", device_id).exit_code == 0

    assert _left(store, device_id) == set()
    assert _left(store, other_id) == EVERYTHING
    assert not store.exists(config_store.key(device_id))  # config_store removes its own


@pytest.mark.parametrize("args", [(), ("--config",)], ids=["unpair", "unpair --config"])
def test_unpairing_deletes_all_its_data(store, paired_client, cli, args):
    _client, device_id, _ = paired_client
    _provide(store, device_id)
    assert store.exists(config_store.key(device_id))

    assert cli("unpair", device_id, *args).exit_code == 0

    assert _left(store, device_id) == set()
    assert not store.exists(config_store.key(device_id))  # an unpaired device has no config in the store


def test_unpairing_alone_still_keeps_the_frame_record(store, paired_client, cli):
    _client, device_id, _ = paired_client
    store.hset(frame_record.key(device_id), mapping={"etag": "0" * 16})

    assert cli("unpair", device_id).exit_code == 0

    assert store.exists(frame_record.key(device_id))


def test_a_code_that_hands_the_device_to_someone_else_deletes_the_last_owners_data(app, store, paired_client, cli):
    _client, device_id, _ = paired_client
    code = cli("code", device_id).output.split(":")[0]
    _provide(store, device_id)
    new_owner = app.test_client()
    new_owner.environ_base["HTTP_X_AUTHENTIK_UID"] = "someone-else"

    assert new_owner.post("/pair", data={"code": code}).status_code == 302

    assert _left(store, device_id) == set()
    assert store.exists(config_store.key(device_id))  # paired again: its config is back in the store


def test_pairing_again_as_the_same_owner_keeps_its_data(store, paired_client, cli):
    client, device_id, _secret = paired_client
    _provide(store, device_id)
    code = cli("code", device_id).output.split(":")[0]

    assert client.post("/pair", data={"code": code}).status_code == 302

    assert _left(store, device_id) == EVERYTHING


def test_pairing_by_token_after_an_unpair_starts_with_nothing_from_before(app, store, client, cli):
    secret = "display-a-very-long-device-secret-value"
    registered = client.post("/api/devices/register", json={"secret": secret, "width": 480, "height": 800})
    device_id = registered.get_json()["device_id"]
    with app.app_context():
        token = db.session.get(Device, device_id).pairing_token
    _provide(store, device_id)  # as if from a time it was paired before

    assert client.post("/pair/token", json={"token": token}).status_code == 200

    assert _left(store, device_id) == set()


@pytest.mark.parametrize(("user", "left"), [("someone-else", set()), ("user-1", EVERYTHING)])
def test_giving_the_device_an_owner_deletes_its_data_unless_it_was_theirs(store, paired_client, cli, user, left):
    _client, device_id, _ = paired_client  # paired by user-1
    _provide(store, device_id)

    assert cli("owner", device_id, user).exit_code == 0

    assert _left(store, device_id) == left


# --- An account removed or replaced on the settings page ------------------------------------------------------------


def test_clearing_the_glowmarkt_account_deletes_the_energy_readings_only(store, paired_client):
    client, device_id, _ = paired_client
    client.post("/device", data=GLOWMARKT)
    _provide(store, device_id)

    assert client.post("/device", data={"interval": "15", "glowmarkt_username": ""}).status_code == 302

    assert _left(store, device_id) == EVERYTHING - {"energy"}


@pytest.mark.parametrize(
    "change",
    [{"glowmarkt_username": "someone-else@example.com"}, {"glowmarkt_password": "a new password"}],
    ids=["username", "password"],
)
def test_another_glowmarkt_account_deletes_the_last_ones_readings(store, paired_client, change):
    client, device_id, _ = paired_client
    client.post("/device", data=GLOWMARKT)
    _provide(store, device_id)

    client.post("/device", data=GLOWMARKT | change)

    assert "energy" not in _left(store, device_id)


@pytest.mark.parametrize("password", ["", "hunter2"], ids=["blank, so kept", "the same again"])
def test_saving_the_same_glowmarkt_account_keeps_its_readings(store, paired_client, password):
    """The provider has nothing new to fetch for it, so deleting them would blank the panel until its next tick."""
    client, device_id, _ = paired_client
    client.post("/device", data=GLOWMARKT)
    _provide(store, device_id)

    client.post("/device", data=GLOWMARKT | {"glowmarkt_password": password})

    assert _left(store, device_id) == EVERYTHING


def test_another_github_token_deletes_the_developer_panel_but_the_same_one_keeps_it(store, paired_client):
    client, device_id, _ = paired_client
    client.post("/device", data={"interval": "15", "github_token": "github_pat_one"})
    _provide(store, device_id)

    client.post("/device", data={"interval": "15", "github_token": "github_pat_one"})
    assert _left(store, device_id) == EVERYTHING

    client.post("/device", data={"interval": "15", "github_token": "github_pat_two"})
    assert _left(store, device_id) == EVERYTHING - {"developer", "pace"}


def test_disconnecting_github_deletes_the_developer_panel_only(app, store, paired_client):
    client, device_id, _ = paired_client
    client.post("/device", data={"interval": "15", "github_token": "github_pat_one"})
    _provide(store, device_id)

    assert client.post("/device/github/disconnect").status_code == 302

    assert _left(store, device_id) == EVERYTHING - {"developer", "pace"}
    with app.app_context():
        assert db.session.get(GitHubToken, device_id) is None


def test_disconnecting_spotify_deletes_the_music_only(app, store, paired_client):
    client, device_id, _ = paired_client
    with app.app_context():
        db.session.add(SpotifyToken(device_id=device_id, refresh_token="refresh"))
        db.session.commit()
    _provide(store, device_id)

    assert client.post("/device/spotify/disconnect").status_code == 302

    assert _left(store, device_id) == EVERYTHING - {"player", "top"}


def test_disconnecting_apple_music_deletes_the_music_only(store, paired_client, musickit):
    client, device_id, _ = paired_client
    client.post("/device/apple-music/connect", data={"music_user_token": "mut"})
    _provide(store, device_id)

    assert client.post("/device/apple-music/disconnect").status_code == 302

    assert _left(store, device_id) == EVERYTHING - {"player", "top"}


# --- Redis down: the change is saved all the same --------------------------------------------------------------------


@pytest.fixture
def redis_down(app):
    server = fakeredis.FakeServer()
    server.connected = False
    app.extensions["redis"] = fakeredis.FakeRedis(server=server, decode_responses=True)
    app.extensions["redis_bytes"] = fakeredis.FakeRedis(server=server)


@pytest.mark.usefixtures("redis_down")
def test_a_redis_that_cant_be_asked_still_lets_the_device_be_forgotten(app, paired_client, cli):
    _client, device_id, _ = paired_client

    with patch.object(device_keys, "logger") as logged:
        assert cli("forget", device_id).exit_code == 0

    with app.app_context():
        assert db.session.get(Device, device_id) is None
    logged.warning.assert_called_once_with(
        "Couldn't delete a device's data from Redis (%s); it lapses by its TTL", "ConnectionError"
    )


@pytest.mark.usefixtures("redis_down")
def test_a_redis_that_cant_be_asked_still_lets_the_glowmarkt_account_be_cleared(app, paired_client):
    client, device_id, _ = paired_client
    client.post("/device", data=GLOWMARKT)

    with patch.object(device_keys, "logger") as logged:
        response = client.post("/device", data={"interval": "15", "glowmarkt_username": ""})

    assert response.status_code == 302
    with app.app_context():
        assert db.session.get(Device, device_id).glowmarkt_credentials.sign_in() is None
    assert logged.warning.call_args.args == (
        "Couldn't delete a device's data from Redis (%s); it lapses by its TTL",
        "ConnectionError",
    )
