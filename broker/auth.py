import functools
import hmac
import os

from flask import current_app, jsonify, redirect, request, session, url_for
from werkzeug.security import check_password_hash

from .db import db
from .models import Device


def require_renderer_auth(view):
    @functools.wraps(view)
    def wrapper(*args, **kwargs):
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return jsonify(error="unauthorized"), 401

        secret = auth_header.removeprefix("Bearer ")
        expected = os.environ["COUNTDOWN_RENDERER_TOKEN"]
        if not secret or not hmac.compare_digest(secret, expected):
            return jsonify(error="unauthorized"), 401
        return view(*args, **kwargs)

    return wrapper


def require_device_auth(view):
    """Protects Pi-facing endpoints shaped /api/devices/<device_id>/... with the
    device's own bearer secret, set at registration time."""

    @functools.wraps(view)
    def wrapped(device_id, *args, **kwargs):
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return jsonify(error="unauthorized"), 401

        secret = auth_header.removeprefix("Bearer ")
        device = db.session.get(Device, device_id)
        if device is None or not check_password_hash(device.device_secret_hash, secret):
            return jsonify(error="unauthorized"), 401

        # device_name is just the Pi's own DEVICE_ID, echoed back here purely so a log line
        # reads as a name instead of an opaque id -- never used for anything else.
        current_app.logger.info(
            "%s %s device=%s (%s)", request.method, request.path, device.device_name or "unnamed", device_id
        )
        return view(device_id, *args, **kwargs)

    return wrapped


def require_paired_session(view):
    """Protects browser-facing pages that act on 'whichever device this browser
    paired with'. The cookie only carries the device_id, so check the device still
    exists and is paired: unpair and forget must take effect on browsers too."""

    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        device = None
        if "device_id" in session:
            device = db.session.get(Device, session["device_id"])
        if device is None or device.paired_at is None:
            session.clear()
            return redirect(url_for("web.pair"))
        return view(*args, **kwargs)

    return wrapped
