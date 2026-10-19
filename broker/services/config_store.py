"""Each screen's config in the store: `v1:config:<device id>`, which the render pool draws the screen from.

SQLite stays the source of truth; Redis holds a copy of the screen's config (renderer_config.for_store: the countdown
API contract's ConfigResponse, less its secrets), which the render pool reads, so it never needs the broker to be up.

The copies have no TTL. They're rebuilt from the database (publish_all) on every config save, as the broker starts
(gunicorn.conf.py), every RESYNC_S in each broker process (start_resync, also from gunicorn.conf.py) and by `flask
config publish`, so one Redis lost comes back within a minute; and one copy at a time (publish) when a device's pairing
changes. A device that should have no copy (not paired, a pairing code to show, deleted) has its key removed. Only
what changed is written, and those devices get a render job (services/render_jobs.py) after the write, so the pool
draws the change at once. Either way, the render pool's list of screens (services/render_screens.py) is rewritten with
them.

Under `maxmemory-policy volatile-lru` Redis evicts only keys with a TTL, so it never evicts these, and they count
against the 128 MB for good: a copy is about 700 bytes of JSON (the store contract's config_store.json), about 1 KB with
Redis's own overhead, so a thousand screens take about 1 MB.
"""

import logging
import threading

from countdown_api import keys as store_keys
from flask import Flask, current_app
from redis import Redis, RedisError
from sqlalchemy import select

from broker.db import db
from broker.models import Device
from broker.services import render_jobs, render_screens, renderer_config

logger = logging.getLogger(__name__)

PREFIX = store_keys.CONFIG_PREFIX
# How often each broker process rewrites the copies, and the render pool's screens, from the database anyway (resync).
RESYNC_S = 60


def key(device_id: str) -> str:
    """Return the key of a device's config in the store, e.g. `v1:config:a1b2c3d4e5f6` (the store contract's)."""
    return store_keys.config(device_id)


def publish(device_id: str) -> None:
    """Bring one device's copy up to date with the database: write it, or remove it if it should have none.

    It gets a render job if that changed anything. Needs an app context; an unreachable Redis is logged, not raised.
    """
    device = db.session.get(Device, device_id)
    _queue(_write({key(device_id): _stored(device) if device is not None else None}))


def publish_all() -> None:
    """Rebuild every device's copy from the database, and remove those of devices that should have none.

    Queues a render only of the screens whose copy changed. Needs an app context; an unreachable Redis is logged, not
    raised.
    """
    wanted = {key(device.device_id): _stored(device) for device in db.session.scalars(select(Device))}
    _queue(_write(wanted, sweep=True))


def resync_forever(app: Flask, stop: threading.Event, interval_s: float = RESYNC_S) -> None:
    """Every `interval_s`, until `stop` is set, rewrite every copy and the render pool's screens from the database.

    publish_all() writes only what differs, so this costs a read of each when nothing has changed. It's what brings
    back a Redis that restarted (it keeps nothing on disk), and lists a screen in the pool once it's seen again after
    going quiet. A failure is logged and the next round tries again.
    """
    while not stop.wait(interval_s):
        try:
            with app.app_context():
                publish_all()
        except Exception:
            # The database, say: the thread must outlive it, or nothing would rewrite the store until a restart.
            logger.exception("Couldn't rewrite the screens' configs from the database; trying again in %ss", interval_s)


def start_resync(app: Flask) -> threading.Thread:
    """Start resync_forever on `app` in a daemon thread of its own (it ends with the process), and return the thread.

    gunicorn.conf.py starts one in each broker worker; more than one at a time is harmless, as each writes the same.
    """
    thread = threading.Thread(target=resync_forever, args=(app, threading.Event()), name="config-resync", daemon=True)
    thread.start()
    return thread


def _stored(device: Device) -> str | None:
    """Return the JSON of the device's copy, or None if the store is to keep none for it."""
    stored = renderer_config.for_store(device)
    return stored.model_dump_json() if stored is not None else None


def _queue(changed: list[str]) -> None:
    """After the copies are written: rewrite the render pool's screens, then queue a render of each changed screen.

    In that order, so a screen that has just left the pool (unpaired, forgotten) gets no job, and one that has just
    joined it gets one (render_screens.write queues it, after its copy is written). Logged, never raised.
    """
    render_screens.write()
    render_jobs.enqueue(current_app.extensions["redis"], changed)


def _write(wanted: dict[str, str | None], *, sweep: bool = False) -> list[str]:
    """Make the keys in `wanted` hold their values (None: no key); return the ids of the devices whose key changed.

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
            return []
        with redis.pipeline(transaction=True) as pipe:
            for name, value in changed.items():
                if value is None:
                    pipe.delete(name)
                else:
                    pipe.set(name, value)
            pipe.execute()
        return [name.removeprefix(PREFIX) for name in changed]
    except RedisError as error:
        # The pool draws the copy it has (or nothing, without one) until the next rebuild, within RESYNC_S.
        logger.warning("Couldn't write the screens' configs to Redis (%s)", type(error).__name__)
        return []
