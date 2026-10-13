"""Registering devices. A screen becomes a device straight away: it exists from its first call, so its
owner can pair with it and name its renderer
(services/assignments.py) before any renderer is running. A renderer started by hand, with no claim
token, waits in the pending pool until a screen without a renderer is there to take it.

Secrets arrive already hashed (auth.hash_secret); the hash is what identifies the caller here."""

import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, or_, select

from ..db import db
from ..models import Device, DeviceConfig, PendingRegistration, RendererAssignment
from . import pairing

# Bounds on what anyone can add: how many renderers may wait in the pool and how many screens may sit
# unpaired at once, and for how long before they have to register again.
MAX_PENDING = 20
MAX_UNPAIRED_SCREENS = 20
PENDING_TTL = timedelta(hours=24)


@dataclass(frozen=True)
class Registered:
    """Where a secret stands: matched into a device (device_id set), waiting in the pool, or, if
    role_conflict, already registered under the other role."""

    device_id: str | None = None
    role_conflict: bool = False

    @property
    def waiting(self) -> bool:
        return self.device_id is None and not self.role_conflict


def existing(role: str, secret_hash: str) -> Registered | None:
    """What a secret is already registered as, if anything, so registering is safe to repeat."""
    device = db.session.scalar(
        select(Device).where(or_(Device.renderer_secret_hash == secret_hash, Device.display_secret_hash == secret_hash))
    )
    if device is not None:
        if getattr(device, f"{role}_secret_hash") != secret_hash:
            return Registered(role_conflict=True)
        return Registered(device_id=device.device_id)

    pending = db.session.get(PendingRegistration, secret_hash)
    if pending is not None:
        return Registered(role_conflict=pending.role != role)
    return None


def register_screen(secret_hash: str, width: int, height: int) -> Registered | None:
    """A screen becomes a device at once, so its owner can pair with it and name its renderer. If a
    renderer is waiting in the pool, the longest-waiting one is taken into the device. Returns None
    when too many screens are already waiting to be paired.

    Every step happens in one transaction that starts with a write, so no other registration can
    run in between (see register_renderer)."""
    now = datetime.now(UTC)
    db.session.execute(delete(PendingRegistration).where(PendingRegistration.created_at < now - PENDING_TTL))
    _drop_stale_screens(now)
    if _unpaired_screens() >= MAX_UNPAIRED_SCREENS:
        db.session.commit()
        return None

    oldest = select(PendingRegistration.secret_hash).order_by(PendingRegistration.created_at).limit(1).scalar_subquery()
    renderer_hash = db.session.scalar(
        delete(PendingRegistration)
        .where(PendingRegistration.secret_hash == oldest)
        .returning(PendingRegistration.secret_hash)
    )
    device_id = _new_device(width, height, display_secret_hash=secret_hash, renderer_secret_hash=renderer_hash)
    db.session.commit()
    return Registered(device_id=device_id)


def register_renderer(secret_hash: str) -> Registered | None:
    """Adds a renderer with no claim token to the pool and matches it with the longest-waiting screen
    that has no renderer and no assignment, if there is one. Returns None when the pool is full.

    Every step happens in one transaction that starts with a write. SQLite allows one writer at a
    time across every thread and process, so from that first write until the commit no other
    registration can run; two callers can never claim the same screen."""
    now = datetime.now(UTC)
    db.session.execute(delete(PendingRegistration).where(PendingRegistration.created_at < now - PENDING_TTL))
    if db.session.scalar(select(func.count()).select_from(PendingRegistration)) >= MAX_PENDING:
        db.session.commit()
        return None

    screen = db.session.scalar(
        select(Device)
        .where(Device.renderer_secret_hash.is_(None), ~Device.assignment.has())
        .order_by(Device.created_at)
        .limit(1)
    )
    if screen is None:
        db.session.add(PendingRegistration(secret_hash=secret_hash, role="renderer", created_at=now))
        db.session.commit()
        return Registered()

    screen.renderer_secret_hash = secret_hash
    db.session.commit()
    return Registered(device_id=screen.device_id)


def claim(secret_hash: str, claim_token: str) -> Registered | None:
    """Gives the device an assignment was made for its renderer's secret, spending the assignment's
    claim token. None for a token that is unknown, spent, or whose device already has a renderer."""
    assignment = db.session.scalar(select(RendererAssignment).where(RendererAssignment.claim_token == claim_token))
    device = assignment.device if assignment else None
    if device is None or device.renderer_secret_hash is not None:
        return None
    device.renderer_secret_hash = secret_hash
    assignment.claim_token = None
    db.session.commit()
    return Registered(device_id=device.device_id)


def _unpaired_screens():
    return db.session.scalar(
        select(func.count())
        .select_from(Device)
        .where(Device.renderer_secret_hash.is_(None), Device.paired_at.is_(None), ~Device.assignment.has())
    )


def _drop_stale_screens(now: datetime) -> None:
    """Forgets screens that registered, were never paired and have waited longer than the pool allows,
    as the pool does. They register again with their next call (a 401 tells them to)."""
    stale = db.session.scalars(
        select(Device).where(
            Device.renderer_secret_hash.is_(None),
            Device.paired_at.is_(None),
            ~Device.assignment.has(),
            Device.created_at < now - PENDING_TTL,
        )
    ).all()
    for device in stale:
        db.session.delete(device)


def pending() -> list[PendingRegistration]:
    """Everyone waiting to be matched, longest-waiting first."""
    return db.session.scalars(select(PendingRegistration).order_by(PendingRegistration.created_at)).all()


def _new_device(width: int, height: int, **secret_hashes: str | None) -> str:
    device_id = secrets.token_hex(6)
    # A screen, not its renderer, is what sends its owner's browser to /pair, so its secret makes the token.
    token_source = secret_hashes["display_secret_hash"]
    device = Device(
        device_id=device_id,
        created_at=datetime.now(UTC),
        config=DeviceConfig(width=width, height=height),
        pairing_token=pairing.token_for(token_source),
        **secret_hashes,
    )
    db.session.add(device)
    return device_id
