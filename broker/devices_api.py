import functools
import json
import secrets
import string
from datetime import UTC, datetime, timedelta

from flask import Blueprint, current_app, jsonify, request
from werkzeug.security import generate_password_hash

from . import spotify as spotify_module
from .auth import require_device_auth
from .crypto import decrypt
from .db import get_db

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
    db = get_db()
    db.execute(
        "INSERT INTO devices (device_id, device_secret_hash, created_at) VALUES (?, ?, ?)",
        (device_id, generate_password_hash(device_secret), datetime.now(UTC).isoformat()),
    )
    db.execute("INSERT INTO device_config (device_id) VALUES (?)", (device_id,))
    db.commit()
    return jsonify(device_id=device_id), 201


def _issue_pairing_code(db, device_id: str) -> str:
    code = "".join(secrets.choice(PAIRING_CODE_ALPHABET) for _ in range(PAIRING_CODE_LENGTH))
    expires_at = datetime.now(UTC) + PAIRING_CODE_TTL
    db.execute("DELETE FROM pairing_codes WHERE device_id = ?", (device_id,))
    db.execute(
        "INSERT INTO pairing_codes (code, device_id, expires_at) VALUES (?, ?, ?)",
        (code, device_id, expires_at.isoformat()),
    )
    db.commit()
    return code


def _pairing_code_for(db, device_id: str) -> str | None:
    """A live code if there is one; otherwise a new one, unless the device is already paired."""
    live = db.execute(
        "SELECT code FROM pairing_codes WHERE device_id = ? AND expires_at > ?",
        (device_id, datetime.now(UTC).isoformat()),
    ).fetchone()
    if live:
        return live["code"]
    paired_at = db.execute("SELECT paired_at FROM devices WHERE device_id = ?", (device_id,)).fetchone()["paired_at"]
    return None if paired_at else _issue_pairing_code(db, device_id)


def _setup_missing(config_row) -> list[str]:
    missing = []
    if not config_row["weather_location"].strip():
        missing.append("a weather location")
    if not json.loads(config_row["tfl_stop_ids"]):
        missing.append("a bus or tube stop")
    return missing


def require_spotify_enabled(default):
    """For Pi-facing Spotify endpoints: answers with `default` (as JSON) instead of
    running the view when the device has Spotify switched off. Apply it below
    @require_device_auth so unauthenticated callers can't probe device settings."""

    def decorator(view):
        @functools.wraps(view)
        def wrapped(device_id, *args, **kwargs):
            row = (
                get_db()
                .execute("SELECT spotify_enabled FROM device_config WHERE device_id = ?", (device_id,))
                .fetchone()
            )
            if row is None:
                return jsonify(error="not found"), 404
            if not row["spotify_enabled"]:
                return jsonify(default)
            return view(device_id, *args, **kwargs)

        return wrapped

    return decorator


@bp.post("/<device_id>/pairing-code")
@require_device_auth
def create_pairing_code(device_id):
    """Forces a fresh code, e.g. to link a new browser to an already-paired device."""
    code = _issue_pairing_code(get_db(), device_id)
    return jsonify(code=code, expires_in_seconds=int(PAIRING_CODE_TTL.total_seconds()))


@bp.get("/<device_id>/config")
@require_device_auth
def get_config(device_id):
    """Polled by the Pi. Bundles the device's own settings together with the
    shared app-level API keys, so a key rotation doesn't require re-flashing
    every gifted device."""
    db = get_db()
    row = db.execute("SELECT * FROM device_config WHERE device_id = ?", (device_id,)).fetchone()
    if row is None:
        return jsonify(error="not found"), 404

    glowmarkt_row = db.execute(
        "SELECT username, password_encrypted FROM glowmarkt_credentials WHERE device_id = ?", (device_id,)
    ).fetchone()
    glowmarkt = {
        "username": glowmarkt_row["username"] if glowmarkt_row and glowmarkt_row["username"] else None,
        "password": decrypt(glowmarkt_row["password_encrypted"])
        if glowmarkt_row and glowmarkt_row["password_encrypted"]
        else None,
    }

    return jsonify(
        interval=row["interval"],
        weather={"api_key": current_app.config["WEATHER_API_KEY"], "location": row["weather_location"]},
        tfl={"app_key": current_app.config["TFL_APP_KEY"], "stop_ids": json.loads(row["tfl_stop_ids"])},
        spotify={"enabled": bool(row["spotify_enabled"])},
        glowmarkt=glowmarkt,
        pairing_code=_pairing_code_for(db, device_id),
        setup_missing=_setup_missing(row),
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
    return jsonify(spotify_module.get_users_top_items(device_id, type_))
