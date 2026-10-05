"""A device's own lifecycle: registering on first boot, pairing codes, and the config poll it
checks in with. Everything but /register needs one of the device's secrets."""

from flask import Blueprint, current_app, jsonify, request
from sqlalchemy.exc import IntegrityError

from ..auth import ROLES, hash_secret, require_device_auth
from ..db import db
from ..models import Device, DeviceConfig, GlowmarktCredentials
from ..services import pairing, registration
from ..services.setup import setup_missing

bp = Blueprint("devices", __name__, url_prefix="/api/devices")


@bp.post("/register")
def register():
    """How a device gets its identity. The client generates its own secret, keeps it locally forever
    after, and sends it with every request; only its hash is stored here, and that hash alone
    identifies the device. Safe to call again with the same secret, e.g. on every boot.

    - {"role": "renderer", "standalone": true, "secret": ...}: a Pi that is its own screen. Becomes
      a device at once (201). The old body, {"device_secret": ...} with no role, means the same.
    - {"role": "renderer" | "display", "secret": ...}: one half of a split deployment. Joins the
      pending pool and is matched with the longest-waiting client of the other role into one
      device (201 {device_id}), or waits (202) until one arrives. A waiting client polls its usual
      endpoint (/api/config, /api/frame), which also answers 202 until it's matched.

    Throttled per IP, every call counting, as anyone can call it."""
    throttle = current_app.extensions["register_throttle"]
    ip = request.remote_addr or "unknown"
    retry_after = throttle.retry_after(ip)
    if retry_after is not None:
        return jsonify(error="too many registrations"), 429, {"Retry-After": str(retry_after)}
    # The call that reaches the limit still goes through; it's the next one that's refused.
    lockout = throttle.record_failure(ip)
    if lockout is not None:
        current_app.logger.warning("Too many registrations from %s; locked out for %ss", ip, lockout)

    body = request.get_json(silent=True) or {}
    legacy = "role" not in body and "device_secret" in body
    role = body.get("role", "renderer")
    standalone = legacy or body.get("standalone") is True
    secret = body.get("secret", body.get("device_secret", ""))
    if role not in ROLES:
        return jsonify(error=f"role must be one of {', '.join(ROLES)}"), 400
    if standalone and role != "renderer":
        return jsonify(error="only a renderer can be standalone"), 400
    if not isinstance(secret, str) or len(secret) < 16:
        return jsonify(error="secret must be a random string of at least 16 characters"), 400

    secret_hash = hash_secret(secret)
    found = registration.existing(role, secret_hash)
    if found is None:
        try:
            if standalone:
                return jsonify(device_id=registration.register_standalone(secret_hash)), 201
            found = registration.register_pending(role, secret_hash)
        except IntegrityError:
            # The same secret registered by a simultaneous request (e.g. a quick retry) that got in
            # first: answer as for any repeat.
            db.session.rollback()
            found = registration.existing(role, secret_hash)
        else:
            if found is None:
                return jsonify(error="too many devices waiting to be matched"), 503, {"Retry-After": "60"}
            if found.device_id:
                return jsonify(device_id=found.device_id), 201

    if found.role_conflict:
        return jsonify(error="that secret is already registered with another role"), 409
    if found.waiting:
        return jsonify(status="waiting"), 202
    return jsonify(device_id=found.device_id), 200


@bp.post("/<device_id>/pairing-code")
@require_device_auth(roles={"renderer"})
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


@bp.get("/<device_id>/config")
@require_device_auth(roles={"renderer"})
def get_config(device_id):
    """Polled by the renderer, here or as /api/config with no device_id. Bundles the device's own
    settings together with the shared app-level API keys, so a key rotation doesn't require
    re-flashing every gifted device. Carries the device_id, which is how a renderer matched through
    the pending pool learns it."""
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
        device_id=device_id,
        interval=config.interval,
        weather={"api_key": current_app.config["WEATHER_API_KEY"], "location": config.weather_location},
        notice_board={"postcode": config.postcode or None},
        tfl={"app_key": current_app.config["TFL_APP_KEY"], "stop_ids": config.tfl_stop_ids},
        spotify={"enabled": config.spotify_enabled},
        glowmarkt=glowmarkt,
        pairing_code=pairing.current_code_for(device_id),
        setup_missing=setup_missing(config),
    )
