"""Registering screens.

A screen becomes a device straight away: it exists from its first call, so its owner can pair with it, with a code
it shows the moment it registers (services/pairing.py), and name it (services/assignments.py). The render pool
draws it from then on: its pairing screen first, then its dashboard.

Secrets arrive already hashed (auth.hash_secret); the hash is what identifies the caller here.

A new screen's weather location starts as the town or city its IP places it in (services/geoip.py), so its dashboard
has weather before its owner ever opens the settings page; it's left blank when the IP can't be placed.
"""

import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from broker.db import db
from broker.models import Device, DeviceConfig

from . import geoip, pairing

# Bound on what anyone can add: how many screens may sit unpaired at once, and for how long before
# they have to register again.
MAX_UNPAIRED_SCREENS = 20
UNPAIRED_TTL = timedelta(hours=24)


@dataclass(frozen=True)
class Registered:
    """The device a secret belongs to; `created` is whether this call registered it, rather than repeating it."""

    device_id: str
    created: bool = False


def _device_with(secret_hash: str) -> Device | None:
    return db.session.scalar(select(Device).where(Device.display_secret_hash == secret_hash))


def register_screen(secret_hash: str, width: int, height: int, ip: str | None = None) -> Registered | None:
    """Make the screen a device at once, so its owner can pair with it and name it.

    A repeat of a registered secret finds its device, and leaves its config alone. Returns None when too many screens
    are already waiting to be paired. `ip` is the screen's, for its weather location.
    """
    device = _device_with(secret_hash)
    if device is not None:
        return Registered(device_id=device.device_id)

    _drop_stale_screens(datetime.now(UTC))
    if _unpaired_screens() >= MAX_UNPAIRED_SCREENS:
        db.session.commit()
        return None
    device_id = _new_device(secret_hash, width, height, ip)
    db.session.commit()
    # So its screen shows a code to pair with by hand right away, if its owner isn't sent to /pair by WiFi setup.
    pairing.issue_code(device_id)
    return Registered(device_id=device_id, created=True)


def _unpaired_screens() -> int:
    return db.session.scalar(
        select(func.count()).select_from(Device).where(Device.paired_at.is_(None), ~Device.assignment.has())
    )


def _drop_stale_screens(now: datetime) -> None:
    """Forget screens that registered, were never paired and have waited longer than allowed.

    They register again with their next call (a 401 tells them to).
    """
    stale = db.session.scalars(
        select(Device).where(
            Device.paired_at.is_(None),
            ~Device.assignment.has(),
            Device.created_at < now - UNPAIRED_TTL,
        )
    ).all()
    for device in stale:
        db.session.delete(device)


def _new_device(display_secret_hash: str, width: int, height: int, ip: str | None) -> str:
    device_id = secrets.token_hex(6)
    # Only a name, with no picked place: the weather provider geocodes it by name until a save (or `flask weather
    # backfill`) picks one.
    weather_location = geoip.suggest_weather_location(ip) or ""
    # A screen's secret makes the token that sends its owner's browser to /pair.
    device = Device(
        device_id=device_id,
        created_at=datetime.now(UTC),
        config=DeviceConfig(width=width, height=height, weather_location=weather_location),
        pairing_token=pairing.token_for(display_secret_hash),
        display_secret_hash=display_secret_hash,
    )
    db.session.add(device)
    return device_id
