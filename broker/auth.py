import functools
import hashlib
import hmac
from datetime import UTC, datetime, timedelta

from flask import current_app, g, jsonify, redirect, request, session, url_for
from sqlalchemy import select
from werkzeug.security import check_password_hash

from .db import db
from .models import Device, PendingRegistration

# How stale devices.last_seen_at may get before a request updates it. Some device endpoints are
# polled every few seconds; this keeps that from being a database write each time.
LAST_SEEN_RESOLUTION = timedelta(minutes=1)


# The roles a device's secrets can hold, in the order a presented secret is checked against them.
# Each has its own column, devices.<role>_secret_hash.
ROLES = ("renderer", "display")

# Retry-After, in seconds, on a 202 to a client still waiting in the pending pool: how often it should
# check back. Matching waits on a renderer being started, which can take a while.
WAITING_RETRY_AFTER = 30

# Werkzeug's password-hash formats, which every secret was stored as before secrets became the
# device's identifier. They're salted, so can't be looked up; one is rewritten as hash_secret()'s
# SHA-256 the first time it verifies.
_LEGACY_HASH_PREFIXES = ("scrypt:", "pbkdf2:")


def hash_secret(secret: str) -> str:
    """The stored form of a device secret. A plain unsalted SHA-256 is enough because secrets are
    long random strings, not passwords, and being deterministic lets a secret find its device."""
    return hashlib.sha256(secret.encode()).hexdigest()


def require_device_auth(roles=frozenset(ROLES)):
    """Protects device-facing endpoints with one of the device's own bearer secrets. On a route
    shaped /.../<device_id>/... the secret must belong to that device; on a route without one, the
    secret alone finds the device. The secret that matches decides the caller's role: a valid secret
    for a role not in `roles` is a 403, anything else a 401, except a secret still waiting in the
    pending pool, which is a 202. Sets g.device_id and g.role, and passes device_id to the view."""

    def decorator(view):
        @functools.wraps(view)
        def wrapped(*args, device_id=None, **kwargs):
            auth_header = request.headers.get("Authorization", "")
            secret = auth_header.removeprefix("Bearer ")
            if not auth_header.startswith("Bearer ") or not secret:
                return jsonify(error="unauthorized"), 401

            if device_id is None:
                device, role = _find_by_secret(secret)
                if device is None and db.session.get(PendingRegistration, hash_secret(secret)) is not None:
                    return jsonify(status="waiting"), 202, {"Retry-After": str(WAITING_RETRY_AFTER)}
            else:
                device = db.session.get(Device, device_id)
                role = _matching_role(device, secret) if device is not None else None
            if role is None:
                return jsonify(error="unauthorized"), 401
            if role not in roles:
                return jsonify(error="forbidden"), 403

            g.device_id, g.role = device.device_id, role
            # device_name is just the Pi's own DEVICE_ID, echoed back here purely so a log line
            # reads as a name instead of an opaque id -- never used for anything else.
            current_app.logger.info(
                "%s %s device=%s (%s) role=%s",
                request.method,
                request.path,
                device.device_name or "unnamed",
                device.device_id,
                role,
            )
            _mark_seen(device)
            return view(*args, device_id=device.device_id, **kwargs)

        return wrapped

    return decorator


def _find_by_secret(secret: str) -> tuple[Device | None, str | None]:
    # An indexed lookup rather than a constant-time compare: the timing can only reveal something
    # about the hash, and no one can work back from a hash to a secret that produces it.
    secret_hash = hash_secret(secret)
    for role in ROLES:
        column = getattr(Device, f"{role}_secret_hash")
        device = db.session.scalar(select(Device).where(column == secret_hash))
        if device is not None:
            return device, role
    return None, None


def _matching_role(device: Device, secret: str) -> str | None:
    for role in ROLES:
        column = f"{role}_secret_hash"
        stored = getattr(device, column)
        if stored is None:
            continue
        if stored.startswith(_LEGACY_HASH_PREFIXES):
            if check_password_hash(stored, secret):
                setattr(device, column, hash_secret(secret))
                db.session.commit()
                return role
        elif hmac.compare_digest(stored, hash_secret(secret)):
            return role
    return None


def _mark_seen(device: Device) -> None:
    now = datetime.now(UTC)
    if device.last_seen_at is None or now - LAST_SEEN_RESOLUTION >= device.last_seen_at:
        device.last_seen_at = now
        db.session.commit()


def require_status_token(view):
    """Protects the read-only status endpoint with STATUS_TOKEN from the environment. With no token
    set the endpoint is off and answers 404, as if it didn't exist."""

    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        expected = current_app.config["STATUS_TOKEN"]
        if not expected:
            return jsonify(error="not found"), 404

        auth_header = request.headers.get("Authorization", "")
        secret = auth_header.removeprefix("Bearer ")
        if not auth_header.startswith("Bearer ") or not hmac.compare_digest(secret, expected):
            return jsonify(error="unauthorized"), 401
        return view(*args, **kwargs)

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
            return redirect(url_for("pages.pair"))
        return view(*args, **kwargs)

    return wrapped
