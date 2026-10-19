"""Pairing a logged-in user with a device, which makes them its owner and marks it paired.

There are two ways in:

- A pairing token: a device that sends its owner's browser to /pair right after WiFi setup puts a
  token in the URL, derived from its secret (token_for), so the browser can pair with a device that
  hasn't registered yet by waiting for it. This is the main path.
- A code: the device shows a short-lived code, which the user enters at /pair. It's the fallback for
  when that redirect doesn't happen.
"""

import hashlib
import secrets
import string
from datetime import UTC, datetime, timedelta

from countdown_api import keys as store_keys
from flask import current_app
from sqlalchemy import delete, or_, select

from broker.db import db
from broker.models import Device, PairingCode
from broker.services import assignments, device_keys, pairing_key, render_jobs

# Excludes visually ambiguous characters (0/O, 1/I/L) since this gets typed in by hand.
CODE_ALPHABET = "".join(c for c in string.ascii_uppercase + string.digits if c not in "0O1IL")
CODE_LENGTH = 6
# Ten minutes: the store contract's TTL of the code's key (services/pairing_key.py), which lapses with it.
CODE_TTL = timedelta(seconds=store_keys.PAIRING_TTL_S)


def token_for(secret_hash: str) -> str:
    """Return the token a device derives from its secret to send its owner's browser.

    It's the hex SHA-256 of "pairing:" plus the hex SHA-256 of the secret (auth.hash_secret). One-way
    from the secret, so it's safe in a URL; the device computes the same two hashes and sends nothing
    extra to register.
    """
    return hashlib.sha256(f"pairing:{secret_hash}".encode()).hexdigest()


def pair_by_token(user: str, token: str) -> str:
    """Make the user the owner of the device this token belongs to.

    Returns "paired", or "waiting" if no such device has registered yet (try again shortly), or "taken"
    if someone else owns it. Pairing again as the same user is fine.
    """
    device = db.session.scalar(select(Device).where(Device.pairing_token == token))
    if device is None:
        return "waiting"
    if device.owner not in (None, user):
        return "taken"
    previous = _pair(device, user)
    db.session.commit()
    _after_pairing(device.device_id, previous, user)
    return "paired"


def _pair(device: Device, user: str) -> str | None:
    """Make `user` the device's owner and drop its codes (not committed); return the owner it had.

    A code is only for handing the device to an owner, so once it has one there's none left to show: without this a
    code issued before pairing by token stayed on the screen until it expired.
    """
    db.session.execute(delete(PairingCode).where(PairingCode.device_id == device.device_id))
    previous = device.owner
    device.owner = user
    if device.paired_at is None:
        device.paired_at = datetime.now(UTC)
    # Its stable name, so the render pool lists it (and draws it) at once; the owner can call it anything later.
    assignments.auto_name(device)
    return previous


def _after_pairing(device_id: str, previous: str | None, user: str) -> None:
    """After the commit: take the device's code out of the store, and what it has from before a change of owner.

    So nothing written for its last owner (or before it was unpaired) shows on the new owner's screen, and a pooled
    screen stops showing its code.
    """
    if previous != user:
        device_keys.forget(device_id)  # its pairing key too
    else:
        pairing_key.delete(device_id)
    # So a pooled screen goes back to its dashboard now, not at the next tick.
    render_jobs.enqueue(current_app.extensions["redis"], [device_id])


def issue_code(device_id: str) -> str:
    """Return a fresh code for the device, replacing any it already had.

    Also clears out every device's expired codes, which would otherwise linger for devices that never
    ask for a new one.
    """
    now = datetime.now(UTC)
    code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))
    # Same cutoff as redeem_code: a code is usable up to and including its expires_at.
    db.session.execute(delete(PairingCode).where(or_(PairingCode.device_id == device_id, PairingCode.expires_at < now)))
    expires_at = now + CODE_TTL
    db.session.add(PairingCode(code=code, device_id=device_id, expires_at=expires_at))
    db.session.commit()
    # For a screen the pool draws, which reads only the store; over the key of any code this one replaces.
    pairing_key.write(device_id, code, expires_at)
    # And draws it now, after the key's written.
    render_jobs.enqueue(current_app.extensions["redis"], [device_id])
    return code


def live_code(device_id: str) -> str | None:
    """Return the device's unexpired code, if it has one."""
    return db.session.scalar(
        select(PairingCode.code).where(PairingCode.device_id == device_id, PairingCode.expires_at > datetime.now(UTC))
    )


def redeem_code(code: str, user: str) -> str | None:
    """Use up a live code and pair the device with the user, returning the device_id.

    None if the code is unknown or expired. Each code works once. A code is proof of being at the
    device, so it also hands a device that already has an owner to a new one.
    """
    pairing_code = db.session.get(PairingCode, code)
    if pairing_code is None or pairing_code.expires_at < datetime.now(UTC):
        return None

    device_id = pairing_code.device_id
    # By itself first, so a second redeem of the same code racing this one fails at the commit rather than pairing too.
    db.session.delete(pairing_code)
    previous = _pair(db.session.get(Device, device_id), user)  # and the device's other codes
    db.session.commit()
    _after_pairing(device_id, previous, user)
    return device_id
