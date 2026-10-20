"""The pacing component (broker/services/pace.py): how long a screen sleeps after each poll.

sleep_s is tested exhaustively with a frozen clock, as a pure function; screen_hints, which gathers its hints, with
fakeredis and the broker's database. What the routes answer with it is in tests/routes/test_frames.py.
"""

import math
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock, patch

import fakeredis
import pytest
import redis

from broker import developer_provider, spotify_provider
from broker.db import db
from broker.models import DeviceConfig
from broker.services import pace
from broker.services.pace import (
    HINT_FLOOR_S,
    LEAD_S,
    MIN_SLEEP_S,
    POLLS_PER_RENDER,
    RENDER_INTERVAL_S,
    Hint,
    poll_interval_s,
    sleep_s,
)

# A minute boundary: 12:00:00 UTC, which is also a multiple of 20, 30 and 60 seconds from the epoch.
MINUTE = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)


def _at(seconds: float) -> datetime:
    return MINUTE + timedelta(seconds=seconds)


def _live(seconds: int, now: datetime = MINUTE, for_s: float = 60) -> Hint:
    return Hint(seconds, now + timedelta(seconds=for_s))


def _since_epoch(at: datetime) -> float:
    return (at - datetime(1970, 1, 1, tzinfo=UTC)).total_seconds()


# --- the constants -----------------------------------------------------------------------------------------------


def test_the_settled_numbers():
    # Renders every minute, polls twice as often, 3 s after each half-minute; hints floored at 20 s (polls every
    # 10 s at the fastest); never a sleep under 5 s. See the module's docstring for why.
    assert (RENDER_INTERVAL_S, POLLS_PER_RENDER, LEAD_S, HINT_FLOOR_S, MIN_SLEEP_S) == (60, 2, 3, 20, 5)
    # A minute boundary is a boundary of every poll interval a hint that divides 60 can give.
    assert _since_epoch(MINUTE) % 60 == 0


# --- no hints: polls at :03 and :33 ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("offset", "expected"),
    [
        (0, 33),  # :03 is under MIN_SLEEP_S away, so :33
        (0.5, 33),  # rounded up: 32.5 s away, and never early
        (1, 32),
        (2.999, 31),
        (3, 30),  # on the slot itself: the next one
        (3.001, 30),
        (10, 23),
        (27.999, 6),
        (28, 5),  # exactly MIN_SLEEP_S away: kept
        (28.001, 35),  # just under: the one after, :03 of the next minute
        (29, 34),
        (30, 33),
        (33, 30),
        (45, 18),
        (57.5, 6),
        (58, 5),
        (58.5, 35),
        (59, 34),
        (59.999, 34),
    ],
)
def test_without_hints_the_screen_wakes_just_after_each_half_minute(offset, expected):
    assert sleep_s(_at(offset), []) == expected


@pytest.mark.parametrize("minute", [0, 1, 7, 59, 60 * 24])
def test_every_minute_is_the_same(minute):
    start = MINUTE + timedelta(minutes=minute)

    assert [sleep_s(start + timedelta(seconds=s), []) for s in range(60)] == [sleep_s(_at(s), []) for s in range(60)]


# --- every offset: where each sleep lands ------------------------------------------------------------------------

HINT_SETS = {
    "none": [],
    "20s": [_live(20, for_s=600)],
    "30s": [_live(30, for_s=600)],
    "40s": [_live(40, for_s=600)],
    "45s, not dividing 60": [_live(45, for_s=600)],
    "under the floor": [_live(7, for_s=600)],
    "over the render interval": [_live(90, for_s=600)],
    "several": [_live(40, for_s=600), _live(25, for_s=600), _live(50, for_s=600)],
}


@pytest.mark.parametrize("hints", HINT_SETS.values(), ids=HINT_SETS.keys())
def test_each_sleep_lands_lead_s_after_a_boundary_and_is_the_first_far_enough_away(hints):
    period = poll_interval_s(MINUTE, hints)
    # MINUTE is a boundary of each of these intervals, so offsets from it are offsets from the epoch's grid.
    assert _since_epoch(MINUTE) % period == 0
    # Every quarter second for two minutes, and some that aren't on any grid.
    offsets = [i / 4 for i in range(4 * 120)] + [0.001, 2.9999, 3.0001, 17.123456, 64.999999]
    for offset in offsets:
        slept = sleep_s(_at(offset), hints)
        assert isinstance(slept, int)
        # Never sooner than MIN_SLEEP_S, never longer than one interval past it.
        assert MIN_SLEEP_S <= slept <= period + MIN_SLEEP_S + 1, offset
        # It aims at LEAD_S after a boundary of the poll interval; rounding up to a whole second wakes the screen up
        # to a second after that, never before.
        wake = offset + slept
        aimed = math.floor((wake - LEAD_S) / period + 1e-9) * period + LEAD_S
        assert 0 <= wake - aimed < 1, offset
        # And that's the first such slot at least MIN_SLEEP_S away: the one before it is too close, or past.
        assert aimed - offset >= MIN_SLEEP_S - 1e-9, offset
        assert aimed - period - offset < MIN_SLEEP_S, offset


@pytest.mark.parametrize("hints", HINT_SETS.values(), ids=HINT_SETS.keys())
def test_from_a_whole_second_the_sleep_lands_exactly_lead_s_after_a_boundary(hints):
    period = poll_interval_s(MINUTE, hints)
    for offset in range(120):
        wake = offset + sleep_s(_at(offset), hints)
        # A 22.5 s or 12.5 s interval only meets whole seconds every other slot, so those wake half a second after it.
        assert (wake - LEAD_S) % period in ({0} if period == int(period) else {0, 0.5}), offset


def test_a_screen_that_obeys_keeps_waking_at_the_same_slots():
    # The broker answers at once, so a screen that sleeps what it's told polls just after :03 and :33 from then on.
    now = _at(17.4)
    for _ in range(6):
        now += timedelta(seconds=sleep_s(now, []))
        assert round((now - MINUTE).total_seconds() % 30, 6) == 3.4


def test_network_delay_never_adds_up():
    # However late each poll arrives (a slow request, a frame being drawn), the next answer starts from the clock.
    now = _at(3)
    for delay in [0.4, 2.7, 4.9, 1.2, 0.0, 3.3, 4.0, 0.8] * 3:
        wake = now + timedelta(seconds=sleep_s(now, []))
        assert LEAD_S <= (wake - MINUTE).total_seconds() % 30 < LEAD_S + 1
        now = wake + timedelta(seconds=delay)


# --- hints -------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("seconds", "interval"),
    [
        (20, 10),  # the developer provider's, in a release: polls every 10 s
        (30, 15),
        (40, 20),
        (45, 22.5),
        (59, 29.5),
        (60, 30),  # equal to the render interval: as if none
        (61, 30),  # longer: as if none
        (3600, 30),
        (19, 10),  # under the floor: the floor
        (5, 10),
        (1, 10),
    ],
)
def test_a_hint_is_floored_capped_and_polled_at_half(seconds, interval):
    assert poll_interval_s(MINUTE, [_live(seconds)]) == interval


def test_without_a_hint_the_screen_polls_twice_a_render():
    assert poll_interval_s(MINUTE, []) == RENDER_INTERVAL_S / POLLS_PER_RENDER == 30


@pytest.mark.parametrize(
    ("offset", "expected"),
    [
        (0, 13),  # :03 is too close
        (1, 12),
        (3, 10),
        (7, 6),
        (8, 5),
        (8.5, 15),
        (9, 14),
        (10, 13),
        (13, 10),
        (59, 14),  # :03 of the next minute is too close, so :13
    ],
)
def test_a_20s_hint_polls_every_10s_just_after_each_boundary(offset, expected):
    now = _at(offset)

    assert sleep_s(now, [_live(20, now)]) == expected


def test_a_hint_that_does_not_divide_the_minute_is_aligned_to_its_own_multiples_from_the_epoch():
    # 45 s: the provider's cycles are at multiples of 45 s from the epoch, so the polls are at multiples of 22.5 + 3.
    boundary = datetime.fromtimestamp(45 * 40_000_000, UTC)
    assert sleep_s(boundary, [_live(45, boundary)]) == 26  # boundary + 3 is too close; + 22.5 + 3, rounded up
    assert sleep_s(boundary + timedelta(seconds=3), [_live(45, boundary)]) == 23  # 22.5 exactly, rounded up
    assert sleep_s(boundary + timedelta(seconds=25.5), [_live(45, boundary)]) == 23  # the next: + 45 + 3


def test_the_shortest_live_hint_wins():
    hints = [_live(40), _live(25), _live(50)]

    assert poll_interval_s(MINUTE, hints) == 12.5
    assert sleep_s(_at(10), hints) == 6  # :15.5 is 5.5 s away, rounded up


def test_an_expired_hint_counts_for_nothing():
    expired = Hint(20, MINUTE - timedelta(seconds=1))

    assert sleep_s(_at(10), [expired]) == sleep_s(_at(10), []) == 23


def test_a_hint_expires_at_its_until_exactly():
    at_until = Hint(20, _at(10))
    just_after = Hint(20, _at(10.001))

    assert sleep_s(_at(10), [at_until]) == 23
    assert sleep_s(_at(10), [just_after]) == 13


def test_an_expired_shorter_hint_leaves_the_live_longer_one():
    hints = [Hint(20, MINUTE - timedelta(seconds=5)), _live(40)]

    assert poll_interval_s(MINUTE, hints) == 20


def test_hints_all_expired_are_the_render_interval():
    hints = [Hint(20, MINUTE), Hint(30, MINUTE - timedelta(hours=1))]

    assert poll_interval_s(MINUTE, hints) == 30


def test_the_hints_order_does_not_matter():
    hints = [_live(40), Hint(20, MINUTE - timedelta(seconds=1)), _live(25)]

    assert sleep_s(_at(41), hints) == sleep_s(_at(41), list(reversed(hints)))


def test_a_naive_time_is_refused():
    with pytest.raises(TypeError):
        sleep_s(datetime(2026, 10, 18, 12, 0), [])  # noqa: DTZ001 -- the point of the test


# --- the hints a screen has: its panels' pace keys ----------------------------------------------------------------


@pytest.fixture
def store(app):
    store = fakeredis.FakeRedis(decode_responses=True)
    app.extensions["redis"] = store
    return store


@pytest.fixture
def screen(app, register_device):
    """Register a screen; return a function that sets its bursty panels and returns its id."""
    device_id, _ = register_device()

    def _set(*, developer: bool = False, spotify: bool = False) -> str:
        with app.app_context():
            config = db.session.get(DeviceConfig, device_id)
            config.developer_enabled = developer
            config.spotify_enabled = spotify
            db.session.commit()
        return device_id

    return _set


def _hints(app, device_id, now=MINUTE):
    with app.app_context():
        return pace.screen_hints(device_id, now)


def test_the_developer_panel_has_the_key_the_developer_provider_writes(app, screen):
    device_id = screen(developer=True)

    with app.app_context():
        keys = pace.hint_keys(db.session.get(DeviceConfig, device_id))

    assert keys == [developer_provider.pace_key(device_id)] == [f"v1:pace:developer:{device_id}"]


def test_the_music_area_has_the_key_the_music_provider_writes(app, screen):
    device_id = screen(spotify=True)

    with app.app_context():
        keys = pace.hint_keys(db.session.get(DeviceConfig, device_id))

    assert keys == [spotify_provider.pace_key(device_id)] == [f"v1:pace:spotify:{device_id}"]


def test_both_panels_have_both_keys(app, screen):
    device_id = screen(developer=True, spotify=True)

    with app.app_context():
        keys = pace.hint_keys(db.session.get(DeviceConfig, device_id))

    assert keys == [f"v1:pace:developer:{device_id}", f"v1:pace:spotify:{device_id}"]


def test_a_screen_without_bursty_panels_has_no_keys_and_asks_redis_nothing(app, screen):
    device_id = screen(developer=False)
    app.extensions["redis"] = untouched = Mock()

    with app.app_context():
        assert pace.hint_keys(db.session.get(DeviceConfig, device_id)) == []
        assert pace.hint_keys(None) == []
    assert _hints(app, device_id) == []
    assert untouched.mock_calls == []


def test_a_pace_key_is_a_hint_until_its_key_expires(app, store, screen):
    device_id = screen(developer=True)
    store.set(f"v1:pace:developer:{device_id}", "20", px=45_000)

    [hint] = _hints(app, device_id)

    assert hint.seconds == 20
    assert MINUTE + timedelta(seconds=44) < hint.until <= MINUTE + timedelta(seconds=45)


def test_a_pace_key_is_ignored_while_the_panel_is_off(app, store, screen):
    device_id = screen(developer=False)
    store.set(f"v1:pace:developer:{device_id}", "20", ex=60)

    assert _hints(app, device_id) == []


@pytest.mark.parametrize("value", ["0", "-5", "soon", "2.5", "", "²"])
def test_a_pace_key_that_is_not_a_positive_whole_number_is_ignored(app, store, screen, value):
    device_id = screen(developer=True)
    store.set(f"v1:pace:developer:{device_id}", value, ex=60)

    assert _hints(app, device_id) == []


def test_a_pace_key_without_a_ttl_is_ignored(app, store, screen):
    # It would never lapse, and keep the screen fast for good.
    device_id = screen(developer=True)
    store.set(f"v1:pace:developer:{device_id}", "20")

    assert _hints(app, device_id) == []


def test_no_pace_key_is_no_hint(app, store, screen):
    assert _hints(app, screen(developer=True)) == []


def test_redis_down_is_no_hints(app, screen):
    device_id = screen(developer=True)
    app.extensions["redis"] = redis.Redis(host="127.0.0.1", port=1, socket_connect_timeout=0.2)

    with patch.object(pace, "logger") as logger:
        hints = _hints(app, device_id)

    assert hints == []
    logger.warning.assert_called_once_with("Couldn't read the screen's pace hints from Redis (%s)", "ConnectionError")


def test_the_legacy_hint_is_gone():
    assert not hasattr(pace, "PACE_LEASE_S")
    assert not hasattr(pace, "parse_hint")
