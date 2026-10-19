"""The screens the render pool draws: `v1:render:screens`, written by the broker from its database.

pi-dashboard's render pool (its docs/stateless-renderers.md, "The pool, the queue and the render function") draws the
screens listed in `v1:render:screens`, a hash of device id to slug (the slug names the screen: it's the `device` label
of the pool's metrics), and drops a job for any other. The broker is the only one that writes it.

A screen is listed while the pool has something to draw for it:

- it has been named (services/assignments.py), so it has a slug;
- it has settings (a device_config row);
- it's paired, so its config is in the store (services/config_store.py), or it has a live pairing code, whose key
  (services/pairing_key.py) the pool draws the pairing screen from;
- it has been seen in the last assignments.SUSPEND_AFTER (assignments.is_quiet). A screen that's back is listed
  again within config_store.RESYNC_S.

tests/fixtures/render_screens.json pins the hash's shape (pi-dashboard's countdown_core.store.render_queue reads it:
`screens()`, and the claim script's HGET).

The hash is written whole, only where it differs (HDEL and HSET in one transaction), whenever the configs are
(config_store.publish and publish_all call write()): on a config save, pairing, unpairing, forgetting and a pairing code
issued, as the broker starts, by `flask config publish`, and every config_store.RESYNC_S, so a Redis that has restarted
(it keeps nothing on disk) has its screens back within a minute; and when a screen is named, which gives it its slug. A
screen newly listed gets a render job at once (services/render_jobs.py), rather than waiting for the ticker's minute.

Like a render job, this is never worth failing a request for: a Redis that can't be asked is logged by the error's type
(never a failed command's text, which quotes its keys and values) and not raised, and the next write puts it right.
"""

import logging
from datetime import UTC, datetime

from flask import current_app
from redis import Redis, RedisError
from sqlalchemy import select

from broker.db import db
from broker.models import PairingCode, RendererAssignment
from broker.services import assignments, render_jobs

logger = logging.getLogger(__name__)

# pi-dashboard's keys.RENDER_SCREENS: the screens the pool renders, a hash of device id to slug.
SCREENS = render_jobs.SCREENS


def listed(now: datetime) -> dict[str, str]:
    """Return the screens the pool is to draw as of `now`: their slugs by device id, from the database."""
    coded = set(db.session.scalars(select(PairingCode.device_id).where(PairingCode.expires_at > now)))
    screens: dict[str, str] = {}
    for assignment in db.session.scalars(select(RendererAssignment)):
        device = assignment.device
        if (
            device is None
            or device.config is None
            or (device.paired_at is None and device.device_id not in coded)
            or not assignments.SLUG_RE.match(assignment.slug)
            or assignments.is_quiet(assignment, now)
        ):
            continue
        screens[device.device_id] = assignment.slug
    return screens


def write() -> int:
    """Make `v1:render:screens` list exactly the screens the pool is to draw, and queue a render of each newly listed.

    Returns how many screens it newly listed. Needs an app context; an unreachable Redis is logged, not raised.
    """
    redis: Redis = current_app.extensions["redis"]
    wanted = listed(datetime.now(UTC))
    try:
        current = redis.hgetall(SCREENS)
        gone = [device_id for device_id in current if device_id not in wanted]
        changed = {device_id: slug for device_id, slug in wanted.items() if current.get(device_id) != slug}
        if not gone and not changed:
            return 0
        with redis.pipeline(transaction=True) as pipe:
            if gone:
                pipe.hdel(SCREENS, *gone)
            if changed:
                pipe.hset(SCREENS, mapping=changed)
            pipe.execute()
    except RedisError as error:
        logger.warning("Couldn't write the render pool's screens to Redis (%s)", type(error).__name__)
        return 0
    added = [device_id for device_id in changed if device_id not in current]
    logger.debug("The render pool's screens: %d listed, %d taken out", len(added), len(gone))
    # After the write: the job is only queued for a screen the hash lists.
    render_jobs.enqueue(redis, added)
    return len(added)
