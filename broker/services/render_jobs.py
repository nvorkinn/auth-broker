"""Jobs for the render pool: asking for a screen to be drawn now, rather than at the next minute's tick.

pi-dashboard's render pool (its docs/stateless-renderers.md, "Config saves and bursts push jobs") draws a screen when a
job for it is on `v1:render:jobs`. Its ticker queues every pooled screen on the minute; the broker queues one sooner
when what it shows has just changed: its config is saved (services/config_store.py, beside the `v1:config:changed`
nudge today's per-screen renderers listen for), or a pairing code is issued or spent (services/pairing.py).

`enqueue` runs the pool's own enqueue script (countdown_core.store.render_queue's `_ENQUEUE`, copied here byte for
byte): `SET v1:render:queued:<id> NX` with QUEUED_TTL_MS, and only if that set it, `LPUSH v1:render:jobs <id>`. So a
screen is in the queue at most once however many ask. tests/fixtures/render_queue.json pins the script's SHA1, its keys
and the TTL; pi-dashboard's tests check its own script against the same file.

Only the screens the pool renders are queued: those in `v1:render:screens` (the orchestrator's hash of them), read
first. The pool's worker would drop a job for any other (one that still has its own renderer), but until a pool runs
nothing takes jobs off the list at all, and it has no TTL, so jobs for every screen would pile up in it. So this is safe
before, during and after the migration: with no pool, or none of the screens in it, nothing is written.

A job is only ever a hint: a Redis that can't be asked is logged by the error's type (never a failed command's text,
which quotes its keys and values) and not raised, and the next minute's tick draws the screen anyway.
"""

import logging
from typing import TYPE_CHECKING

from flask import current_app
from redis import RedisError

if TYPE_CHECKING:
    from collections.abc import Iterable

    from redis import Redis

logger = logging.getLogger(__name__)

# pi-dashboard's keys.RENDER_JOBS: the job queue, a list of device ids.
JOBS = "v1:render:jobs"
# pi-dashboard's keys.RENDER_SCREENS: the screens the pool renders, a hash of device id to slug.
SCREENS = "v1:render:screens"
# pi-dashboard's render_queue.QUEUED_TTL_MS: the queued flag lasts the ticker's minute, so a worker that died between
# the pop and the claim keeps the screen out of the queue for no longer than that.
QUEUED_TTL_MS = 60_000


def queued_key(device_id: str) -> str:
    """Return the flag that a job for the screen is in the queue, e.g. `v1:render:queued:a1b2c3d4e5f6`.

    pi-dashboard's keys.render_queued.
    """
    return f"v1:render:queued:{device_id}"


# Copied byte for byte from pi-dashboard's countdown_core/store/render_queue.py (tests/services/test_render_jobs.py
# checks its SHA1 against tests/fixtures/render_queue.json). Redis's clock, in Unix milliseconds, as a string: the
# seconds and the milliseconds, put together as text so that the number doesn't go through Lua's floating point.
_NOW_MS = "local t = redis.call('TIME')\nlocal now_ms = t[1] .. string.format('%03d', math.floor(t[2] / 1000))\n"

# KEYS: queued, jobs. ARGV: the device id, the queued flag's TTL. Returns 1 if it queued the screen, 0 if it was queued
# already.
_ENQUEUE = (
    _NOW_MS
    + """
if redis.call('SET', KEYS[1], now_ms, 'NX', 'PX', ARGV[2]) then
  redis.call('LPUSH', KEYS[2], ARGV[1])
  return 1
end
return 0
"""
)


def enqueue(client: Redis, device_ids: Iterable[str]) -> int:
    """Queue a render of each screen the pool renders and that isn't queued already; return how many this queued.

    Two round trips: which of them the pool renders, then the jobs. A Redis that can't be asked is logged (its error's
    type) and counts as none queued: never raised.
    """
    device_ids = list(dict.fromkeys(device_ids))
    if not device_ids:
        return 0
    try:
        pooled = [
            device_id for device_id, slug in zip(device_ids, client.hmget(SCREENS, device_ids), strict=True) if slug
        ]
        if not pooled:
            return 0
        script = client.register_script(_ENQUEUE)
        with client.pipeline(transaction=False) as pipe:
            for device_id in pooled:
                script(keys=[queued_key(device_id), JOBS], args=[device_id, QUEUED_TTL_MS], client=pipe)
            return sum(bool(queued) for queued in pipe.execute())
    except RedisError as error:
        logger.warning(
            "Couldn't queue %d render jobs (%s); the next tick draws them", len(device_ids), type(error).__name__
        )
        return 0


def request(*device_ids: str) -> None:
    """Ask the pool to draw these screens now, on the broker's Redis (`enqueue`). Needs an app context; never raises."""
    enqueue(current_app.extensions["redis"], device_ids)
