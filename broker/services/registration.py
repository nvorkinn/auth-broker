"""Registering devices. A standalone Pi, which is its own screen, becomes a device straight away. A
split deployment's renderer and screen register separately, in either order, and wait in the pending
pool until one of each can be matched into a single device.

Secrets arrive already hashed (auth.hash_secret); the hash is what identifies the caller here."""

import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, or_, select

from ..db import db
from ..models import Device, DeviceConfig, PendingRegistration

# Bounds on the pool, which anyone can add to: how many may wait at once, and for how long before a
# waiting client has to register again.
MAX_PENDING = 20
PENDING_TTL = timedelta(hours=24)

OTHER_ROLE = {"renderer": "display", "display": "renderer"}


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


def register_standalone(secret_hash: str) -> str:
    """A device with only a renderer secret: a Pi that renders and shows its own frames."""
    device_id = _new_device(renderer_secret_hash=secret_hash)
    db.session.commit()
    return device_id


def register_pending(role: str, secret_hash: str) -> Registered | None:
    """Adds the caller to the pool and matches it with the longest-waiting client of the other role,
    if there is one. Returns None when the pool is full.

    Every step happens in one transaction that starts with a write. SQLite allows one writer at a
    time across every thread and process, so from that first write until the commit no other
    registration can run; two callers can never claim the same partner."""
    now = datetime.now(UTC)
    db.session.execute(delete(PendingRegistration).where(PendingRegistration.created_at < now - PENDING_TTL))
    if db.session.scalar(select(func.count()).select_from(PendingRegistration)) >= MAX_PENDING:
        db.session.commit()
        return None

    db.session.add(PendingRegistration(secret_hash=secret_hash, role=role, created_at=now))
    db.session.flush()

    oldest = (
        select(PendingRegistration.secret_hash)
        .where(PendingRegistration.role == OTHER_ROLE[role])
        .order_by(PendingRegistration.created_at)
        .limit(1)
        .scalar_subquery()
    )
    partner_hash = db.session.scalar(
        delete(PendingRegistration)
        .where(PendingRegistration.secret_hash == oldest)
        .returning(PendingRegistration.secret_hash)
    )
    if partner_hash is None:
        db.session.commit()
        return Registered()

    db.session.execute(delete(PendingRegistration).where(PendingRegistration.secret_hash == secret_hash))
    hashes = {f"{role}_secret_hash": secret_hash, f"{OTHER_ROLE[role]}_secret_hash": partner_hash}
    device_id = _new_device(**hashes)
    db.session.commit()
    return Registered(device_id=device_id)


def pending() -> list[PendingRegistration]:
    """Everyone waiting to be matched, longest-waiting first."""
    return db.session.scalars(select(PendingRegistration).order_by(PendingRegistration.created_at)).all()


def _new_device(**secret_hashes: str) -> str:
    device_id = secrets.token_hex(6)
    db.session.add(Device(device_id=device_id, created_at=datetime.now(UTC), config=DeviceConfig(), **secret_hashes))
    return device_id
