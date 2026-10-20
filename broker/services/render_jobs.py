"""Jobs for the render pool: asking for a screen to be drawn now, rather than at the next minute's tick.

pi-dashboard's render pool (its docs/stateless-renderers.md, "What queues a render") draws a screen when a
job for it is on `v1:render:jobs`. Its ticker queues every pooled screen on the minute; the broker and its providers
queue one sooner when what it shows has just changed (the developer provider on each cycle of a release, so a pooled
screen follows the release at the burst's pace).

`enqueue` runs the store contract's enqueue script (countdown_api.scripts.ENQUEUE, which the pool runs too):
`SET v1:render:queued:<id> NX` with QUEUED_TTL_MS, and only if that set it, `LPUSH v1:render:jobs <id>`. So a screen
is in the queue at most once however many ask.

Only the screens the pool renders are queued: those in `v1:render:screens` (services/render_screens.py), read first.
The pool's worker would drop a job for any other, and with no pool running nothing would take jobs off the list, which
has no TTL; so a screen the pool doesn't draw gets nothing written.

A job is only ever a hint: a Redis that can't be asked is logged by the error's type (never a failed command's text,
which quotes its keys and values) and not raised, and the next minute's tick draws the screen anyway.
"""

import logging
from typing import TYPE_CHECKING

from countdown_api import keys as store_keys
from countdown_api import scripts
from redis import RedisError

if TYPE_CHECKING:
    from collections.abc import Iterable

    from redis import Redis

logger = logging.getLogger(__name__)

# The store contract's (countdown_api.keys), as pi-dashboard's render pool reads them. The job queue, a list of device
# ids:
JOBS = store_keys.RENDER_JOBS
# The screens the pool renders, a hash of device id to slug:
SCREENS = store_keys.RENDER_SCREENS
# The queued flag lasts the ticker's minute, so a worker that died between the pop and the claim keeps the screen out
# of the queue for no longer than that.
QUEUED_TTL_MS = store_keys.RENDER_QUEUED_TTL_MS


def queued_key(device_id: str) -> str:
    """Return the flag that a job for the screen is in the queue, e.g. `v1:render:queued:a1b2c3d4e5f6`.

    The store contract's (countdown_api.keys.render_queued).
    """
    return store_keys.render_queued(device_id)


# The store contract's enqueue script (countdown-api's lua/enqueue.lua), which pi-dashboard's render pool runs too.
# KEYS: queued, jobs. ARGV: the device id, the queued flag's TTL. Returns 1 if it queued the screen, 0 if it was queued
# already.
_ENQUEUE = scripts.ENQUEUE.source


def enqueue(client: Redis, device_ids: Iterable[str]) -> int:
    """Queue a render of each screen the pool renders and that isn't queued already; return how many this queued.

    Two round trips: which of them the pool renders, then the jobs. A Redis that can't be asked is logged (its error's
    type) and counts as none queued: never raised.
    """
    device_ids = list(device_ids)
    if not device_ids:
        return 0
    script = client.register_script(_ENQUEUE)
    try:
        pooled = [
            device_id for device_id, slug in zip(device_ids, client.hmget(SCREENS, device_ids), strict=True) if slug
        ]
        if not pooled:
            return 0
        with client.pipeline(transaction=False) as pipe:
            for device_id in pooled:
                script(keys=[queued_key(device_id), JOBS], args=[device_id, QUEUED_TTL_MS], client=pipe)
            return sum(bool(queued) for queued in pipe.execute())
    except RedisError as error:
        logger.warning(
            "Couldn't queue %d render jobs (%s); the next tick draws them", len(device_ids), type(error).__name__
        )
        return 0
