"""Jobs for the render pool (broker/services/render_jobs.py): the pool's own enqueue script, and who queues a job.

tests/fixtures/render_enqueue.json is a byte-for-byte copy of pi-dashboard's script (countdown_core.store.render_queue
`_ENQUEUE`), so the broker queues jobs exactly as the pool's ticker does; tests/fixtures/render_queue.json pins its
SHA1, its keys and its TTL, and pi-dashboard's tests check its own script against that same file.

The script runs on fakeredis's Lua and, where marked, on a real Redis too: a redis-server of the tests' own, on a free
port, when one is installed (CI installs it); those are skipped otherwise. Who queues: the developer provider
(tests/test_developer_provider.py), and here a config save, a code issued and a code spent, through the real routes
and `flask devices` commands with the broker's Redis a fakeredis.
"""

import hashlib
import json
import shutil
import socket
import subprocess
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import fakeredis
import pytest
import redis

from broker.db import db
from broker.models import Device
from broker.services import assignments, config_store, pairing, pairing_key, render_jobs, render_screens
from broker.services.render_jobs import JOBS, QUEUED_TTL_MS, SCREENS, enqueue, queued_key

FIXTURES = Path(__file__).parent.parent / "fixtures"
ENQUEUE_COPY = json.loads((FIXTURES / "render_enqueue.json").read_text())
# Shared with pi-dashboard (countdown/tests/fixtures/render_queue.json), whose test_render_queue.py checks it too.
SHARED = json.loads((FIXTURES / "render_queue.json").read_text())
DEVICE = SHARED["device_id"]
OTHER = "0f9e8d7c6b5a"


@pytest.fixture
def pool():
    """Return a pool where the pool renders screens a, b and a1b2c3d4e5f6 (as the orchestrator lists them)."""
    pool = fakeredis.FakeRedis(decode_responses=True)
    pool.hset(SCREENS, mapping={"a": "screen-a", "b": "screen-b", "a1b2c3d4e5f6": "screen-c"})
    return pool


def test_the_script_is_pi_dashboards_byte_for_byte():
    assert ENQUEUE_COPY["script"] == render_jobs._ENQUEUE


def test_the_keys_are_the_pools():
    # pi-dashboard's keys.RENDER_JOBS and keys.render_queued.
    assert JOBS == "v1:render:jobs"
    assert SCREENS == "v1:render:screens"
    assert queued_key("a1b2c3d4e5f6") == "v1:render:queued:a1b2c3d4e5f6"
    assert QUEUED_TTL_MS == 60_000


def test_queues_each_screen_with_its_flag(pool):
    before_ms = int(time.time() * 1000)

    assert enqueue(pool, ["a", "b"]) == 2

    # LPUSH, as the pool's: the worker BRPOPs, so the first queued is the first drawn.
    assert pool.lrange(JOBS, 0, -1) == ["b", "a"]
    for device_id in ("a", "b"):
        # The flag holds when it was queued, by Redis's clock, in Unix ms (the pool's measure of a job's wait).
        assert int(pool.get(queued_key(device_id))) >= before_ms - 1000
        assert 0 < pool.pttl(queued_key(device_id)) <= QUEUED_TTL_MS


def test_a_screen_already_queued_is_queued_once(pool):
    enqueue(pool, ["a"])

    assert enqueue(pool, ["a", "b"]) == 1

    assert pool.lrange(JOBS, 0, -1) == ["b", "a"]


def test_a_screen_is_queued_again_once_its_job_is_taken(pool):
    enqueue(pool, ["a"])
    # What the pool's worker does: pop the job, and claim it (which deletes the flag).
    pool.rpop(JOBS)
    pool.delete(queued_key("a"))

    assert enqueue(pool, ["a"]) == 1
    assert pool.lrange(JOBS, 0, -1) == ["a"]


def test_only_the_screens_the_pool_renders_are_queued(pool):
    assert enqueue(pool, ["a", "own-renderer"]) == 1

    assert pool.lrange(JOBS, 0, -1) == ["a"]
    assert pool.exists(queued_key("own-renderer")) == 0


def test_with_no_pool_nothing_is_written():
    # Before the pool is deployed nothing would take jobs off the list, which has no TTL.
    pool = fakeredis.FakeRedis(decode_responses=True)

    assert enqueue(pool, ["a"]) == 0

    assert pool.keys("*") == []


def test_no_screens_asks_nothing(pool):
    with patch.object(pool, "pipeline") as pipeline:
        assert enqueue(pool, []) == 0
    pipeline.assert_not_called()


def test_redis_down_is_logged_by_type_and_not_raised():
    server = fakeredis.FakeServer()
    server.connected = False  # every command raises ConnectionError
    pool = fakeredis.FakeRedis(server=server, decode_responses=True)

    # Patched rather than caplog: other tests (alembic's logging set-up) can leave loggers disabled.
    with patch.object(render_jobs, "logger") as logger:
        assert enqueue(pool, ["a1b2c3d4e5f6"]) == 0

    logger.warning.assert_called_once()
    assert "ConnectionError" in logger.warning.call_args.args
    assert "a1b2c3d4e5f6" not in repr(logger.warning.call_args)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="module")
def redis_server():
    """Start a redis-server of the tests' own, with a password and no persistence; skip if there's none installed."""
    binary = shutil.which("redis-server")
    if binary is None:
        pytest.skip("no redis-server installed")
    port = _free_port()
    process = subprocess.Popen(  # noqa: S603 -- a fixed binary and arguments
        [binary, "--port", str(port), "--bind", "127.0.0.1", "--save", "", "--appendonly", "no", "--requirepass", "pw"],
        stdout=subprocess.DEVNULL,
    )
    client = redis.Redis(port=port, password="pw", decode_responses=True)
    for _ in range(100):
        try:
            client.ping()
            break
        except redis.ConnectionError:
            time.sleep(0.05)
    yield port
    process.terminate()
    process.wait(5)


@pytest.fixture(params=["fakeredis", "redis-server"])
def redis_client(request):
    """Return a client decoding values as text, as the broker's does, on fakeredis or on the real Redis (emptied)."""
    if request.param == "fakeredis":
        client = fakeredis.FakeRedis(decode_responses=True)
    else:
        client = redis.Redis(port=request.getfixturevalue("redis_server"), password="pw", decode_responses=True)
        client.flushall()
    _pool(client, DEVICE, OTHER)
    return client


@pytest.fixture
def store(app):
    """Make the broker's Redis one fakeredis with Lua, and return it."""
    client = fakeredis.FakeRedis(decode_responses=True)
    app.extensions["redis"] = client
    return client


def _pool(store, *device_ids):
    """Put the screens in the pool, as the orchestrator does."""
    store.hset(render_jobs.SCREENS, mapping=dict.fromkeys(device_ids, "nvorkinn-test"))


def _jobs(store) -> list[str]:
    """Return the device ids queued, oldest first (the workers BRPOP the right-hand end)."""
    return list(reversed(store.lrange(SHARED["jobs_key"], 0, -1)))


def _take_jobs(store) -> list[str]:
    """Return the jobs queued, and empty the queue as the workers would (their claim takes the flag too)."""
    found = _jobs(store)
    store.delete(SHARED["jobs_key"], *(render_jobs.queued_key(device_id) for device_id in found))
    return found


def _headers(secret):
    return {"Authorization": f"Bearer {secret}"}


# --- on a real Redis too, against the fixture shared with pi-dashboard ---------------------------------------------


def test_the_script_is_the_pools():
    assert hashlib.sha1(render_jobs._ENQUEUE.encode(), usedforsecurity=False).hexdigest() == SHARED["enqueue_sha1"]


def test_the_keys_and_ttl_are_the_pools():
    assert SHARED["jobs_key"] == render_jobs.JOBS
    assert render_jobs.queued_key(DEVICE) == SHARED["queued_key"]
    assert SHARED["queued_ttl_ms"] == render_jobs.QUEUED_TTL_MS


def test_redis_knows_the_script_by_the_fixtures_sha1(redis_client):
    render_jobs.enqueue(redis_client, [DEVICE])

    assert redis_client.script_exists(SHARED["enqueue_sha1"]) == [True]


def test_enqueue_queues_the_screen_and_flags_it(redis_client):
    before_ms = int(redis_client.time()[0]) * 1000

    assert render_jobs.enqueue(redis_client, [DEVICE]) == 1

    assert redis_client.lrange(SHARED["jobs_key"], 0, -1) == [DEVICE]
    # The flag holds when it was queued, by Redis's clock, in Unix ms; and lapses within the minute.
    assert before_ms <= int(redis_client.get(SHARED["queued_key"])) <= before_ms + 2000
    assert 0 < redis_client.pttl(SHARED["queued_key"]) <= SHARED["queued_ttl_ms"]


def test_a_screen_is_queued_at_most_once(redis_client):
    assert render_jobs.enqueue(redis_client, [DEVICE, OTHER, DEVICE]) == 2
    assert render_jobs.enqueue(redis_client, [DEVICE]) == 0

    assert redis_client.lrange(SHARED["jobs_key"], 0, -1) == [OTHER, DEVICE]


def test_a_screen_is_queued_again_once_its_job_is_claimed(redis_client):
    render_jobs.enqueue(redis_client, [DEVICE])
    redis_client.rpop(SHARED["jobs_key"])
    redis_client.delete(SHARED["queued_key"])  # as the pool's claim does

    assert render_jobs.enqueue(redis_client, [DEVICE]) == 1


def test_a_screen_the_pool_doesnt_render_is_not_queued(redis_client):
    # Nothing takes jobs off the list until a pool runs, and it has no TTL: so nothing is written for such a screen.
    assert render_jobs.enqueue(redis_client, ["not-pooled", DEVICE]) == 1

    assert redis_client.lrange(SHARED["jobs_key"], 0, -1) == [DEVICE]
    assert redis_client.exists(render_jobs.queued_key("not-pooled")) == 0


# --- a config save --------------------------------------------------------------------------------------------------


def test_a_config_save_queues_the_screen_and_still_nudges_it(store, paired_client):
    client, device_id, _ = paired_client
    _pool(store, device_id)
    _take_jobs(store)
    pubsub = store.pubsub()
    pubsub.subscribe(config_store.CHANGED)
    pubsub.get_message(timeout=1)

    client.post("/device", data={"style": "card"})

    assert _jobs(store) == [device_id]
    assert pubsub.get_message(timeout=1)["data"] == device_id  # today's per-screen renderers still need it
    # After the write: the job's render reads the new config.
    assert json.loads(store.get(config_store.key(device_id)))["style"] == "card"


def test_a_save_that_changes_no_copy_still_queues_the_screen(store, paired_client):
    client, device_id, _ = paired_client
    _pool(store, device_id)
    client.post("/device", data={"style": "card"})
    _take_jobs(store)

    client.post("/device", data={"style": "card"})

    assert _jobs(store) == [device_id]


def test_a_rebuild_queues_only_the_screens_whose_copy_changed(app, store, paired_client, register_device):
    _client, device_id, _ = paired_client
    other_id, _ = register_device("another-very-long-device-secret")
    _pool(store, device_id, other_id)
    with app.app_context():
        db.session.get(Device, other_id).paired_at = datetime.now(UTC)
        db.session.commit()
        config_store.publish_all()
    assert other_id in _take_jobs(store)

    with app.app_context():
        store.delete(config_store.key(device_id))  # a copy the store lost
        config_store.publish_all()

    assert _jobs(store) == [device_id]


def test_a_save_of_a_screen_the_pool_doesnt_draw_queues_nothing(app, store, paired_client):
    client, device_id, _ = paired_client
    # A screen unseen for a day isn't in the pool's list (services/render_screens.py).
    long_ago = datetime.now(UTC) - assignments.SUSPEND_AFTER - timedelta(hours=1)
    with app.app_context():
        device = db.session.get(Device, device_id)
        device.display_last_seen_at = device.created_at = device.assignment.created_at = long_ago
        db.session.commit()
        render_screens.write()
    assert store.hgetall(render_jobs.SCREENS) == {}
    _take_jobs(store)

    client.post("/device", data={"style": "card"})

    assert json.loads(store.get(config_store.key(device_id)))["style"] == "card"
    assert _jobs(store) == []


def test_a_rejected_save_queues_nothing(store, paired_client):
    client, device_id, _ = paired_client
    _pool(store, device_id)
    _take_jobs(store)

    assert client.post("/device", data={"postcode": "not a postcode", "style": "card"}).status_code == 400

    assert _jobs(store) == []


# --- a pairing code -------------------------------------------------------------------------------------------------


def test_issuing_a_code_queues_the_screen_after_writing_its_key(app, store, paired_client):
    client, device_id, secret = paired_client
    _pool(store, device_id)
    _take_jobs(store)
    seen = []
    enqueue_ = render_jobs.enqueue

    def enqueue_and_look(client, device_ids):
        seen.append(store.get(pairing_key.key(device_id)) is not None)
        return enqueue_(client, device_ids)

    with patch.object(render_jobs, "enqueue", side_effect=enqueue_and_look):
        client.post("/api/devices/pairing-code", headers=_headers(secret))

    assert _jobs(store) == [device_id]
    assert seen[0] is True  # the first job (pairing.issue_code's) already sees the code's key


def test_a_code_issued_by_get_api_config_queues_the_screen(store, client, register_device):
    device_id, secret = register_device()
    _pool(store, device_id)

    client.get("/api/config", headers=_headers(secret))  # unpaired, so this issues the code

    assert store.get(pairing_key.key(device_id)) is not None
    assert _jobs(store) == [device_id]


def test_using_the_code_queues_the_screen_after_deleting_its_key(store, paired_client):
    client, device_id, secret = paired_client
    _pool(store, device_id)
    code = client.post("/api/devices/pairing-code", headers=_headers(secret)).get_json()["code"]
    _take_jobs(store)
    seen = []
    enqueue_ = render_jobs.enqueue

    def enqueue_and_look(client, device_ids):
        seen.append(store.get(pairing_key.key(device_id)))
        return enqueue_(client, device_ids)

    with patch.object(render_jobs, "enqueue", side_effect=enqueue_and_look):
        client.post("/pair", data={"code": code})

    assert _jobs(store) == [device_id]
    assert seen[0] is None


def test_pairing_by_token_queues_the_screen(app, store, client):
    device_id = client.post(
        "/api/devices/register", json={"secret": "display-secret-for-a-token-pairing", "width": 800, "height": 480}
    ).get_json()["device_id"]
    _pool(store, device_id)
    with app.app_context():
        pairing.issue_code(device_id)
        token = db.session.get(Device, device_id).pairing_token
    _take_jobs(store)

    assert client.post("/pair/token", json={"token": token}).status_code == 200

    assert store.get(pairing_key.key(device_id)) is None
    assert _jobs(store) == [device_id]


def test_the_code_command_queues_the_screen(store, paired_client, cli):
    _client, device_id, _ = paired_client
    _pool(store, device_id)
    _take_jobs(store)

    assert cli("code", device_id).exit_code == 0

    assert _jobs(store) == [device_id]


# --- Redis down -----------------------------------------------------------------------------------------------------


def _down_redis():
    """Return a Redis that can't be asked: every command raises ConnectionError."""
    server = fakeredis.FakeServer()
    server.connected = False
    return fakeredis.FakeRedis(server=server, decode_responses=True)


def test_redis_down_never_fails_a_save_or_a_code(app, client, register_device):
    _device_id, secret = register_device()
    app.extensions["redis"] = _down_redis()

    with patch.object(render_jobs, "logger") as logger:
        code = client.get("/api/config", headers=_headers(secret)).get_json()["pairing_code"]
        asked = client.post("/api/devices/pairing-code", headers=_headers(secret))
        used = client.post("/pair", data={"code": asked.get_json()["code"]})
        saved = client.post("/device", data={"style": "card"})

    assert code
    assert asked.status_code == 200
    assert used.status_code == 302
    assert saved.status_code == 302
    assert logger.warning.call_count == 4  # the two codes issued, the one spent and the save
    for call in logger.warning.call_args_list:
        assert call.args[1:] == (1, "ConnectionError")
        assert code not in str(call)
        assert asked.get_json()["code"] not in str(call)


def test_a_redis_error_from_the_script_itself_is_logged_not_raised(store):
    _pool(store, DEVICE)
    with (
        patch.object(store, "pipeline", side_effect=redis.ResponseError("NOSCRIPT quoting a1b2c3")),
        patch.object(render_jobs, "logger") as logger,
    ):
        assert render_jobs.enqueue(store, [DEVICE]) == 0

    logger.warning.assert_called_once()
    assert logger.warning.call_args.args[1:] == (1, "ResponseError")
