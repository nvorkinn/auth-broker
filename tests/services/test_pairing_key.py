"""A device's live pairing code in the store, `v1:pairing:<device id>` (broker/services/pairing_key.py).

Through the real routes and `flask devices` commands, with the broker's Redis a fakeredis (TTLs and all). The
contract's pairing_key.json is the stored shape, which pi-dashboard's render pool reads: both repositories' tests read
that file.
"""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import fakeredis
import pytest
import redis
from countdown_api import fixtures

from broker.db import db
from broker.models import Device, DeviceConfig
from broker.services import config_store, pairing, pairing_key

ROOT = Path(__file__).parent.parent.parent
FIXTURE = fixtures.load("pairing_key.json")


@pytest.fixture
def store(app):
    """Make the broker's Redis (both its clients) one fakeredis, and return the client that decodes."""
    server = fakeredis.FakeServer()
    app.extensions["redis"] = fakeredis.FakeRedis(server=server, decode_responses=True)
    app.extensions["redis_bytes"] = fakeredis.FakeRedis(server=server)
    return app.extensions["redis"]


def _ask_for_code(app, device_id) -> str:
    """Issue the device a code, as `flask devices code` does; return it."""
    with app.app_context():
        code = pairing.issue_code(device_id)
        config_store.publish(device_id)
    return code


def _stored(store, device_id):
    value = store.get(pairing_key.key(device_id))
    return None if value is None else json.loads(value)


# --- the stored value ---------------------------------------------------------------------------------------------


def test_the_key_is_the_one_the_renderer_reads():
    assert pairing_key.key(FIXTURE["device_id"]) == FIXTURE["key"]


def test_the_stored_code_is_the_shape_the_renderer_reads(app, store):
    device_id = FIXTURE["device_id"]
    with app.app_context():
        now = datetime.now(UTC)
        device = Device(device_id=device_id, created_at=now, paired_at=now, owner="user-1")
        device.config = DeviceConfig(**FIXTURE["settings"])
        db.session.add(device)
        db.session.commit()

        with patch("broker.services.pairing.secrets.choice", side_effect=iter(FIXTURE["value"]["pairing_code"])):
            code = pairing.issue_code(device_id)

    assert code == FIXTURE["value"]["pairing_code"]
    assert _stored(store, device_id) == FIXTURE["value"]
    # It lapses when the code does.
    assert FIXTURE["ttl_s"] * 1000 - 5000 < store.pttl(FIXTURE["key"]) <= FIXTURE["ttl_s"] * 1000
    assert timedelta(seconds=FIXTURE["ttl_s"]) == pairing.CODE_TTL


def test_the_stored_code_is_the_config_with_the_code_in_it(app, store, register_device, config_of):
    device_id, _secret = register_device()

    code = _ask_for_code(app, device_id)

    assert _stored(store, device_id) == {**config_of(device_id), "pairing_code": code}
    # And the config's own copy still never has it: there's none while a code is live.
    assert store.get(config_store.key(device_id)) is None


def test_a_device_with_no_settings_has_no_key(app, store):
    with app.app_context():
        db.session.add(Device(device_id="no-config", created_at=datetime.now(UTC)))
        db.session.commit()
        pairing.issue_code("no-config")

    assert _stored(store, "no-config") is None


# --- replaced, used, paired, unpaired, forgotten --------------------------------------------------------------------


def test_a_new_code_replaces_the_last_ones_key(app, store, paired_client):
    _client, device_id, _secret = paired_client
    first = _ask_for_code(app, device_id)

    second = _ask_for_code(app, device_id)

    assert second != first
    assert _stored(store, device_id)["pairing_code"] == second


def test_using_the_code_deletes_its_key(app, store, paired_client):
    client, device_id, _secret = paired_client
    code = _ask_for_code(app, device_id)
    assert _stored(store, device_id) is not None

    client.post("/pair", data={"code": code})

    assert _stored(store, device_id) is None
    assert store.get(config_store.key(device_id)) is not None  # its config back in the store, without the code


def test_a_code_used_by_a_new_owner_deletes_its_key(app, store, client, register_device):
    device_id, _secret = register_device()
    code = _ask_for_code(app, device_id)

    client.post("/pair", data={"code": code})

    assert _stored(store, device_id) is None


def test_pairing_by_token_drops_the_code_and_its_key(app, store, client):
    display_secret = "display-secret-for-a-token-pairing"
    device_id = client.post(
        "/api/devices/register",
        json={"secret": display_secret, "width": 800, "height": 480, "format": "mono-1bpp-packed"},
    )
    device_id = device_id.get_json()["device_id"]
    with app.app_context():
        pairing.issue_code(device_id)  # as `flask devices code` would have, before its owner's browser paired it
        token = db.session.get_one(Device, device_id).pairing_token

    assert client.post("/pair/token", json={"token": token}).status_code == 200

    assert _stored(store, device_id) is None
    with app.app_context():
        assert pairing.live_code(device_id) is None
    # So the store has its config again, which the pool draws from.
    assert json.loads(store.get(config_store.key(device_id)))["pairing_code"] is None


def test_pairing_by_token_again_as_the_same_owner_drops_the_code(app, store, client):
    display_secret = "display-secret-for-a-token-pairing"
    device_id = client.post(
        "/api/devices/register",
        json={"secret": display_secret, "width": 800, "height": 480, "format": "mono-1bpp-packed"},
    )
    device_id = device_id.get_json()["device_id"]
    with app.app_context():
        token = db.session.get_one(Device, device_id).pairing_token
    client.post("/pair/token", json={"token": token})
    with app.app_context():
        pairing.issue_code(device_id)

    client.post("/pair/token", json={"token": token})

    assert _stored(store, device_id) is None


@pytest.mark.parametrize("command", [["unpair"], ["unpair", "--config"], ["forget"], ["owner", "user-2"]])
def test_a_device_changing_hands_loses_its_codes_key(app, store, paired_client, cli, command):
    _client, device_id, _secret = paired_client
    code = _ask_for_code(app, device_id)

    assert cli(command[0], device_id, *command[1:]).exit_code == 0

    # Unpairing issues a new code to show; the old one's key is gone either way.
    stored = _stored(store, device_id)
    assert stored is None or stored["pairing_code"] != code


def test_an_owner_set_again_to_the_same_user_drops_the_code(app, store, paired_client, cli):
    _client, device_id, _secret = paired_client
    _ask_for_code(app, device_id)

    assert cli("owner", device_id, "user-1").exit_code == 0

    assert _stored(store, device_id) is None
    with app.app_context():
        assert pairing.live_code(device_id) is None


def test_the_code_command_stores_the_code_it_prints(store, paired_client, cli):
    _client, device_id, _ = paired_client

    result = cli("code", device_id)

    assert result.exit_code == 0
    assert result.output.startswith(_stored(store, device_id)["pairing_code"])


def test_only_this_devices_key_goes(app, store, paired_client, register_device, client):
    _client, device_id, _secret = paired_client
    other_id, _other_secret = register_device("another-very-long-device-secret")
    other_code = _ask_for_code(app, other_id)
    code = _ask_for_code(app, device_id)

    client.post("/pair", data={"code": code})

    assert _stored(store, other_id)["pairing_code"] == other_code


# --- Redis down ---------------------------------------------------------------------------------------------------


class _DownRedis:
    """A Redis that can't be asked, whose errors quote what was sent, as a real failed command's can."""

    def __getattr__(self, name):
        def fail(*args, **_kwargs):
            msg = f"can't {name} {args!r}"
            raise redis.ConnectionError(msg)

        return fail

    def register_script(self, _script):
        # Redis-py's own makes no call: the script is sent when it runs, which fails like any other command.
        return self.__getattr__("evalsha")


def test_redis_down_never_stops_a_code_being_issued_or_used(app, client, register_device, cli):
    device_id, _secret = register_device()
    app.extensions["redis"] = _DownRedis()

    with (
        patch.object(pairing_key, "logger") as logger,
        patch("broker.services.device_keys.logger") as keys_logger,
    ):
        issued = cli("code", device_id)
        code = issued.output.split(":")[0]
        used = client.post("/pair", data={"code": code})

    assert issued.exit_code == 0
    assert used.status_code == 302
    assert logger.warning.call_count == 1  # the write
    assert keys_logger.warning.call_count == 1  # and the delete, with the last owner's data (device_keys.forget)
    for call in [*logger.warning.call_args_list, *keys_logger.warning.call_args_list]:
        assert call.args[1:] == ("ConnectionError",)
        assert code not in str(call)


def test_no_code_is_ever_logged(app, store, paired_client, cli):
    client, device_id, _secret = paired_client
    with (
        patch.object(pairing_key, "logger") as logger,
        patch("broker.services.device_keys.logger") as keys_logger,
    ):
        code = _ask_for_code(app, device_id)
        client.post("/pair", data={"code": code})
        cli("code", device_id)
        cli("forget", device_id)

    for call in [*logger.mock_calls, *keys_logger.mock_calls]:
        assert code not in str(call)
