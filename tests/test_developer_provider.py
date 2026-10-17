"""The developer provider (broker/developer_provider.py): what it writes to the store and the pace it asks for.

tests/fixtures/developer.json is the stored contract: pi-dashboard's renderer tests read the same file (copied into its
tests), so a change to either side that breaks the other shows up in both.
"""

import json
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import fakeredis
import pytest
import requests

from broker import developer_provider
from broker.db import db
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
from broker.provider_loop import SUB_CHANGED, next_tick

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "developer.json").read_text())
FETCHED_AT = datetime.fromisoformat(FIXTURE["fetched_at"]).timestamp()
GITHUB = FIXTURE["github"]
IDLE_GITHUB = GITHUB | {
    "releases": [{**GITHUB["releases"][0], "state": "done", "finished": "2026-10-17T11:59:00+00:00"}]
}


@pytest.fixture
def store():
    return fakeredis.FakeRedis(decode_responses=True)


@pytest.fixture
def provider(app, store):
    return DeveloperProvider(app, store, clock=lambda: FETCHED_AT)


@pytest.fixture
def device(app, register_device):
    """Make a device with the developer panel on (and a GitHub token, by default); return its id."""

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
    """Run a cycle against a GitHub that answers with `github` (the Copilot plan as the fixture has it)."""
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


def test_the_pace_is_withdrawn_once_the_release_is_over(provider, store, device):
    device_id = device()
    _run(provider)

    _run(provider, IDLE_GITHUB)

    assert store.get(pace_key(device_id)) is None
    assert _stored(store, stored_key(device_id))["github"]["releases"][0]["state"] == "done"


def test_a_release_just_finished_asks_for_nothing(provider, store, device):
    device_id = device()

    _run(provider, IDLE_GITHUB)

    assert store.get(pace_key(device_id)) is None


def test_it_polls_faster_while_a_release_runs_and_goes_back_after(provider, device):
    device()
    assert provider.interval_s() == IDLE_INTERVAL_S

    _run(provider)
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


def test_the_stored_json_is_what_the_route_answers(app, client, provider, store, device):
    """The route and the provider share one snapshot, so a renderer reads the same data either way."""
    device_id = device()
    _run(provider)
    with (
        patch("broker.clients.github.summary", return_value=GITHUB),
        patch("broker.clients.github.copilot_premium_requests", return_value=280),
        patch("broker.services.developer.ai_plans.plans", return_value=FIXTURE["plans"]),
    ):
        answer = client.get(
            "/api/developer", headers={"Authorization": "Bearer a-very-long-device-secret-1"}
        ).get_json()

    stored = _stored(store, stored_key(device_id))
    assert answer == {k: v for k, v in stored.items() if k != "fetched_at"}


def test_a_github_failure_writes_nothing_and_keeps_the_pace_of_a_running_release(provider, store, device):
    device_id = device()
    _run(provider)
    before = store.get(stored_key(device_id))

    with patch("broker.clients.github.summary", side_effect=requests.HTTPError("502")):
        provider.run_cycle()

    assert store.get(stored_key(device_id)) == before
    # One hiccup mid-release doesn't slow the polls down.
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


def test_redis_down_is_logged_and_tried_next_tick(app, device):
    device()
    server = fakeredis.FakeServer()
    server.connected = False  # every command raises ConnectionError
    provider = DeveloperProvider(
        app, fakeredis.FakeRedis(server=server, decode_responses=True), clock=lambda: FETCHED_AT
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
