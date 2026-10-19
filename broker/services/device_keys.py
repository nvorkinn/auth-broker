"""Removing a device's own data from the store when its accounts change or it changes hands.

The providers write each device's data under keys of its own (pi-dashboard's store/keys.py has them all):

    v1:energy:<device id>         the glow-provider's Glowmarkt readings (TTL about a day)
    v1:spotify:<device id>        the music provider's player (3 min)
    v1:spotify-top:<device id>    and its top lists (2 h)
    v1:developer:<device id>      the developer provider's GitHub summary and AI plans (15 min)
    v1:pace:developer:<device id> its faster-refresh hint, while a release runs (1 min)

Three other keys are the device's, and are looked after where they're written:

- `v1:config:<device id>` is config_store's: publish() removes it for a device that's unpaired or gone;
- `v1:frame:<device id>`, the screen's latest picture, is the render pool's (services/frame_record.py):
  frame_record.forget() deletes it when a device is forgotten, or unpaired with its config. Otherwise the pool draws
  over it at the render the change queues, and deleting it first would only leave the screen with no frame;
- `v1:pairing:<device id>`, its live pairing code, is services/pairing_key.py's: it lapses with the code, goes when
  the code is used or the device paired, and forget() deletes it too (a device unpaired, handed on or gone has no code
  left: its codes go with the change).

Left alone, each key lasts until its TTL, so an account unlinked, or a screen handed to someone else, could still show
the old account's data until then. So whenever the broker removes or replaces an account it deletes the keys made from
it, and whenever a device is unpaired, gets an owner other than the one it had, or is forgotten, it deletes them all.
Always after the database's commit: the change is the database's, and the providers, which read the database, write
the keys again only from what it says now.

Redis that can't be asked doesn't fail the change: it's logged (the error's type only: a failed pipeline's text quotes
its commands), and the keys lapse by their TTLs.
"""

import logging

from flask import current_app
from redis import Redis, RedisError

from broker.services import pairing_key

logger = logging.getLogger(__name__)


def energy(device_id: str) -> list[str]:
    """Return the keys made from the device's Glowmarkt account (the glow-provider's energy_key)."""
    return [f"v1:energy:{device_id}"]


def music(device_id: str) -> list[str]:
    """Return the keys made from the device's Spotify and Apple Music accounts (the music provider's)."""
    return [f"v1:spotify:{device_id}", f"v1:spotify-top:{device_id}"]


def developer(device_id: str) -> list[str]:
    """Return the keys made from the device's GitHub token and AI plans (the developer provider's)."""
    return [f"v1:developer:{device_id}", f"v1:pace:developer:{device_id}"]


def delete(keys: list[str]) -> None:
    """Delete `keys` from the store. Needs an app context; a Redis that can't be asked is logged, not raised."""
    redis: Redis = current_app.extensions["redis"]
    try:
        with redis.pipeline(transaction=True) as pipe:
            for key in keys:
                pipe.delete(key)
            pipe.execute()
    except RedisError as error:
        logger.warning("Couldn't delete a device's data from Redis (%s); it lapses by its TTL", type(error).__name__)


def forget(device_id: str) -> None:
    """Delete the providers' keys for the device, and its pairing code's: for one unpaired, handed on or gone."""
    delete([*energy(device_id), *music(device_id), *developer(device_id), pairing_key.key(device_id)])
