"""A device's live pairing code in the store, `v1:pairing:<device id>`, for a screen the render pool draws.

The copy of a screen's config in the store (services/config_store.py) never carries a pairing code, and there's none
at all while the device has a code to show. A render in the pool (pi-dashboard's docs/stateless-renderers.md) reads
only the store, so while a code is live the broker keeps this key: the screen's config (the contract's
ConfigResponse, made by renderer_config.build) with the code in it. That's everything the pool needs to draw the
pairing screen in the screen's own size and language; the screen stays in the pool's list while it has a live code
(services/render_screens.py). The store contract's pairing_key.json pins the shape (pi-dashboard's store.pairing reads
it).

Plaintext, by Nikolai's decision (stateless-renderers.md, "The pairing screen"): every Redis reader on the host can
see a live code for its ten minutes, the same trust as the config. Its TTL is the code's own expiry, so the key lapses
with the code by itself; it goes sooner when the code does (used, replaced, or the device paired, unpaired or
forgotten: pairing.py, device_keys.forget). An issued code is written over the last one's key.

Redis that can't be asked never stops a code being issued or used: the error's type is logged (never the code, nor a
failed command's text, which quotes the value), and the key lapses by its TTL or is never there, so a pooled screen
just doesn't show the code. Under `volatile-lru` Redis may evict the key too, with the same result.
"""

import logging
from typing import TYPE_CHECKING

from countdown_api import keys as store_keys
from flask import current_app
from redis import Redis, RedisError

from broker.db import db
from broker.models import DeviceConfig

if TYPE_CHECKING:
    from datetime import datetime

logger = logging.getLogger(__name__)

PREFIX = store_keys.PAIRING_PREFIX


def key(device_id: str) -> str:
    """Return the key of a device's live pairing code, e.g. `v1:pairing:a1b2c3d4e5f6` (the store contract's)."""
    return store_keys.pairing(device_id)


def write(device_id: str, code: str, expires_at: datetime) -> None:
    """Store the device's config with `code` in it, expiring when the code does (`expires_at`, aware).

    Needs an app context, after the code's commit. Nothing is stored for a device with no settings (there's no screen
    to draw); a Redis that can't be asked is logged, not raised.
    """
    # Here, not at the top: renderer_config imports pairing, which imports this module.
    from broker.services import renderer_config  # noqa: PLC0415

    config = db.session.get(DeviceConfig, device_id)
    if config is None:
        return
    value = renderer_config.build(config, pairing_code=code).model_dump_json()
    redis: Redis = current_app.extensions["redis"]
    try:
        # PXAT: the code's own expiry, to the millisecond, however long the commit took.
        redis.set(key(device_id), value, pxat=int(expires_at.timestamp() * 1000))
    except RedisError as error:
        logger.warning("Couldn't write a pairing code to Redis (%s); pooled, it won't show", type(error).__name__)


def delete(device_id: str) -> None:
    """Remove the device's pairing key. Needs an app context; a Redis that can't be asked is logged, not raised."""
    redis: Redis = current_app.extensions["redis"]
    try:
        redis.delete(key(device_id))
    except RedisError as error:
        logger.warning("Couldn't delete a pairing code from Redis (%s); it lapses by its TTL", type(error).__name__)
