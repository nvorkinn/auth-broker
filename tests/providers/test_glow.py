"""The glow-provider: Glowmarkt mocked (responses), Redis faked (fakeredis), the broker's database a real one.

tests/fixtures/glow_energy.json is the contract with pi-dashboard, whose tests read the same file: what Glowmarkt
answers, what the provider asks it, and the `v1:energy:<device id>` value that comes of it.
"""

import json
from datetime import UTC, datetime
from pathlib import Path

import fakeredis
import pytest
import redis
import responses

from broker.clients import glowmarkt
from broker.db import db
from broker.models import Device, DeviceConfig, GlowmarktCredentials
from broker.providers import glow

FIXTURE = json.loads((Path(__file__).parent.parent / "fixtures" / "glow_energy.json").read_text())
DEVICE = FIXTURE["device_id"]
RESOURCE = FIXTURE["stored"]["resource_id"]
READINGS_URL = f"{glowmarkt.BASE_URL}/resource/{RESOURCE}/readings"
NOW = datetime.fromisoformat(FIXTURE["now"]).astimezone(UTC)


def add_device(app, device_id, *, username="me@example.com", password="hunter2", postcode="", **creds):  # noqa: S107
    with app.app_context():
        db.session.add(Device(device_id=device_id, created_at=NOW))
        db.session.add(DeviceConfig(device_id=device_id, postcode=postcode))
        stored = GlowmarktCredentials(device_id=device_id, username=username, **creds)
        if password is not None:
            stored.password = password
        db.session.add(stored)
        db.session.commit()


class FakeGlowmarkt:
    """Glowmarkt answering as in the fixture, for any account but the ones told to fail.

    A sign-in gets the fixture's token, or the next of `tokens`; a token in `expired` is refused with a 401.
    """

    def __init__(self, rsps):
        self.rsps = rsps
        self.wrong_usernames = set()
        self.tokens = []
        self.expired = set()
        rsps.add_callback(responses.POST, f"{glowmarkt.BASE_URL}/auth", callback=self.auth)
        rsps.add(responses.GET, f"{glowmarkt.BASE_URL}/virtualentity", json=FIXTURE["virtualentity"])
        rsps.add_callback(responses.GET, READINGS_URL, callback=self.readings)

    def auth(self, request):
        body = json.loads(request.body)
        if body["username"] in self.wrong_usernames:
            return 401, {}, json.dumps({"valid": False})
        token = self.tokens.pop(0) if self.tokens else FIXTURE["auth"]["token"]
        return 200, {}, json.dumps(FIXTURE["auth"] | {"token": token})

    def readings(self, request):
        if request.headers["token"] in self.expired:
            return 401, {}, json.dumps({"error": "token expired"})
        period = request.params["period"]
        if request.params != FIXTURE["requests"][period]:
            return 200, {}, json.dumps({"status": "OK", "data": []})  # another zone's, or another time's
        return 200, {}, json.dumps(FIXTURE["readings"][period])

    def calls_to(self, path):
        return [c for c in self.rsps.calls if c.request.url.split("?")[0].endswith(path)]


@pytest.fixture
def glow_api():
    """Fake Glowmarkt; anything else asked of the network fails the test."""
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        yield FakeGlowmarkt(rsps)


@pytest.fixture
def store():
    return fakeredis.FakeRedis(decode_responses=True)


@pytest.fixture
def provider(app, store):
    provider_app = glow.create_app()
    yield glow.GlowProvider(provider_app, store, now=lambda: NOW)
    with provider_app.app_context():
        db.engine.dispose()


def test_writes_glowmarkts_answers_under_the_devices_key_with_a_ttl(app, provider, store, glow_api):
    add_device(app, DEVICE)

    assert provider.run_cycle() == 1

    assert json.loads(store.get(f"v1:energy:{DEVICE}")) == FIXTURE["stored"]
    assert 0 < store.ttl(f"v1:energy:{DEVICE}") <= glow.TTL_S
    sign_in = json.loads(glow_api.calls_to("/auth")[0].request.body)
    assert sign_in == {"username": "me@example.com", "password": "hunter2", "applicationId": glowmarkt.APPLICATION_ID}
    readings = glow_api.calls_to("/readings")
    assert len(readings) == 3
    assert all(c.request.headers["token"] == FIXTURE["auth"]["token"] for c in readings)


def test_asks_for_the_screens_own_zone(app, provider, store, glow_api):
    """A Norwegian screen's days are Oslo's: in September, two hours ahead of UTC, so offset=-120."""
    add_device(app, "oslo", postcode="0150")

    assert provider.run_cycle() == 1

    stored = json.loads(store.get("v1:energy:oslo"))
    assert stored["offset"] == "-120"
    hourly = glow_api.calls_to("/readings")[0].request.params
    assert (hourly["from"], hourly["to"]) == ("2026-09-24T07:00:00", "2026-09-25T07:10:00")


def test_a_device_that_cant_sign_in_doesnt_stop_the_others(app, provider, store, glow_api):
    add_device(app, "a-wrong", username="wrong@example.com")
    add_device(app, DEVICE)
    glow_api.wrong_usernames.add("wrong@example.com")

    assert provider.run_cycle() == 1

    assert store.get("v1:energy:a-wrong") is None
    assert store.get(f"v1:energy:{DEVICE}") is not None


def test_devices_without_a_usable_account_are_skipped(app, provider, store, glow_api):
    add_device(app, "no-password", password=None)
    add_device(app, "no-username", username="  ")
    add_device(app, "undecryptable", password=None, password_encrypted="not-a-fernet-token")
    add_device(app, DEVICE)

    assert provider.run_cycle() == 1

    assert store.keys("v1:energy:*") == [f"v1:energy:{DEVICE}"]
    assert len(glow_api.calls_to("/auth")) == 1


def test_keeps_its_token_between_cycles(app, provider, glow_api):
    add_device(app, DEVICE)

    provider.run_cycle()
    provider.run_cycle()

    assert len(glow_api.calls_to("/auth")) == 1
    assert len(glow_api.calls_to("/virtualentity")) == 1
    assert len(glow_api.calls_to("/readings")) == 6


def test_signs_in_again_when_the_token_has_expired(app, provider, store, glow_api):
    add_device(app, DEVICE)
    provider.run_cycle()
    store.flushall()
    glow_api.expired.add(FIXTURE["auth"]["token"])
    glow_api.tokens.append("glow-token-2")

    provider.run_cycle()

    assert json.loads(store.get(f"v1:energy:{DEVICE}")) == FIXTURE["stored"]
    assert len(glow_api.calls_to("/auth")) == 2
    assert glow_api.calls_to("/readings")[-1].request.headers["token"] == "glow-token-2"


def test_a_change_of_password_signs_in_afresh(app, provider, glow_api):
    add_device(app, DEVICE)
    provider.run_cycle()
    with app.app_context():
        db.session.get(GlowmarktCredentials, DEVICE).password = "new-password"
        db.session.commit()

    provider.run_cycle()

    sign_ins = [json.loads(c.request.body)["password"] for c in glow_api.calls_to("/auth")]
    assert sign_ins == ["hunter2", "new-password"]


def test_a_nudge_only_fetches_accounts_new_since_the_last_tick(app, provider, store, glow_api):
    add_device(app, DEVICE)
    provider.run_cycle()
    add_device(app, "new-device")

    assert provider.run_cycle(nudged=True) == 1

    assert store.get("v1:energy:new-device") is not None
    assert len(glow_api.calls_to("/readings")) == 6


def test_an_unreachable_redis_is_logged_and_retried_next_tick(app, provider, store, glow_api, monkeypatch, caplog):
    add_device(app, DEVICE)
    working = provider.store
    provider.store = redis.Redis(host="127.0.0.1", port=1, socket_connect_timeout=0.1)

    assert provider.run_cycle() == 0
    assert "Couldn't write" in caplog.text

    provider.store = working
    assert provider.run_cycle(nudged=True) == 1
    assert store.get(f"v1:energy:{DEVICE}") is not None


# --- The loop ------------------------------------------------------------------------------


class FakeClock:
    def __init__(self, start):
        self.t = start

    def __call__(self):
        return self.t


def test_waits_for_the_next_epoch_aligned_tick(app, store, monkeypatch):
    clock = FakeClock(1000 * glow.INTERVAL_S + 123.4)
    provider = glow.GlowProvider(glow.create_app(), store, clock=clock)
    monkeypatch.setattr(provider, "run_cycle", lambda **_: 0)
    deadlines = []

    def wait(deadline):
        deadlines.append(deadline)
        provider.stop()
        return False

    monkeypatch.setattr(provider, "_wait", wait)
    provider.serve()

    assert deadlines == [1001 * glow.INTERVAL_S]


def test_a_nudge_on_sub_changed_ends_the_wait(app, store):
    clock = FakeClock(0.0)
    provider = glow.GlowProvider(glow.create_app(), store, clock=clock)
    provider._pubsub = store.pubsub(ignore_subscribe_messages=True)
    provider._pubsub.subscribe(glow.SUB_CHANGED)
    store.publish(glow.SUB_CHANGED, "")

    assert provider._wait(glow.INTERVAL_S) is True


def test_the_wait_ends_at_the_deadline_or_a_stop(app, store):
    clock = FakeClock(100.0)
    provider = glow.GlowProvider(glow.create_app(), store, clock=clock)

    assert provider._wait(50.0) is False
    provider.stop()
    assert provider._wait(10_000.0) is False


def test_serve_runs_a_nudged_cycle_after_a_nudge_and_a_full_one_after_a_tick(app, store, monkeypatch):
    provider = glow.GlowProvider(glow.create_app(), store)
    cycles = []
    waits = iter([True, False])

    def wait(_deadline):
        try:
            return next(waits)
        except StopIteration:
            provider.stop()
            return False

    monkeypatch.setattr(provider, "run_cycle", lambda *, nudged: cycles.append(nudged))
    monkeypatch.setattr(provider, "_wait", wait)
    provider.serve()

    assert cycles == [False, True, False]


def test_the_provider_cant_write_to_the_database(app, provider):
    add_device(app, DEVICE)
    with provider.app.app_context():
        db.session.get(GlowmarktCredentials, DEVICE).username = "changed"
        with pytest.raises(Exception, match="readonly"):
            db.session.commit()
