"""How long a screen sleeps after each poll: the one place any poll or refresh time is worked out.

The screen (the ESP32) has no clock: it sleeps for the whole seconds each answer's `Retry-After` gives it. So every
`GET` and `POST /api/frame` answer, with a frame, a 304 or a 404, carries `sleep_s(now, hints)`, worked out afresh from
the server's clock, and nothing else computes a poll or refresh time.

- Renders happen on the minute: a render boundary is every multiple of RENDER_INTERVAL_S from the epoch.
- The screen polls POLLS_PER_RENDER times as often, LEAD_S after each boundary of that shorter period, so it wakes just
  after a render has landed: with nothing bursty, at about :03 and :33. An out-of-cycle render (a config save, a burst)
  then shows within about half an interval.
- A provider whose data is bursty asks for faster with a hint: a `v1:pace:<source>:<id>` key holding seconds, whose TTL
  is the hint's expiry. The shortest live hint, never under HINT_FLOOR_S nor over RENDER_INTERVAL_S, takes the place
  of the render interval, and the screen polls at half of it, aligned to its multiples (the providers' cycles are
  epoch-aligned too, see provider_loop.next_tick).
- A slot under MIN_SLEEP_S away is skipped for the one after, so the screen never wakes straight back up, and network
  delay never adds up: the next answer starts from the clock again.

sleep_s and poll_interval_s are pure. screen_hints gathers a screen's hints for them: its own panels' pace keys
(hint_keys, from its config) read from Redis in one round trip.
"""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from itertools import batched
from typing import TYPE_CHECKING

from flask import current_app
from redis import Redis, RedisError

from broker.db import db
from broker.models import DeviceConfig

if TYPE_CHECKING:
    from collections.abc import Sequence

logger = logging.getLogger(__name__)

# How often the screens are rendered, in seconds: on every minute (the render pool's ticker). Also the config's
# `interval` in the store.
RENDER_INTERVAL_S = 60
# The screen polls this many times per render interval (or per hint), so an out-of-cycle render shows within about
# half an interval, and its piggybacked logs and metrics are flushed as often.
POLLS_PER_RENDER = 2
# How long after a boundary the screen wakes: long enough for the minute's renders to have landed, short enough for
# the clock on the screen to be right.
LEAD_S = 3
# The fastest refresh any hint gets, so a screen never polls more often than every HINT_FLOOR_S / POLLS_PER_RENDER.
HINT_FLOOR_S = 20
# The shortest sleep the screen is ever given: a nearer slot is skipped for the one after.
MIN_SLEEP_S = 5

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_ONE_US = timedelta(microseconds=1)
_US_PER_S = 1_000_000


@dataclass(frozen=True)
class Hint:
    """A provider asking for the screen to refresh every `seconds`, until `until` (when its key expires)."""

    seconds: int
    until: datetime


def clock() -> datetime:
    """Return the server's time, which every sleep is worked out from (the tests freeze it)."""
    return datetime.now(UTC)


def poll_interval_s(now: datetime, hints: Sequence[Hint]) -> float:
    """Return the seconds between the screen's polls at `now`: half the render interval, or half the shortest hint."""
    return _cycle_s(now, hints) / POLLS_PER_RENDER


def sleep_s(now: datetime, hints: Sequence[Hint]) -> int:
    """How long the screen should sleep before its next poll, from the server's clock.

    It's the time until LEAD_S after the next boundary of poll_interval_s (counted from the epoch), or of the one after
    if that's under MIN_SLEEP_S away, rounded up to a whole second so the screen never wakes early. `now` must be
    timezone-aware.
    """
    period = _cycle_s(now, hints) * _US_PER_S // POLLS_PER_RENDER
    lead, shortest = LEAD_S * _US_PER_S, MIN_SLEEP_S * _US_PER_S
    # In whole microseconds, so a boundary is exact.
    at = (now - _EPOCH) // _ONE_US
    slot = ((at - lead) // period + 1) * period + lead
    if slot - at < shortest:
        slot += period
    return -(-(slot - at) // _US_PER_S)


def _cycle_s(now: datetime, hints: Sequence[Hint]) -> int:
    """Return the shortest live hint, kept in HINT_FLOOR_S..RENDER_INTERVAL_S; RENDER_INTERVAL_S if none is live."""
    live = [hint.seconds for hint in hints if hint.until > now]
    if not live:
        return RENDER_INTERVAL_S
    return min(max(min(live), HINT_FLOOR_S), RENDER_INTERVAL_S)


def key(source: str, item_id: str) -> str:
    """Return the key where `source` asks for a faster refresh while `item_id` is bursty (pi-dashboard's keys.pace).

    For example `v1:pace:developer:<device id>`, which developer_provider.pace_key writes.
    """
    return f"v1:pace:{source}:{item_id}"


def hint_keys(config: DeviceConfig | None) -> list[str]:
    """Return the pace keys of the providers behind the screen's own panels.

    Today that's only the developer provider's, while the screen's developer panel is on.
    """
    if config is None or not config.developer_enabled:
        return []
    return [key("developer", config.device_id)]


def screen_hints(device_id: str, now: datetime) -> list[Hint]:
    """Return the hints for the screen `device_id`: its pace keys' in Redis.

    Live or lapsed: sleep_s tells. Redis being unreachable only loses the hints (logged).
    """
    return _stored_hints(hint_keys(db.session.get(DeviceConfig, device_id)), now)


def retry_after(device_id: str) -> int:
    """Return how long the screen `device_id` should sleep after this poll."""
    now = clock()
    return sleep_s(now, screen_hints(device_id, now))


def _stored_hints(names: list[str], now: datetime) -> list[Hint]:
    """Read the pace keys `names` and their TTLs, in one round trip; return each as a Hint expiring with its key."""
    if not names:
        return []
    redis: Redis = current_app.extensions["redis"]
    try:
        with redis.pipeline(transaction=False) as pipe:
            for name in names:
                pipe.get(name)
                pipe.pttl(name)
            replies = pipe.execute()
    except RedisError as error:
        logger.warning("Couldn't read the screen's pace hints from Redis (%s)", type(error).__name__)
        return []
    hints = []
    for value, ttl_ms in batched(replies, 2, strict=True):
        seconds = _seconds(value)
        # A key with no TTL (-1) would never lapse, so it isn't a hint; -2 is a key that's gone.
        if seconds is not None and isinstance(ttl_ms, int) and ttl_ms > 0:
            hints.append(Hint(seconds, now + timedelta(milliseconds=ttl_ms)))
    return hints


def _seconds(value: object) -> int | None:
    """Return `value` as a positive whole number of seconds, or None if it isn't one."""
    if not isinstance(value, str) or not (text := value.strip()).isascii() or not text.isdigit():
        return None
    seconds = int(text)
    return seconds if seconds > 0 else None
