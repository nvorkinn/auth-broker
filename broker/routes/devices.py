"""A screen's registration (its frames are routes/frames.py's)."""

from typing import TYPE_CHECKING, TypeGuard

from flask import Blueprint, current_app, jsonify, request
from sqlalchemy.exc import IntegrityError

from broker.auth import hash_secret, with_retry_after
from broker.db import db
from broker.models import FRAME_FORMATS, FrameFormat
from broker.services import registration

if TYPE_CHECKING:
    from flask.typing import ResponseReturnValue

bp = Blueprint("devices", __name__, url_prefix="/api/devices")

MIN_SECRET_LENGTH = 16


@bp.post("/register")
def register() -> ResponseReturnValue:
    """Give a screen its identity.

    {"secret": ..., "width": ..., "height": ..., "format": ...}, the secret the screen generated and keeps, its size in
    pixels and the format it takes its frame in (one of FRAME_FORMATS), becomes a device at once (201 {device_id});
    only the secret's hash is stored, and identifies the device. A repeat, e.g. on every boot, is a 200, and changes
    nothing. A new device's weather location is guessed from the screen's IP.

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
    width, height, frame_format = body.get("width"), body.get("height"), body.get("format")
    if not isinstance(secret, str) or len(secret) < MIN_SECRET_LENGTH:
        return jsonify(error="secret must be a random string of at least 16 characters"), 400
    if not (_is_size(width) and _is_size(height)):
        return jsonify(error="width and height must be positive integers"), 400
    if not _is_frame_format(frame_format):
        return jsonify(error=f"format must be one of {', '.join(FRAME_FORMATS)}"), 400

    secret_hash = hash_secret(secret)
    try:
        found = registration.register_screen(secret_hash, width, height, frame_format, ip=request.remote_addr)
    except IntegrityError:
        # The same secret registered by a simultaneous request that got in first: answer as for any repeat.
        db.session.rollback()
        found = registration.register_screen(secret_hash, width, height, frame_format, ip=request.remote_addr)

    if found is None:
        return with_retry_after(jsonify(error="too many devices waiting"), 60), 503
    return jsonify(device_id=found.device_id), 201 if found.created else 200


def _is_size(value: object) -> TypeGuard[int]:
    """Whether a reported width or height is a positive int (and not a bool, which is one to Python)."""
    return type(value) is int and value > 0


def _is_frame_format(value: object) -> TypeGuard[FrameFormat]:
    """Whether a reported frame format is one the renderer can draw."""
    return value in FRAME_FORMATS
