"""Jobs for the render pool (broker/services/render_jobs.py): the pool's own enqueue script, on fakeredis's Lua.

tests/fixtures/render_enqueue.json is a byte-for-byte copy of pi-dashboard's script (countdown_core.store.render_queue
`_ENQUEUE`), so the broker queues jobs exactly as the pool's ticker does.
"""

import json
import time
from pathlib import Path
from unittest.mock import patch

import fakeredis
import pytest

from broker.services import render_jobs
from broker.services.render_jobs import JOBS, QUEUED_TTL_MS, SCREENS, enqueue, queued_key

FIXTURE = json.loads((Path(__file__).parent.parent / "fixtures" / "render_enqueue.json").read_text())


@pytest.fixture
def store():
    """Return a store where the pool renders screens a, b and a1b2c3d4e5f6 (as the orchestrator lists them)."""
    store = fakeredis.FakeRedis(decode_responses=True)
    store.hset(SCREENS, mapping={"a": "screen-a", "b": "screen-b", "a1b2c3d4e5f6": "screen-c"})
    return store


def test_the_script_is_pi_dashboards_byte_for_byte():
    assert FIXTURE["script"] == render_jobs._ENQUEUE


def test_the_keys_are_the_pools():
    # pi-dashboard's keys.RENDER_JOBS and keys.render_queued.
    assert JOBS == "v1:render:jobs"
    assert SCREENS == "v1:render:screens"
    assert queued_key("a1b2c3d4e5f6") == "v1:render:queued:a1b2c3d4e5f6"
    assert QUEUED_TTL_MS == 60_000


def test_queues_each_screen_with_its_flag(store):
    before_ms = int(time.time() * 1000)

    assert enqueue(store, ["a", "b"]) == 2

    # LPUSH, as the pool's: the worker BRPOPs, so the first queued is the first drawn.
    assert store.lrange(JOBS, 0, -1) == ["b", "a"]
    for device_id in ("a", "b"):
        # The flag holds when it was queued, by Redis's clock, in Unix ms (the pool's measure of a job's wait).
        assert int(store.get(queued_key(device_id))) >= before_ms - 1000
        assert 0 < store.pttl(queued_key(device_id)) <= QUEUED_TTL_MS


def test_a_screen_already_queued_is_queued_once(store):
    enqueue(store, ["a"])

    assert enqueue(store, ["a", "b"]) == 1

    assert store.lrange(JOBS, 0, -1) == ["b", "a"]


def test_a_screen_is_queued_again_once_its_job_is_taken(store):
    enqueue(store, ["a"])
    # What the pool's worker does: pop the job, and claim it (which deletes the flag).
    store.rpop(JOBS)
    store.delete(queued_key("a"))

    assert enqueue(store, ["a"]) == 1
    assert store.lrange(JOBS, 0, -1) == ["a"]


def test_only_the_screens_the_pool_renders_are_queued(store):
    assert enqueue(store, ["a", "own-renderer"]) == 1

    assert store.lrange(JOBS, 0, -1) == ["a"]
    assert store.exists(queued_key("own-renderer")) == 0


def test_with_no_pool_nothing_is_written():
    # Before the pool is deployed nothing would take jobs off the list, which has no TTL.
    store = fakeredis.FakeRedis(decode_responses=True)

    assert enqueue(store, ["a"]) == 0

    assert store.keys("*") == []


def test_no_screens_asks_nothing(store):
    with patch.object(store, "pipeline") as pipeline:
        assert enqueue(store, []) == 0
    pipeline.assert_not_called()


def test_redis_down_is_logged_by_type_and_not_raised():
    server = fakeredis.FakeServer()
    server.connected = False  # every command raises ConnectionError
    store = fakeredis.FakeRedis(server=server, decode_responses=True)

    # Patched rather than caplog: other tests (alembic's logging set-up) can leave loggers disabled.
    with patch.object(render_jobs, "logger") as logger:
        assert enqueue(store, ["a1b2c3d4e5f6"]) == 0

    logger.warning.assert_called_once()
    assert "ConnectionError" in logger.warning.call_args.args
    assert "a1b2c3d4e5f6" not in repr(logger.warning.call_args)
