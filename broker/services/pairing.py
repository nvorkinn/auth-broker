"""Pairing a browser with a device: the device shows a short-lived code, and entering it at /pair
binds that browser's session to the device and marks the device paired."""

import secrets
import string
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select

from ..db import db
from ..models import Device, PairingCode

# Excludes visually ambiguous characters (0/O, 1/I/L) since this gets typed in by hand.
CODE_ALPHABET = "".join(c for c in string.ascii_uppercase + string.digits if c not in "0O1IL")
CODE_LENGTH = 6
CODE_TTL = timedelta(minutes=10)


def issue_code(device_id: str) -> str:
    """A fresh code for the device, replacing any it already had."""
    code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))
    expires_at = datetime.now(UTC) + CODE_TTL
    db.session.execute(delete(PairingCode).where(PairingCode.device_id == device_id))
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


def redeem_code(code: str) -> str | None:
    """Uses up a live code and marks its device paired, returning the device_id; None if the
    code is unknown or expired. Each code works once."""
    pairing_code = db.session.get(PairingCode, code)
    if pairing_code is None or pairing_code.expires_at < datetime.now(UTC):
        return None

    device_id = pairing_code.device_id
    db.session.delete(pairing_code)
    device = db.session.get(Device, device_id)
    if device.paired_at is None:
        device.paired_at = datetime.now(UTC)
    db.session.commit()
    return device_id
