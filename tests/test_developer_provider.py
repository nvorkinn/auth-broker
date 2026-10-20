"""The developer provider (broker/developer_provider.py): what it writes to the store and the pace it asks for.

The store contract's developer.json (countdown_api.fixtures) is the stored value: pi-dashboard's renderer tests read the
same file, so a change to either side that breaks the other shows up in both.
"""

import json
import shutil
import threading
import time
from datetime import UTC, datetime
from unittest.mock import Mock, patch

import fakeredis
import pytest
import redis
import requests
from countdown_api import fixtures
from cryptography.fernet import Fernet
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from broker import developer_provider, provider_loop
from broker.db import _db_path, db
from broker.developer_provider import (
    BURST_INTERVAL_S,
    IDLE_INTERVAL_S,
    PACE_S,
    PACE_TTL_S,
    TTL_S,
    DeveloperProvider,
    pace_key,
    stored_key,
)
from broker.models import DeviceConfig, GitHubToken
from broker.provider_loop import SUB_CHANGED, next_tick, read_only_app
from broker.services import render_jobs
from broker.services.render_jobs import JOBS, SCREENS, queued_key

FIXTURE = fixtures.load("developer.json")
FETCHED_AT = datetime.fromisoformat(FIXTURE["fetched_at"]).timestamp()
GITHUB = FIXTURE["github"]
# A release that just finished, still on the panel for github.SHOWN_AFTER.
JUST_DONE_GITHUB = GITHUB | {
    "releases": [{**GITHUB["releases"][0], "state": "done", "finished": "2026-10-17T11:59:00+00:00"}]
}
# No release on the panel: none running, and none finished within github.SHOWN_AFTER.
IDLE_GITHUB = GITHUB | {"releases": []}


@pytest.fixture
def store():
    return fakeredis.FakeRedis(decode_responses=True)


@pytest.fixture
def provider_app(app):
    """Return the app the provider runs in, as main() has it: read_only_app(), on the database that `app` writes."""
    provider_app = read_only_app()
    yield provider_app
    with provider_app.app_context():
        db.engine.dispose()


@pytest.fixture
def provider(provider_app, store):
    return DeveloperProvider(provider_app, store, clock=lambda: FETCHED_AT)


@pytest.fixture
def device(app, register_device):
    def _make(*, enabled=True, token="github_pat_abc", secret="a-very-long-device-secret-1"):  # noqa: S107
        device_id, _ = register_device(secret)
        with app.app_context():
            db.session.get(DeviceConfig, device_id).developer_enabled = enabled
            if token is not None:
                row = GitHubToken(device_id=device_id)
                row.token = token
                db.session.add(row)
            db.session.commit()
        return device_id

    return _make


def _run(provider, github=GITHUB):
    with (
        patch("broker.clients.github.summary", return_value=github) as summary,
        patch("broker.clients.github.copilot_premium_requests", return_value=280),
        patch("broker.services.developer.ai_plans.plans", return_value=FIXTURE["plans"]),
    ):
        provider.run_cycle()
    return summary


def _stored(store, key):
    value = store.get(key)
    return None if value is None else json.loads(value)


def test_writes_the_panels_data_in_the_stored_shape(provider, store, device):
    device_id = device()

    _run(provider)

    assert _stored(store, stored_key(device_id)) == FIXTURE
    assert 0 < store.ttl(stored_key(device_id)) <= TTL_S


def test_a_running_release_asks_for_a_faster_refresh_that_lapses(provider, store, device):
    device_id = device()

    _run(provider)

    assert store.get(pace_key(device_id)) == str(PACE_S)
    assert 0 < store.ttl(pace_key(device_id)) <= PACE_TTL_S


def test_a_release_just_finished_keeps_the_pace_while_it_shows(provider, store, device):
    device_id = device()
    _run(provider)

    _run(provider, JUST_DONE_GITHUB)

    assert store.get(pace_key(device_id)) == str(PACE_S)
    assert _stored(store, stored_key(device_id))["github"]["releases"][0]["state"] == "done"


def test_the_pace_is_withdrawn_once_the_release_is_off_the_panel(provider, store, device):
    device_id = device()
    _run(provider, JUST_DONE_GITHUB)

    _run(provider, IDLE_GITHUB)

    assert store.get(pace_key(device_id)) is None


def _pool(store, *device_ids):
    """Put screens in the render pool, as services/render_screens.py does."""
    store.hset(SCREENS, mapping={device_id: f"screen-{device_id}" for device_id in device_ids})


def _jobs(store):
    """Return the render pool's queue, and take its jobs as the pool's worker would (pop, then claim: the flag goes)."""
    jobs = store.lrange(JOBS, 0, -1)
    store.delete(JOBS, *(queued_key(device_id) for device_id in jobs))
    return jobs


def test_each_cycle_of_a_release_queues_a_render_of_the_screen(provider, store, device):
    device_id = device()
    _pool(store, device_id)

    _run(provider)
    assert _jobs(store) == [device_id]

    _run(provider)
    assert _jobs(store) == [device_id]


def test_a_render_still_queued_is_not_queued_twice(provider, store, device):
    device_id = device()
    _pool(store, device_id)

    _run(provider)
    _run(provider)

    assert store.lrange(JOBS, 0, -1) == [device_id]


def test_the_cycle_that_finds_the_release_gone_queues_one_last_render(provider, store, device):
    device_id = device()
    _pool(store, device_id)
    _run(provider, JUST_DONE_GITHUB)
    assert _jobs(store) == [device_id]

    _run(provider, IDLE_GITHUB)
    assert _jobs(store) == [device_id]

    _run(provider, IDLE_GITHUB)
    assert _jobs(store) == []


def test_no_release_queues_nothing(provider, store, device):
    _pool(store, device())

    _run(provider, IDLE_GITHUB)

    assert store.exists(JOBS) == 0


def test_only_the_screens_with_a_release_are_queued(provider, store, device):
    busy = device(secret="a-very-long-device-secret-1", token="busy")
    idle = device(secret="a-very-long-device-secret-2", token="idle")
    _pool(store, busy, idle)

    with (
        patch("broker.clients.github.summary", side_effect=lambda token: GITHUB if token == "busy" else IDLE_GITHUB),
        patch("broker.clients.github.copilot_premium_requests", return_value=280),
        patch("broker.services.developer.ai_plans.plans", return_value=FIXTURE["plans"]),
    ):
        provider.run_cycle()

    assert _jobs(store) == [busy]


def test_a_screen_with_its_own_renderer_is_not_queued(provider, store, device):
    device()

    _run(provider)

    assert store.exists(JOBS) == 0


def test_a_github_failure_mid_release_queues_nothing(provider, store, device):
    _pool(store, device())
    _run(provider)
    _jobs(store)

    with patch("broker.clients.github.summary", side_effect=requests.HTTPError("502")):
        provider.run_cycle()

    assert _jobs(store) == []


def test_a_job_that_cant_be_queued_is_logged_by_type_and_the_cycle_goes_on(provider, store, device):
    first = device(secret="a-very-long-device-secret-1")
    second = device(secret="a-very-long-device-secret-2")
    _pool(store, first, second)
    # Only the job fails (its text quoting a key, as a failed command's would); the data and the pace still go in.
    script = Mock(side_effect=redis.ConnectionError(queued_key(first)))
    failing = patch.object(store, "register_script", return_value=script)

    with failing, patch.object(render_jobs, "logger") as logger:
        _run(provider)

    for device_id in (first, second):
        assert _stored(store, stored_key(device_id)) == FIXTURE
        assert store.get(pace_key(device_id)) == str(PACE_S)
    assert logger.warning.call_count == 2
    assert "ConnectionError" in logger.warning.call_args.args
    assert first not in repr(logger.warning.call_args_list)


def test_it_polls_faster_while_a_release_runs_and_goes_back_after(provider, device):
    device()
    assert provider.interval_s() == IDLE_INTERVAL_S

    _run(provider)
    assert provider.interval_s() == BURST_INTERVAL_S

    _run(provider, JUST_DONE_GITHUB)
    assert provider.interval_s() == BURST_INTERVAL_S

    _run(provider, IDLE_GITHUB)
    assert provider.interval_s() == IDLE_INTERVAL_S


def test_a_device_without_a_token_still_gets_its_plans(provider, store, device):
    device_id = device(token=None)

    with (
        patch("broker.clients.github.summary") as summary,
        patch("broker.services.developer.ai_plans.plans", return_value=FIXTURE["plans"]) as plans,
    ):
        provider.run_cycle()

    summary.assert_not_called()
    assert plans.call_args.kwargs == {"has_copilot": False}
    assert _stored(store, stored_key(device_id)) == FIXTURE | {"github": None}
    assert store.get(pace_key(device_id)) is None


def test_a_device_with_the_panel_off_is_not_polled(provider, store, device):
    device(enabled=False)

    summary = _run(provider)

    summary.assert_not_called()
    assert store.keys("v1:*") == []


def test_polls_everything_each_cycle_whether_or_not_anyone_is_looking(provider, device):
    device()

    first = _run(provider)
    second = _run(provider)

    assert (first.call_count, second.call_count) == (1, 1)


def test_a_github_failure_writes_nothing_and_keeps_the_pace_of_a_running_release(provider, store, device):
    device_id = device()
    _run(provider)
    before = store.get(stored_key(device_id))

    with patch("broker.clients.github.summary", side_effect=requests.HTTPError("502")):
        provider.run_cycle()

    assert store.get(stored_key(device_id)) == before
    assert provider.interval_s() == BURST_INTERVAL_S


def test_one_failing_device_does_not_lose_the_others(app, provider, store, device):
    broken = device(secret="a-very-long-device-secret-1", token="bad")
    fine = device(secret="a-very-long-device-secret-2", token="good")

    def summary(token):
        if token == "bad":
            msg = "401"
            raise requests.HTTPError(msg)
        return GITHUB

    with (
        patch("broker.clients.github.summary", side_effect=summary),
        patch("broker.clients.github.copilot_premium_requests", return_value=280),
        patch("broker.services.developer.ai_plans.plans", return_value=FIXTURE["plans"]),
    ):
        provider.run_cycle()

    assert store.get(stored_key(broken)) is None
    assert _stored(store, stored_key(fine)) == FIXTURE


def test_a_device_switched_off_stops_the_fast_polls(app, provider, store, device):
    device_id = device()
    _run(provider)
    with app.app_context():
        db.session.get(DeviceConfig, device_id).developer_enabled = False
        db.session.commit()

    _run(provider)

    assert provider.interval_s() == IDLE_INTERVAL_S


def test_the_provider_cant_write_to_the_database(provider, device):
    device_id = device()

    with provider.app.app_context():
        db.session.get(DeviceConfig, device_id).developer_enabled = False
        with pytest.raises(OperationalError, match="readonly"):
            db.session.commit()


def test_a_cycle_reads_a_read_only_database_and_leaves_it_as_it_was(provider, store, device):
    """As in production, with ./data mounted read-only: nothing is created beside it (journal, WAL, lock), or changed.

    The modes only stop a process that isn't root (as in CI); the comparison catches a write either way.
    """
    device_id = device()
    path = _db_path()
    before = {file.name: file.read_bytes() for file in path.parent.iterdir()}
    modified = path.stat().st_mtime_ns
    path.chmod(0o444)
    path.parent.chmod(0o555)
    try:
        _run(provider)
    finally:
        path.parent.chmod(0o755)
        path.chmod(0o644)

    assert _stored(store, stored_key(device_id)) == FIXTURE
    assert {file.name: file.read_bytes() for file in path.parent.iterdir()} == before
    assert path.stat().st_mtime_ns == modified


def test_main_runs_the_provider_on_the_read_only_database(tmp_path, migrated_db, monkeypatch):
    """With only CREDENTIAL_ENCRYPTION_KEY of the broker's secrets, and no migrations: nothing is added beside it."""
    data = tmp_path / "data"
    data.mkdir()
    shutil.copyfile(migrated_db, data / "broker.db")
    monkeypatch.setenv("BROKER_DB_PATH", str(data / "broker.db"))
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", Fernet.generate_key().decode())
    for name in ("FLASK_SECRET_KEY", "SPOTIFY_CLIENT_ID", "SPOTIFY_CLIENT_SECRET", "SPOTIFY_REDIRECT_URI"):
        monkeypatch.delenv(name, raising=False)
    ran = []

    def run(self):  # a method, so not list.append itself, which wouldn't be handed the provider
        ran.append(self)

    monkeypatch.setattr(provider_loop, "configure_logging", lambda: None)
    monkeypatch.setattr(provider_loop, "connect", lambda: fakeredis.FakeRedis(decode_responses=True))
    monkeypatch.setattr(provider_loop.signal, "signal", lambda *_: None)
    monkeypatch.setattr(DeveloperProvider, "run", run)

    developer_provider.main()

    (provider,) = ran
    with provider.app.app_context():
        try:
            assert developer_provider.wanted_devices() == []
            with pytest.raises(OperationalError, match="readonly"):
                db.session.execute(text("DELETE FROM device_config"))
        finally:
            db.session.remove()
            db.engine.dispose()
    assert [file.name for file in data.iterdir()] == ["broker.db"]


def test_redis_down_is_logged_and_tried_next_tick(provider_app, device):
    device()
    server = fakeredis.FakeServer()
    server.connected = False  # every command raises ConnectionError
    provider = DeveloperProvider(
        provider_app, fakeredis.FakeRedis(server=server, decode_responses=True), clock=lambda: FETCHED_AT
    )

    # Patched rather than caplog: other tests (alembic's logging set-up) can leave loggers disabled.
    with (
        patch("broker.clients.github.summary", return_value=GITHUB),
        patch("broker.clients.github.copilot_premium_requests", return_value=280),
        patch.object(provider, "logger") as logger,
    ):
        provider._try_cycle()  # the loop's own error handling: it doesn't raise

    assert "couldn't reach Redis" in logger.warning.call_args.args[0]


def test_a_nudge_on_sub_changed_ends_the_wait_early(app, store):
    provider = DeveloperProvider(app, store, clock=lambda: FETCHED_AT)
    assert not provider._listen(0.01)  # subscribes; nothing published yet

    store.publish(SUB_CHANGED, "")

    assert provider._listen(0.5)


def test_stop_ends_the_wait(app, store):
    """Within a second, though the next tick is most of a minute away."""
    provider = DeveloperProvider(app, store, clock=lambda: FETCHED_AT + 1)
    threading.Timer(0.1, provider.stop).start()

    started = time.monotonic()
    provider._wait_for_next_cycle()

    assert time.monotonic() - started < 2


def test_ticks_are_epoch_aligned_at_the_current_interval(app, store):
    assert next_tick(FETCHED_AT + 1, BURST_INTERVAL_S) % BURST_INTERVAL_S == 0
    assert developer_provider.IDLE_INTERVAL_S > developer_provider.BURST_INTERVAL_S
    assert datetime.fromtimestamp(next_tick(FETCHED_AT + 1, 60), UTC).second == 0


def _rate_limited(status=429, headers=None, text=""):
    response = requests.Response()
    response.status_code = status
    response.headers.update(headers or {})
    response._content = text.encode()
    return requests.HTTPError(f"{status}", response=response)


class Clock:
    def __init__(self, now=FETCHED_AT):
        self.now = now

    def __call__(self):
        return self.now


@pytest.mark.parametrize(
    ("error", "wait"),
    [
        (_rate_limited(429, {"Retry-After": "90"}), 90),
        (_rate_limited(403, {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": str(int(FETCHED_AT) + 600)}), 600),
        (_rate_limited(403, text='{"message": "You have exceeded a secondary rate limit"}'), 60),
    ],
)
def test_a_rate_limited_token_is_left_alone_until_github_says(provider_app, store, device, error, wait):
    device_id = device()
    clock = Clock()
    provider = DeveloperProvider(provider_app, store, clock=clock)
    _run(provider)
    before = store.get(stored_key(device_id))

    with patch("broker.clients.github.summary", side_effect=error), patch.object(provider, "logger") as logger:
        provider.run_cycle()
    assert "rate-limiting" in logger.warning.call_args.args[0]
    assert store.get(stored_key(device_id)) == before

    clock.now += wait - 1
    assert _run(provider).call_count == 0  # still backing off: GitHub isn't asked

    clock.now += 1
    assert _run(provider).call_count == 1
    assert _stored(store, stored_key(device_id))["fetched_at"] == datetime.fromtimestamp(clock.now, UTC).isoformat()


def test_a_rate_limited_device_does_not_hold_up_the_others(provider_app, store, device):
    limited = device(secret="a-very-long-device-secret-1", token="limited")
    fine = device(secret="a-very-long-device-secret-2", token="fine")
    provider = DeveloperProvider(provider_app, store, clock=Clock())

    def summary(token):
        if token == "limited":
            raise _rate_limited(429, {"Retry-After": "300"})
        return GITHUB

    for _ in range(2):
        with (
            patch("broker.clients.github.summary", side_effect=summary) as called,
            patch("broker.clients.github.copilot_premium_requests", return_value=280),
            patch("broker.services.developer.ai_plans.plans", return_value=FIXTURE["plans"]),
        ):
            provider.run_cycle()

    # The limited token was tried once, then left alone; the other was polled both times.
    assert [call.args[0] for call in called.call_args_list] == ["fine"]
    assert store.get(stored_key(limited)) is None
    assert _stored(store, stored_key(fine)) == FIXTURE


def test_a_403_that_isnt_a_rate_limit_does_not_back_off(provider_app, store, device):
    device()
    provider = DeveloperProvider(provider_app, store, clock=Clock())

    with patch("broker.clients.github.summary", side_effect=_rate_limited(403, text='{"message": "Bad credentials"}')):
        provider.run_cycle()

    assert _run(provider).call_count == 1
