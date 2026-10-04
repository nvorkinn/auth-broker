import functools
import secrets
import string
from datetime import UTC, datetime, timedelta

from flask import Blueprint, current_app, jsonify, request
from sqlalchemy import delete, select
from werkzeug.security import generate_password_hash

from . import spotify as spotify_module
from .auth import require_device_auth
from .crypto import decrypt
from .db import db
from .models import Device, DeviceConfig, GlowmarktCredentials, PairingCode

bp = Blueprint("devices_api", __name__, url_prefix="/api/devices")

# Excludes visually ambiguous characters (0/O, 1/I/L) since this gets typed in by hand.
PAIRING_CODE_ALPHABET = "".join(c for c in string.ascii_uppercase + string.digits if c not in "0O1IL")
PAIRING_CODE_LENGTH = 6
PAIRING_CODE_TTL = timedelta(minutes=10)


@bp.post("/register")
def register():
    """Called once by a Pi on first boot. The Pi generates its own device_secret
    and keeps it locally forever after; only its hash is ever stored here."""
    body = request.get_json(silent=True) or {}
    device_secret = body.get("device_secret", "")
    if len(device_secret) < 16:
        return jsonify(error="device_secret must be a random string of at least 16 characters"), 400

    device_id = secrets.token_hex(6)
    db.session.add(
        Device(
            device_id=device_id,
            device_secret_hash=generate_password_hash(device_secret),
            created_at=datetime.now(UTC).isoformat(),
            config=DeviceConfig(),
        )
    )
    db.session.commit()
    return jsonify(device_id=device_id), 201


def _issue_pairing_code(device_id: str) -> str:
    code = "".join(secrets.choice(PAIRING_CODE_ALPHABET) for _ in range(PAIRING_CODE_LENGTH))
    expires_at = datetime.now(UTC) + PAIRING_CODE_TTL
    db.session.execute(delete(PairingCode).where(PairingCode.device_id == device_id))
    db.session.add(PairingCode(code=code, device_id=device_id, expires_at=expires_at.isoformat()))
    db.session.commit()
    return code


def _pairing_code_for(device_id: str) -> str | None:
    """A live code if there is one; otherwise a new one, unless the device is already paired."""
    live = db.session.scalar(
        select(PairingCode.code).where(
            PairingCode.device_id == device_id, PairingCode.expires_at > datetime.now(UTC).isoformat()
        )
    )
    if live:
        return live
    paired_at = db.session.get(Device, device_id).paired_at
    return None if paired_at else _issue_pairing_code(device_id)


def _setup_missing(config: DeviceConfig) -> list[str]:
    missing = []
    if not config.weather_location.strip():
        missing.append("a weather location")
    if not config.postcode.strip():
        missing.append("a postcode")
    if not config.tfl_stop_ids:
        missing.append("a bus or tube stop")
    return missing


def require_spotify_enabled(default):
    """For Pi-facing Spotify endpoints: answers with `default` (as JSON) instead of
    running the view when the device has Spotify switched off. Apply it below
    @require_device_auth so unauthenticated callers can't probe device settings."""

    def decorator(view):
        @functools.wraps(view)
        def wrapped(device_id, *args, **kwargs):
            config = db.session.get(DeviceConfig, device_id)
            if config is None:
                return jsonify(error="not found"), 404
            if not config.spotify_enabled:
                return jsonify(default)
            return view(device_id, *args, **kwargs)

        return wrapped

    return decorator


@bp.post("/<device_id>/pairing-code")
@require_device_auth
def create_pairing_code(device_id):
    """Forces a fresh code, e.g. to link a new browser to an already-paired device."""
    code = _issue_pairing_code(device_id)
    return jsonify(code=code, expires_in_seconds=int(PAIRING_CODE_TTL.total_seconds()))


def _update_device_name(device_id: str) -> None:
    """Opportunistically keeps devices.device_name in sync with the Pi's own
    DEVICE_ID (sent as X-Device-Name on every config poll) -- a purely cosmetic
    label for logs/admin output, never the device's real identity, so a rename
    on the Pi just needs its next poll to take effect here, no re-pairing."""
    name = request.headers.get("X-Device-Name", "").strip()
    if name:
        db.session.get(Device, device_id).device_name = name
        db.session.commit()


@bp.get("/<device_id>/config")
@require_device_auth
def get_config(device_id):
    """Polled by the Pi. Bundles the device's own settings together with the
    shared app-level API keys, so a key rotation doesn't require re-flashing
    every gifted device."""
    _update_device_name(device_id)
    config = db.session.get(DeviceConfig, device_id)
    if config is None:
        return jsonify(error="not found"), 404

    creds = db.session.get(GlowmarktCredentials, device_id)
    glowmarkt = {
        "username": creds.username if creds and creds.username else None,
        "password": decrypt(creds.password_encrypted) if creds and creds.password_encrypted else None,
    }

    return jsonify(
        interval=config.interval,
        weather={"api_key": current_app.config["WEATHER_API_KEY"], "location": config.weather_location},
        notice_board={"postcode": config.postcode or None},
        tfl={"app_key": current_app.config["TFL_APP_KEY"], "stop_ids": config.tfl_stop_ids},
        spotify={"enabled": config.spotify_enabled},
        glowmarkt=glowmarkt,
        pairing_code=_pairing_code_for(device_id),
        setup_missing=_setup_missing(config),
    )


@bp.get("/<device_id>/now-playing")
@require_device_auth
@require_spotify_enabled(default=None)
def now_playing(device_id):
    """Polled by the Pi in place of talking to Spotify directly - this device
    never sees a Spotify token, only its own device secret."""
    return jsonify(spotify_module.get_current_track(device_id))


@bp.get("/<device_id>/queue")
@require_device_auth
@require_spotify_enabled(default=[])
def queue(device_id):
    """Upcoming Spotify queue, same auth and token handling as now-playing.
    Always a JSON list; empty when Spotify is disabled, unlinked or nothing is queued."""
    return jsonify(spotify_module.get_queue(device_id))


@bp.get("/<device_id>/top/<type_>")
@require_device_auth
@require_spotify_enabled(default=None)
def top(device_id: str, type_: str):
    return jsonify(spotify_module.get_users_top_items(device_id, type_, request.args.to_dict()))
