import json
import secrets
import string
from datetime import datetime, timedelta, timezone

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
        (device_id, generate_password_hash(device_secret), datetime.now(timezone.utc).isoformat()),
    )
    db.execute("INSERT INTO device_config (device_id) VALUES (?)", (device_id,))
    db.commit()
    return jsonify(device_id=device_id), 201


@bp.post("/<device_id>/pairing-code")
@require_device_auth
def create_pairing_code(device_id):
    """Called by the Pi whenever it wants to show a fresh pairing code on screen."""
    code = "".join(secrets.choice(PAIRING_CODE_ALPHABET) for _ in range(PAIRING_CODE_LENGTH))
    expires_at = datetime.now(timezone.utc) + PAIRING_CODE_TTL

    db = get_db()
    db.execute("DELETE FROM pairing_codes WHERE device_id = ?", (device_id,))
    db.execute(
        "INSERT INTO pairing_codes (code, device_id, expires_at) VALUES (?, ?, ?)",
        (code, device_id, expires_at.isoformat()),
    )
    db.commit()
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
    )


@bp.get("/<device_id>/now-playing")
@require_device_auth
def now_playing(device_id):
    """Polled by the Pi in place of talking to Spotify directly - this device
    never sees a Spotify token, only its own device secret."""
    row = get_db().execute(
        "SELECT spotify_enabled FROM device_config WHERE device_id = ?", (device_id,)
    ).fetchone()
    if row is None:
        return jsonify(error="not found"), 404
    if not row["spotify_enabled"]:
        return jsonify(None)

    return jsonify(spotify_module.get_current_track(device_id))
