"""Pairing a logged-in user with a device, which makes them its owner and marks it paired. There are
two ways in:

- A pairing token: a device that sends its owner's browser to /pair right after WiFi setup puts a
  token in the URL, derived from its secret (token_for), so the browser can pair with a device that
  hasn't registered yet by waiting for it. This is the main path.
- A code: the device shows a short-lived code, which the user enters at /pair. It's the fallback for
  when that redirect doesn't happen."""

import hashlib
import secrets
import string
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, or_, select

from ..db import db
from ..models import Device, PairingCode

# Excludes visually ambiguous characters (0/O, 1/I/L) since this gets typed in by hand.
CODE_ALPHABET = "".join(c for c in string.ascii_uppercase + string.digits if c not in "0O1IL")
CODE_LENGTH = 6
CODE_TTL = timedelta(minutes=10)


def token_for(renderer_secret_hash: str) -> str:
    """The token a device derives from its secret to send its owner's browser, hex SHA-256 of
    "pairing:" plus the hex SHA-256 of the secret (auth.hash_secret). One-way from the secret, so
    it's safe in a URL; the device computes the same two hashes and sends nothing extra to register."""
    return hashlib.sha256(f"pairing:{renderer_secret_hash}".encode()).hexdigest()


def pair_by_token(user: str, token: str) -> str:
    """Makes the user the owner of the device this token belongs to: "paired", or "waiting" if no
    such device has registered yet (try again shortly), or "taken" if someone else owns it. Pairing
    again as the same user is fine."""
    device = db.session.scalar(select(Device).where(Device.pairing_token == token))
    if device is None:
        return "waiting"
    if device.owner not in (None, user):
        return "taken"
    _pair(device, user)
    db.session.commit()
    return "paired"


def _pair(device: Device, user: str) -> None:
    device.owner = user
    if device.paired_at is None:
        device.paired_at = datetime.now(UTC)


def issue_code(device_id: str) -> str:
    """A fresh code for the device, replacing any it already had. Also clears out every device's
    expired codes, which would otherwise linger for devices that never ask for a new one."""
    now = datetime.now(UTC)
    code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))
    # Same cutoff as redeem_code: a code is usable up to and including its expires_at.
    db.session.execute(delete(PairingCode).where(or_(PairingCode.device_id == device_id, PairingCode.expires_at < now)))
    expires_at = now + CODE_TTL
    db.session.add(PairingCode(code=code, device_id=device_id, expires_at=expires_at))
    db.session.commit()
    return code


def live_code(device_id: str) -> str | None:
    """The device's unexpired code, if it has one."""
    return db.session.scalar(
        select(PairingCode.code).where(PairingCode.device_id == device_id, PairingCode.expires_at > datetime.now(UTC))
    )


def current_code_for(device_id: str) -> str | None:
    """A live code if there is one; otherwise a new one, unless the device is already paired."""
    live = live_code(device_id)
    if live:
        return live
    paired_at = db.session.get(Device, device_id).paired_at
    return None if paired_at else issue_code(device_id)


def redeem_code(code: str, user: str) -> str | None:
    """Uses up a live code and pairs the device with the user, returning the device_id; None if the
    code is unknown or expired. Each code works once. A code is proof of being at the device, so it
    also hands a device that already has an owner to a new one."""
    pairing_code = db.session.get(PairingCode, code)
    if pairing_code is None or pairing_code.expires_at < datetime.now(UTC):
        return None

    device_id = pairing_code.device_id
    db.session.delete(pairing_code)
    _pair(db.session.get(Device, device_id), user)
    db.session.commit()
    return device_id
