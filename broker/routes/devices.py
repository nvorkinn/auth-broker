"""A device's own lifecycle: registering on first boot, pairing codes, and the config poll it checks in with.

Everything but /register needs one of the device's secrets.
"""

from typing import TYPE_CHECKING

from countdown_api.blueprints import ApiHTTPError

# At run time: the API registry reads get_config's signature, return annotation included, when it registers it.
from countdown_api.models import ConfigResponse  # noqa: TC002
from flask import Blueprint, current_app, jsonify, request
from sqlalchemy.exc import IntegrityError

from broker.api import api
from broker.auth import hash_secret, require_device_auth, with_retry_after
from broker.db import db
from broker.models import Device, DeviceConfig
from broker.services import config_store, pairing, registration, renderer_config

if TYPE_CHECKING:
    from flask.typing import ResponseReturnValue

bp = Blueprint("devices", __name__, url_prefix="/api/devices")

# The shortest secret a screen or renderer may register with.
MIN_SECRET_LENGTH = 16


@bp.post("/register")
def register() -> ResponseReturnValue:
    """Give a screen its identity.

    The screen generates its own secret, keeps it locally forever after, and sends it with every
    request; only its hash is stored here, and that hash alone identifies the device.
    Safe to call again with the same secret, e.g. on every boot.

    {"secret": ..., "width": ..., "height": ...}, the screen's size in pixels, becomes a device at once
    (201 {device_id}), which its owner pairs with and names a renderer for; the orchestrator then starts
    that renderer, which claims the device itself (see routes/renderers.py). A repeat is a 200.

    Throttled per IP, every call counting, as anyone can call it.
    """
    throttle = current_app.extensions["register_throttle"]
    ip = request.remote_addr or "unknown"
    retry_after = throttle.retry_after(ip)
    if retry_after is not None:
        return with_retry_after(jsonify(error="too many registrations"), retry_after), 429
    # The call that reaches the limit still goes through; it's the next one that's refused.
    lockout = throttle.record_failure(ip)
    if lockout is not None:
        current_app.logger.warning("Too many registrations from %s; locked out for %ss", ip, lockout)

    body = request.get_json(silent=True) or {}
    secret = body.get("secret", "")
    width, height = body.get("width"), body.get("height")
    if not isinstance(secret, str) or len(secret) < MIN_SECRET_LENGTH:
        return jsonify(error="secret must be a random string of at least 16 characters"), 400
    if not all(type(size) is int and size > 0 for size in (width, height)):
        return jsonify(error="width and height must be positive integers"), 400

    secret_hash = hash_secret(secret)
    try:
        found = registration.register_screen(secret_hash, width, height)
    except IntegrityError:
        # The same secret registered by a simultaneous request (e.g. a quick retry) that got in
        # first: answer as for any repeat.
        db.session.rollback()
        found = registration.register_screen(secret_hash, width, height)

    if found is None:
        return with_retry_after(jsonify(error="too many devices waiting"), 60), 503
    if found.role_conflict:
        return jsonify(error="that secret is already registered as a renderer"), 409
    return jsonify(device_id=found.device_id), 201 if found.created else 200


@bp.post("/pairing-code")
@require_device_auth()
def create_pairing_code(device_id: str) -> ResponseReturnValue:
    """Force a fresh code, e.g. to link a new browser to an already-paired device.

    A screen can ask too, since a device that has no renderer yet has nothing else to show one.
    """
    code = pairing.issue_code(device_id)
    # The renderer has a code to show now, which only GET /api/config gives it: take its config out of the store.
    config_store.publish(device_id)
    return jsonify(code=code, expires_in_seconds=int(pairing.CODE_TTL.total_seconds()))


def _update_device_name(device_id: str, name: str | None) -> None:
    """Opportunistically keep devices.device_name in sync with the Pi's own DEVICE_ID.

    It is sent as X-Device-Name on every config poll -- a purely cosmetic
    label for logs/admin output, never the device's real identity, so a rename
    on the Pi just needs its next poll to take effect here, no re-pairing.
    """
    name = (name or "").strip()
    if name:
        db.session.get(Device, device_id).device_name = name
        db.session.commit()


@api.get_config()
def get_config(*, device_id: str, x_device_name: str | None = None) -> ConfigResponse:
    """Answer the renderer's config poll (GET /api/config in the contract, which also says that only its role may).

    What's in it is services/renderer_config.py's: the device's settings. While the device isn't paired this also
    issues the code it shows (pairing.current_code_for). A renderer reading the store reads the same config from
    there, without a code, and asks here only when the store has none for it.
    """
    _update_device_name(device_id, x_device_name)
    config = db.session.get(DeviceConfig, device_id)
    if config is None:
        raise ApiHTTPError(404, message="not found")
    return renderer_config.build(config, pairing_code=pairing.current_code_for(device_id))
