"""Render jobs for the render pool: asking for a screen to be drawn now, rather than at the next minute's tick.

The pool (pi-dashboard's docs/stateless-renderers.md, "The pool, the queue and the render function") draws a screen
when a job for it comes off `v1:render:jobs`. Its ticker queues every pooled screen on the minute; the broker queues
one when what it draws changes: its config is saved (services/config_store.py, beside the `v1:config:changed` nudge
today's per-screen renderers listen for), or a pairing code is issued or spent (services/pairing.py).

ENQUEUE is pi-dashboard's `countdown_core.store.render_queue._ENQUEUE`, byte for byte: `SET v1:render:queued:<id> NX`
(holding Redis's own time, in Unix ms, for 60 s), and only if that set it, `LPUSH v1:render:jobs <id>`. So a screen is
in the queue at most once however many triggers fire, and the flag lapses by itself if a worker dies between the pop
and the claim. tests/fixtures/render_queue.json pins the script's SHA1, the keys and the TTL; both repositories' tests
check their own copy against it.

A job for a screen the pool doesn't render (one that still has its own renderer, or none at all) is dropped by the
worker's claim, so queueing for any screen is safe. Redis that can't be asked never fails what queued the job: the
error's type is logged (never its text), and the screen is drawn at the next tick.
"""

import logging
from typing import TYPE_CHECKING

from flask import current_app
from redis import Redis, RedisError

if TYPE_CHECKING:
    from collections.abc import Iterable

logger = logging.getLogger(__name__)

JOBS = "v1:render:jobs"
QUEUED_PREFIX = "v1:render:queued:"
# The queued flag's TTL: a minute, the ticker's interval (pi-dashboard's render_queue.QUEUED_TTL_MS).
QUEUED_TTL_MS = 60_000

# Redis's clock, in Unix milliseconds, as a string: the seconds and the milliseconds, put together as text so that the
# number doesn't go through Lua's floating point.
_NOW_MS = "local t = redis.call('TIME')\nlocal now_ms = t[1] .. string.format('%03d', math.floor(t[2] / 1000))\n"

# KEYS: queued, jobs. ARGV: the device id, the queued flag's TTL. Returns 1 if it queued the screen, 0 if it was queued
# already.
ENQUEUE = (
    _NOW_MS
    + """
if redis.call('SET', KEYS[1], now_ms, 'NX', 'PX', ARGV[2]) then
  redis.call('LPUSH', KEYS[2], ARGV[1])
  return 1
end
return 0
"""
)


def queued_key(device_id: str) -> str:
    """Return the flag that a job for the screen is queued, e.g. `v1:render:queued:a1b2c3d4e5f6` (render_queued)."""
    return f"{QUEUED_PREFIX}{device_id}"


def enqueue(client: Redis, device_ids: Iterable[str]) -> int:
    """Queue a render of each screen not queued already, in one round trip; return how many this queued.

    For a caller with its own client (a provider); raises what Redis raises.
    """
    device_ids = list(dict.fromkeys(device_ids))
    if not device_ids:
        return 0
    script = client.register_script(ENQUEUE)
    with client.pipeline(transaction=False) as pipe:
        for device_id in device_ids:
            script(keys=[queued_key(device_id), JOBS], args=[device_id, QUEUED_TTL_MS], client=pipe)
        return sum(bool(queued) for queued in pipe.execute())


def request(*device_ids: str) -> None:
    """Ask the pool to draw these screens now, on the broker's Redis. Needs an app context.

    A Redis that can't be asked is logged, not raised: the screens are drawn at the next tick.
    """
    redis: Redis = current_app.extensions["redis"]
    try:
        enqueue(redis, device_ids)
    except RedisError as error:
        logger.warning("Couldn't queue a render in Redis (%s); the next tick draws it", type(error).__name__)
