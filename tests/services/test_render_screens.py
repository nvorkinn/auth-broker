"""The render pool's list of screens, `v1:render:screens` (broker/services/render_screens.py), as the broker writes it.

The contract's render_screens.json is the hash's shape, which pi-dashboard's render pool reads (keys.RENDER_SCREENS):
both repositories' tests read that file. The rest goes through the real routes and `flask devices` commands, with the
broker's Redis a fakeredis.
"""

import json
import threading
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import fakeredis
import pytest
import redis
from countdown_api import fixtures

from broker.db import db
from broker.models import Device, DeviceConfig, PairingCode, RendererAssignment
from broker.services import assignments, config_store, pairing, pairing_key, render_jobs, render_screens

FIXTURE = fixtures.load("render_screens.json")
SCREENS = FIXTURE["key"]


@pytest.fixture
def store(app):
    """Make the broker's Redis one fakeredis (with Lua, for the render jobs), and return it."""
    client = fakeredis.FakeRedis(decode_responses=True)
    app.extensions["redis"] = client
    return client


def _add_screen(device_id, slug, *, paired=True, config=True, seen=None):
    """Add a device to the database as the broker has it once it's named (needs an app context)."""
    now = datetime.now(UTC)
    device = Device(
        device_id=device_id,
        created_at=now - timedelta(days=30),
        paired_at=now if paired else None,
        owner="user-1" if paired else None,
        display_last_seen_at=seen or now,
    )
    if config:
        device.config = DeviceConfig()
    db.session.add(device)
    db.session.add(RendererAssignment(id=device_id[:6], device=device, slug=slug, created_at=now - timedelta(days=30)))
    db.session.commit()
    return device


def _jobs(store) -> list[str]:
    """Return the device ids queued, oldest first."""
    return list(reversed(store.lrange(render_jobs.JOBS, 0, -1)))


def _headers(secret):
    return {"Authorization": f"Bearer {secret}"}


# --- the hash, against the fixture shared with pi-dashboard --------------------------------------------------------


def test_the_key_is_the_pools():
    # pi-dashboard's keys.RENDER_SCREENS, which render_jobs reads to queue only the screens the pool draws.
    assert render_screens.SCREENS == SCREENS == render_jobs.SCREENS


def test_the_hash_is_the_shape_the_pool_reads(app, store):
    with app.app_context():
        for device_id, slug in FIXTURE["screens"].items():
            _add_screen(device_id, slug)

        render_screens.write()

    # Field the device id, value the slug: what the pool's claim script HGETs, and labels its metrics with.
    assert store.hgetall(SCREENS) == FIXTURE["screens"]
    assert store.ttl(SCREENS) == -1  # no TTL: it's rewritten from the database, and never evicted


# --- which screens ---------------------------------------------------------------------------------------------------


def test_a_screen_with_a_slug_and_a_config_is_listed_and_queued(app, store):
    with app.app_context():
        _add_screen("a1b2c3d4e5f6", "nvorkinn-kitchen")

        assert render_screens.write() == 1

    assert store.hgetall(SCREENS) == {"a1b2c3d4e5f6": "nvorkinn-kitchen"}
    # Drawn now, not at the ticker's next minute.
    assert _jobs(store) == ["a1b2c3d4e5f6"]


def test_only_a_newly_listed_screen_is_queued(app, store):
    with app.app_context():
        _add_screen("a1b2c3d4e5f6", "nvorkinn-kitchen")
        render_screens.write()
        store.delete(render_jobs.JOBS, render_jobs.queued_key("a1b2c3d4e5f6"))  # as the pool's worker would
        _add_screen("0f9e8d7c6b5a", "nvorkinn-hall")

        assert render_screens.write() == 1

    assert _jobs(store) == ["0f9e8d7c6b5a"]


def test_a_screen_without_a_slug_is_not_listed(app, store, paired_client):
    _client, device_id, _ = paired_client
    with app.app_context():
        db.session.delete(db.session.get_one(Device, device_id).assignment)
        db.session.commit()

        render_screens.write()

    assert store.hgetall(SCREENS) == {}


def test_a_screen_without_settings_is_not_listed(app, store):
    with app.app_context():
        _add_screen("a1b2c3d4e5f6", "nvorkinn-kitchen", config=False)

        render_screens.write()

    assert store.hgetall(SCREENS) == {}


def test_an_unpaired_screen_is_listed_only_while_it_has_a_code_to_show(app, store):
    with app.app_context():
        _add_screen("a1b2c3d4e5f6", "nvorkinn-kitchen", paired=False)
        render_screens.write()
        assert store.hgetall(SCREENS) == {}  # nothing for the pool to draw: no config in the store, and no code

        # The pool draws the pairing screen from the code's key (services/pairing_key.py).
        pairing.issue_code("a1b2c3d4e5f6")
        render_screens.write()
        assert store.hgetall(SCREENS) == {"a1b2c3d4e5f6": "nvorkinn-kitchen"}

        # The code lapses: off the list at the next write.
        with patch.object(render_screens, "datetime") as clock:
            clock.now.return_value = datetime.now(UTC) + pairing.CODE_TTL + timedelta(seconds=1)
            render_screens.write()

    assert store.hgetall(SCREENS) == {}


def test_a_new_screen_is_listed_under_its_id_while_it_has_a_code_and_no_name(app, store):
    with app.app_context():
        device = _add_screen("a1b2c3d4e5f6", "unused", paired=False)
        db.session.delete(device.assignment)
        db.session.commit()
        render_screens.write()
        assert store.hgetall(SCREENS) == {}  # no code to show

        pairing.issue_code("a1b2c3d4e5f6")  # lists it itself

    assert store.hgetall(SCREENS) == {"a1b2c3d4e5f6": "a1b2c3d4e5f6"}
    assert "a1b2c3d4e5f6" in _jobs(store)


def test_registering_a_screen_gives_it_a_code_the_pool_draws(app, store, client):
    device_id = client.post(
        "/api/devices/register",
        json={"secret": "display-secret-for-a-new-screen", "width": 480, "height": 800, "format": "mono-1bpp-packed"},
    ).get_json()["device_id"]

    assert store.hgetall(SCREENS) == {device_id: device_id}
    assert json.loads(store.get(pairing_key.key(device_id)))["pairing_code"]
    assert device_id in _jobs(store)


def test_an_unpaired_screens_poll_brings_back_a_lapsed_code(app, store, client):
    secret = "display-secret-for-a-waiting-screen"
    device_id = client.post(
        "/api/devices/register", json={"secret": secret, "width": 480, "height": 800, "format": "mono-1bpp-packed"}
    ).get_json()["device_id"]
    with app.app_context():
        first = pairing.live_code(device_id)
        for code in db.session.scalars(db.select(PairingCode)):
            code.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        db.session.commit()
        render_screens.write()
        assert store.hgetall(SCREENS) == {}  # the code lapsed, and with it the pairing screen

    client.post("/api/frame", headers=_headers(secret))

    with app.app_context():
        assert pairing.live_code(device_id) not in (None, first)
    assert store.hgetall(SCREENS) == {device_id: device_id}


def test_a_paired_screens_poll_issues_no_code(app, store, client, paired_client):
    client, device_id, secret = paired_client

    client.post("/api/frame", headers=_headers(secret))

    with app.app_context():
        assert pairing.live_code(device_id) is None


def test_a_paired_screen_showing_a_code_stays_listed(app, store):
    # Its config isn't in the store while the code is live (renderer_config.for_store), but the code's key is.
    with app.app_context():
        _add_screen("a1b2c3d4e5f6", "nvorkinn-kitchen")
        pairing.issue_code("a1b2c3d4e5f6")
        config_store.publish("a1b2c3d4e5f6")

    assert store.get(config_store.key("a1b2c3d4e5f6")) is None
    assert store.hgetall(SCREENS) == {"a1b2c3d4e5f6": "nvorkinn-kitchen"}


def test_a_quiet_screen_is_left_out_until_it_is_back(app, store):
    with app.app_context():
        now = datetime.now(UTC)
        _add_screen("a1b2c3d4e5f6", "nvorkinn-kitchen", seen=now - assignments.SUSPEND_AFTER - timedelta(minutes=1))
        _add_screen("0f9e8d7c6b5a", "nvorkinn-hall", seen=now - assignments.SUSPEND_AFTER + timedelta(minutes=1))

        render_screens.write()
        assert store.hgetall(SCREENS) == {"0f9e8d7c6b5a": "nvorkinn-hall"}

        # Seen again: listed at the next write.
        db.session.get_one(Device, "a1b2c3d4e5f6").display_last_seen_at = now
        db.session.commit()
        render_screens.write()

    assert store.hgetall(SCREENS) == FIXTURE["screens"]


def test_screens_nobody_has_are_taken_out_and_the_rest_left(app, store):
    # A screen the broker doesn't have, and one under another slug.
    store.hset(SCREENS, mapping={"gone00000000": "someone-old", "a1b2c3d4e5f6": "nvorkinn-old"})
    with app.app_context():
        _add_screen("a1b2c3d4e5f6", "nvorkinn-kitchen")

        assert render_screens.write() == 0  # listed already, under another slug: no job

    assert store.hgetall(SCREENS) == {"a1b2c3d4e5f6": "nvorkinn-kitchen"}
    assert _jobs(store) == []


def test_nothing_is_written_when_the_hash_is_right(app, store):
    with app.app_context():
        _add_screen("a1b2c3d4e5f6", "nvorkinn-kitchen")
        render_screens.write()

        with patch.object(store, "pipeline") as pipeline:
            assert render_screens.write() == 0

    pipeline.assert_not_called()


def test_redis_down_is_logged_by_type_and_not_raised(app):
    server = fakeredis.FakeServer()
    server.connected = False  # every command raises ConnectionError
    app.extensions["redis"] = fakeredis.FakeRedis(server=server, decode_responses=True)
    with app.app_context():
        _add_screen("a1b2c3d4e5f6", "nvorkinn-kitchen")

        # Patched rather than caplog: other tests (alembic's logging set-up) can leave loggers disabled.
        with patch.object(render_screens, "logger") as logger:
            assert render_screens.write() == 0

    logger.warning.assert_called_once()
    assert "ConnectionError" in logger.warning.call_args.args
    assert "a1b2c3d4e5f6" not in repr(logger.warning.call_args)
    assert "nvorkinn" not in repr(logger.warning.call_args)


# --- kept in step with the database ----------------------------------------------------------------------------------


def test_pairing_names_the_screen_and_lists_and_draws_it_at_once(app, store, client):
    display = "display-secret-for-naming-a-screen"
    device_id = client.post(
        "/api/devices/register", json={"secret": display, "width": 480, "height": 800, "format": "mono-1bpp-packed"}
    ).get_json()["device_id"]
    with app.app_context():
        token = db.session.get_one(Device, device_id).pairing_token
    # Not paired yet, but it shows a code to pair with by hand: listed under its id, as naming it will make it.
    assert store.hgetall(SCREENS) == {device_id: device_id}

    assert client.post("/pair/token", json={"token": token}).status_code == 200

    # Under its stable name, its own id (assignments.auto_name): what its owner calls it can change, this can't.
    assert store.hgetall(SCREENS) == {device_id: device_id}
    assert device_id in _jobs(store)


def test_renaming_a_screen_leaves_its_entry_alone(store, client, paired_client):
    _client, _device_id, _ = paired_client
    before = store.hgetall(SCREENS)

    client.post("/device", data={"screen_name": "Kitchen"})

    assert store.hgetall(SCREENS) == before


def test_unpairing_keeps_a_screen_listed_only_while_its_code_is_live(app, store, paired_client, cli):
    _client, device_id, _ = paired_client
    slug = store.hget(SCREENS, device_id)
    assert slug

    assert cli("unpair", device_id).exit_code == 0

    # It shows the code unpairing issued it (its pairing key), so the pool still draws it.
    assert store.hgetall(SCREENS) == {device_id: slug}
    with app.app_context(), patch.object(render_screens, "datetime") as clock:
        clock.now.return_value = datetime.now(UTC) + pairing.CODE_TTL + timedelta(seconds=1)
        render_screens.write()
    assert store.hgetall(SCREENS) == {}


def test_forgetting_a_screen_takes_it_out(store, paired_client, cli):
    _client, device_id, _ = paired_client

    assert cli("forget", device_id).exit_code == 0

    assert store.hgetall(SCREENS) == {}


def test_a_config_save_puts_back_a_hash_redis_lost(store, paired_client):
    client, device_id, _ = paired_client
    slug = store.hget(SCREENS, device_id)
    store.flushall()  # Redis restarted

    client.post("/device", data={"style": "card"})

    assert store.hgetall(SCREENS) == {device_id: slug}
    assert device_id in _jobs(store)


def test_the_broker_puts_the_hash_back_as_it_starts(app, store, paired_client):
    _client, device_id, _ = paired_client
    slug = store.hget(SCREENS, device_id)
    store.flushall()

    # What gunicorn.conf.py's post_worker_init runs (and `flask config publish`).
    with app.app_context():
        from broker.cli import publish_store  # noqa: PLC0415

        publish_store()

    assert store.hgetall(SCREENS) == {device_id: slug}


# --- every minute ----------------------------------------------------------------------------------------------------


def test_the_resync_rewrites_the_configs_and_the_screens_until_stopped(app, store, paired_client):
    _client, device_id, _ = paired_client
    slug = store.hget(SCREENS, device_id)
    config = store.get(config_store.key(device_id))
    store.flushall()  # Redis restarted while the broker ran
    stop = threading.Event()
    rounds = []
    publish_all = config_store.publish_all

    def publish_and_count():
        publish_all()
        rounds.append(store.hgetall(SCREENS))
        if len(rounds) == 2:
            stop.set()

    with patch.object(config_store, "publish_all", side_effect=publish_and_count):
        config_store.resync_forever(app, stop, interval_s=0.01)

    assert rounds == [{device_id: slug}, {device_id: slug}]
    assert store.get(config_store.key(device_id)) == config


def test_the_resync_carries_on_after_a_round_fails(app):
    stop = threading.Event()
    calls = []

    def fail_then_stop():
        calls.append(1)
        if len(calls) == 2:
            stop.set()
        msg = "unexpected"
        raise redis.exceptions.ResponseError(msg)

    with (
        patch.object(config_store, "publish_all", side_effect=fail_then_stop),
        patch.object(config_store, "logger") as logger,
    ):
        config_store.resync_forever(app, stop, interval_s=0.01)

    assert len(calls) == 2
    assert logger.exception.call_count == 2


def test_the_resync_runs_in_a_daemon_thread(app):
    with patch.object(config_store, "resync_forever") as resync:
        thread = config_store.start_resync(app)
        thread.join(1)

    assert thread.daemon  # so it never holds up a worker that's stopping
    assert resync.call_args.args[0] is app
