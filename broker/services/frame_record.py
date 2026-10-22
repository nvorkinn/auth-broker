"""Each screen's latest frame in the store, `v1:frame:<device id>`: the render pool writes it, and the broker serves it.

pi-dashboard's render pool (its docs/stateless-renderers.md) writes each new picture of a screen to its record in Redis
(its countdown_core.store.frame_record), and that is the only copy there is: the broker keeps none in its database.
The screen is served the record (routes/frames.py), and the config page's preview shows the same.

The record is a Redis hash, so the small fields can be read without the frame:

- `frame`: the panel's 48,000 bytes (800x480, a bit a pixel): raw bytes, so it's read with a client that doesn't
  decode values, `app.extensions["redis_bytes"]`;
- `etag`: the frame's 8-byte BLAKE2b in hex, the screen's ETag;
- `rendered_at`: when the pool first wrote this picture, ISO 8601 with its UTC offset.

A screen that has the record's picture gets its 304 from `etag` alone (`head`); only one that doesn't costs reading
the 48 KB (`frame_of`). A record that isn't what the store contract's frame_record.json pins (a field missing, an etag
that isn't 16 hex digits, a time without its offset, a frame that isn't 48,000 bytes hashing to its etag) is no
record. The pool gives each record a TTL of a day from its last change (`volatile-lru` may evict it before then), and
writes it again at its next render; meanwhile, as after a Redis restart, the screen is told there's no frame yet and
asks again shortly, keeping the picture it has. The broker deletes a device's record when it forgets the device, or
unpairs it with its config (`forget`).

Nothing logged says what a frame shows. A Redis that can't be asked is logged by the error's type, and counts as no
record.
"""

import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import blake2b
from typing import TYPE_CHECKING, cast

from countdown_api import keys as store_keys
from flask import current_app
from redis import RedisError

if TYPE_CHECKING:
    from redis import Redis

logger = logging.getLogger(__name__)

PREFIX = store_keys.FRAME_PREFIX
# The hash's fields.
FRAME, ETAG, RENDERED_AT = "frame", "etag", "rendered_at"
# 1-bit packed 800x480.
FRAME_BYTES = 800 * 480 // 8
_ETAG = re.compile(r"[0-9a-f]{16}")


@dataclass(frozen=True)
class StoredFrame:
    """A screen's record: its etag, when it was drawn and, if it was read, the frame itself (else None)."""

    etag: str
    rendered_at: datetime
    frame: bytes | None = None


def key(device_id: str) -> str:
    """Return the key of a screen's record, e.g. `v1:frame:a1b2c3d4e5f6` (the store contract's)."""
    return store_keys.frame(device_id)


def head(device_id: str) -> StoredFrame | None:
    """Return the record's etag and time, without its frame; None if there's none, or it can't be read."""
    values = _hmget(device_id, ETAG, RENDERED_AT)
    return _stored(*values) if values is not None else None


def read(device_id: str) -> StoredFrame | None:
    """Return the whole record, frame and all; None if there's none, or it can't be read."""
    values = _hmget(device_id, ETAG, RENDERED_AT, FRAME)
    if values is None:
        return None
    stored = _stored(*values[:2])
    if stored is None or not _is_frame(values[2], stored.etag):
        return None
    return StoredFrame(stored.etag, stored.rendered_at, values[2])


def frame_of(device_id: str, etag: str) -> bytes | None:
    """Return the record's frame if it's still the picture hashed `etag`; None if it went, changed or can't be read."""
    values = _hmget(device_id, FRAME)
    if values is None or not _is_frame(values[0], etag):
        return None
    return values[0]


def forget(device_id: str) -> None:
    """Delete a screen's record. Needs an app context; a Redis that can't be asked is logged."""
    try:
        _redis().delete(key(device_id))
    except RedisError as error:
        # It lapses within a day by itself; a forgotten device can't be served it, and an unpaired one is drawn again.
        logger.warning("Couldn't delete a screen's frame from Redis (%s)", type(error).__name__)


def _redis() -> Redis:
    return current_app.extensions["redis_bytes"]


def _hmget(device_id: str, *fields: str) -> list[bytes | None] | None:
    """Return the record's `fields` as bytes (None for one it hasn't); None if Redis can't be asked (logged)."""
    try:
        # A client without decode_responses, so bytes; redis-py types it as either.
        return cast("list[bytes | None]", _redis().hmget(key(device_id), list(fields)))
    except RedisError as error:
        logger.warning("Couldn't read a screen's frame from Redis (%s)", type(error).__name__)
        return None


def _stored(etag: bytes | None, rendered_at: bytes | None) -> StoredFrame | None:
    """Return the record these fields make; None if there's none, or they aren't what the pool writes."""
    if etag is None or rendered_at is None:
        return None
    try:
        etag_text, at = etag.decode("ascii"), datetime.fromisoformat(rendered_at.decode("ascii"))
    except ValueError:  # UnicodeDecodeError is one
        return None
    if not _ETAG.fullmatch(etag_text) or at.tzinfo is None:
        return None
    return StoredFrame(etag_text, at.astimezone(UTC))


def _is_frame(frame: bytes | None, etag: str) -> bool:
    return frame is not None and len(frame) == FRAME_BYTES and blake2b(frame, digest_size=8).hexdigest() == etag
