"""Registering devices. A screen becomes a device straight away: it exists from its first call, so its
owner can pair with it and name its renderer (services/assignments.py) before any renderer is running.
A renderer started by the orchestrator claims its assignment's device with the assignment's claim token.

Secrets arrive already hashed (auth.hash_secret); the hash is what identifies the caller here."""

import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, or_, select

from ..db import db
from ..models import Device, DeviceConfig, RendererAssignment
from . import pairing

# Bound on what anyone can add: how many screens may sit unpaired at once, and for how long before
# they have to register again.
MAX_UNPAIRED_SCREENS = 20
UNPAIRED_TTL = timedelta(hours=24)


@dataclass(frozen=True)
class Registered:
    """The device a secret belongs to, or role_conflict if the secret is already registered as the other
    kind of client. `created` is whether this call made the registration, rather than repeating it."""

    device_id: str | None = None
    role_conflict: bool = False
    created: bool = False


def _device_with(secret_hash: str) -> Device | None:
    return db.session.scalar(
        select(Device).where(or_(Device.renderer_secret_hash == secret_hash, Device.display_secret_hash == secret_hash))
    )


def register_screen(secret_hash: str) -> Registered | None:
    """A screen becomes a device at once, so its owner can pair with it and name its renderer. A repeat
    of a registered secret finds its device. Returns None when too many screens are already waiting to
    be paired."""
    device = _device_with(secret_hash)
    if device is not None:
        if device.display_secret_hash != secret_hash:
            return Registered(role_conflict=True)
        return Registered(device_id=device.device_id)

    _drop_stale_screens(datetime.now(UTC))
    if _unpaired_screens() >= MAX_UNPAIRED_SCREENS:
        db.session.commit()
        return None
    device_id = _new_device(display_secret_hash=secret_hash)
    db.session.commit()
    return Registered(device_id=device_id, created=True)


def claim(secret_hash: str, claim_token: str) -> Registered | None:
    """Gives the device an assignment was made for its renderer's secret, spending the assignment's
    claim token. A repeat of a claimed secret finds its device. None for a token that is unknown, spent,
    or whose device already has a renderer."""
    device = _device_with(secret_hash)
    if device is not None:
        if device.renderer_secret_hash != secret_hash:
            return Registered(role_conflict=True)
        return Registered(device_id=device.device_id)

    assignment = db.session.scalar(select(RendererAssignment).where(RendererAssignment.claim_token == claim_token))
    device = assignment.device if assignment else None
    if device is None or device.renderer_secret_hash is not None:
        return None
    device.renderer_secret_hash = secret_hash
    assignment.claim_token = None
    db.session.commit()
    return Registered(device_id=device.device_id, created=True)


def _unpaired_screens():
    return db.session.scalar(
        select(func.count())
        .select_from(Device)
        .where(Device.renderer_secret_hash.is_(None), Device.paired_at.is_(None), ~Device.assignment.has())
    )


def _drop_stale_screens(now: datetime) -> None:
    """Forgets screens that registered, were never paired and have waited longer than allowed. They
    register again with their next call (a 401 tells them to)."""
    stale = db.session.scalars(
        select(Device).where(
            Device.renderer_secret_hash.is_(None),
            Device.paired_at.is_(None),
            ~Device.assignment.has(),
            Device.created_at < now - UNPAIRED_TTL,
        )
    ).all()
    for device in stale:
        db.session.delete(device)


def _new_device(display_secret_hash: str) -> str:
    device_id = secrets.token_hex(6)
    # A screen's secret makes the token that sends its owner's browser to /pair.
    device = Device(
        device_id=device_id,
        created_at=datetime.now(UTC),
        config=DeviceConfig(),
        pairing_token=pairing.token_for(display_secret_hash),
        display_secret_hash=display_secret_hash,
    )
    db.session.add(device)
    return device_id
