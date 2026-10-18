"""Each screen's config in the store: `v1:config:<device id>`, and a nudge on `v1:config:changed` when it changes.

SQLite stays the source of truth; Redis holds a copy of what GET /api/config answers each renderer (less its secrets,
see renderer_config.for_store), so the renderers read their config from there and don't need the broker to be up, and
a change reaches a renderer at once: after writing a device's copy, the broker publishes its device id on
`v1:config:changed`, which its renderer listens for. The renderer asks GET /api/config only when there's no copy.

The copies have no TTL. They're rebuilt from the database (publish_all) on every config save, as the broker starts
(gunicorn.conf.py) and by `flask config publish`, so one Redis lost comes back; and one copy at a time (publish) when
a device's pairing changes. A device that should have no copy (not paired, a pairing code to show, deleted) has its
key removed. Only what changed is written, and only those devices are nudged.

Under `maxmemory-policy volatile-lru` Redis evicts only keys with a TTL, so it never evicts these, and they count
against the 128 MB for good: a copy is about 700 bytes of JSON (tests/fixtures/config_store.json), about 1 KB with
Redis's own overhead, so a thousand screens take about 1 MB.
"""

import logging

from flask import current_app
from redis import Redis, RedisError
from sqlalchemy import select

from broker.db import db
from broker.models import Device
from broker.services import renderer_config

logger = logging.getLogger(__name__)

PREFIX = "v1:config:"
# The pub/sub channel the broker publishes a device's id on after changing its copy.
CHANGED = "v1:config:changed"


def key(device_id: str) -> str:
    """Return the key of a device's config in the store, e.g. `v1:config:a1b2c3d4e5f6`."""
    return f"{PREFIX}{device_id}"


def publish(device_id: str) -> None:
    """Bring one device's copy up to date with the database: write it, or remove it if it should have none.

    Its renderer is nudged if that changed anything. Needs an app context; an unreachable Redis is logged, not raised.
    """
    device = db.session.get(Device, device_id)
    _write({key(device_id): _stored(device) if device is not None else None})


def publish_all() -> None:
    """Rebuild every device's copy from the database, and remove those of devices that should have none.

    Nudges only the renderers whose copy changed. Needs an app context; an unreachable Redis is logged, not raised.
    """
    wanted = {key(device.device_id): _stored(device) for device in db.session.scalars(select(Device))}
    _write(wanted, sweep=True)


def _stored(device: Device) -> str | None:
    """Return the JSON of the device's copy, or None if the store is to keep none for it."""
    stored = renderer_config.for_store(device)
    return stored.model_dump_json() if stored is not None else None


def _write(wanted: dict[str, str | None], *, sweep: bool = False) -> None:
    """Make the keys in `wanted` hold their values (None: no key), then nudge each device whose key changed.

    With `sweep`, every other config key in the store is removed too (a device deleted while Redis was away).
    """
    redis: Redis = current_app.extensions["redis"]
    try:
        if sweep:
            for found in redis.scan_iter(match=f"{PREFIX}*", count=1000):
                wanted.setdefault(found, None)
        names = list(wanted)
        current = dict(zip(names, redis.mget(names), strict=True)) if names else {}
        changed = {name: value for name, value in wanted.items() if current[name] != value}
        if not changed:
            return
        # One transaction, the writes before the nudges, so a renderer that's nudged reads the new copy.
        with redis.pipeline(transaction=True) as pipe:
            for name, value in changed.items():
                if value is None:
                    pipe.delete(name)
                else:
                    pipe.set(name, value)
            for name in changed:
                pipe.publish(CHANGED, name.removeprefix(PREFIX))
            pipe.execute()
    except RedisError as error:
        # The renderers keep the copy they have (or ask the broker, without one) until the next rebuild.
        logger.warning("Couldn't write the screens' configs to Redis (%s)", type(error).__name__)
