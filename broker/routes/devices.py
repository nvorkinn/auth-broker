"""A device's own lifecycle: registering on first boot, pairing codes, and the config poll it
checks in with. Everything but /register needs one of the device's secrets."""

import secrets
from datetime import UTC, datetime

from flask import Blueprint, current_app, jsonify, request
from werkzeug.security import generate_password_hash

from ..auth import ROLES, require_device_auth
from ..db import db
from ..models import Device, DeviceConfig, GlowmarktCredentials
from ..services import pairing
from ..services.setup import setup_missing

bp = Blueprint("devices", __name__, url_prefix="/api/devices")


@bp.post("/register")
def register():
    """Called once per role on first boot: by a Pi or server-side renderer as "renderer", by a
    screen as "display". The client generates its own secret and keeps it locally forever after;
    only its hash is ever stored here.

    With no device_id it creates a new device. With one it attaches the role to that existing
    device, as long as the role's slot is empty; re-attaching needs the slot cleared from the web UI
    first. The old body, {"device_secret": ...} with no role, still registers a renderer."""
    body = request.get_json(silent=True) or {}
    role = body.get("role", "renderer")
    secret = body.get("secret", body.get("device_secret", ""))
    if role not in ROLES:
        return jsonify(error=f"role must be one of {', '.join(ROLES)}"), 400
    if not isinstance(secret, str) or len(secret) < 16:
        return jsonify(error="secret must be a random string of at least 16 characters"), 400

    secret_hash = generate_password_hash(secret)
    column = f"{role}_secret_hash"
    device_id = body.get("device_id")
    if device_id is not None:
        # TODO: this trusts anyone who knows the device_id. Once the config website can mint a
        # short-lived one-time link code, require it here.
        device = db.session.get(Device, device_id) if isinstance(device_id, str) else None
        if device is None or getattr(device, column) is not None:
            return jsonify(error=f"no device {device_id} with a free {role} slot"), 409
        setattr(device, column, secret_hash)
        db.session.commit()
        return jsonify(device_id=device_id), 200

    device_id = secrets.token_hex(6)
    db.session.add(
        Device(device_id=device_id, created_at=datetime.now(UTC), config=DeviceConfig(), **{column: secret_hash})
    )
    db.session.commit()
    return jsonify(device_id=device_id), 201


@bp.post("/<device_id>/pairing-code")
@require_device_auth(roles={"renderer", "display"})
def create_pairing_code(device_id):
    """Forces a fresh code, e.g. to link a new browser to an already-paired device."""
    code = pairing.issue_code(device_id)
    return jsonify(code=code, expires_in_seconds=int(pairing.CODE_TTL.total_seconds()))


def _update_device_name(device_id: str) -> None:
    """Opportunistically keeps devices.device_name in sync with the Pi's own
    DEVICE_ID (sent as X-Device-Name on every config poll) -- a purely cosmetic
    label for logs/admin output, never the device's real identity, so a rename
    on the Pi just needs its next poll to take effect here, no re-pairing."""
    name = request.headers.get("X-Device-Name", "").strip()
    if name:
        db.session.get(Device, device_id).device_name = name
        db.session.commit()


@bp.get("/<device_id>/status")
@require_device_auth(roles={"display"})
def get_status(device_id):
    """Polled by a screen, which has no config of its own, so it can draw its pairing code itself."""
    status = {"paired": db.session.get(Device, device_id).paired_at is not None}
    code = pairing.current_code_for(device_id)
    if code:
        status["pairing_code"] = code
    return jsonify(status)


@bp.get("/<device_id>/config")
@require_device_auth(roles={"renderer"})
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
        "username": (creds and creds.username) or None,
        "password": creds.password if creds else None,
    }

    return jsonify(
        interval=config.interval,
        weather={"api_key": current_app.config["WEATHER_API_KEY"], "location": config.weather_location},
        notice_board={"postcode": config.postcode or None},
        tfl={"app_key": current_app.config["TFL_APP_KEY"], "stop_ids": config.tfl_stop_ids},
        spotify={"enabled": config.spotify_enabled},
        glowmarkt=glowmarkt,
        pairing_code=pairing.current_code_for(device_id),
        setup_missing=setup_missing(config),
    )
