import functools

from flask import jsonify, redirect, request, session, url_for
from werkzeug.security import check_password_hash

from .db import get_db


def require_device_auth(view):
    """Protects Pi-facing endpoints shaped /api/devices/<device_id>/... with the
    device's own bearer secret, set at registration time."""

    @functools.wraps(view)
    def wrapped(device_id, *args, **kwargs):
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return jsonify(error="unauthorized"), 401

        secret = auth_header.removeprefix("Bearer ")
        row = get_db().execute("SELECT device_secret_hash FROM devices WHERE device_id = ?", (device_id,)).fetchone()
        if row is None or not check_password_hash(row["device_secret_hash"], secret):
            return jsonify(error="unauthorized"), 401

        return view(device_id, *args, **kwargs)

    return wrapped


def require_paired_session(view):
    """Protects browser-facing pages that act on 'whichever device this browser
    paired with', established by entering a pairing code."""

    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if "device_id" not in session:
            return redirect(url_for("web.pair"))
        return view(*args, **kwargs)

    return wrapped
