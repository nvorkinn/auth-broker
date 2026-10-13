import functools
import hashlib
import hmac
from datetime import UTC, datetime, timedelta

from flask import current_app, g, jsonify, redirect, request, url_for
from sqlalchemy import select

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


def hash_secret(secret: str) -> str:
    """The stored form of a device secret. A plain unsalted SHA-256 is enough because secrets are
    long random strings, not passwords, and being deterministic lets a secret find its device."""
    return hashlib.sha256(secret.encode()).hexdigest()


def with_retry_after(response, seconds):
    """Sets Retry-After on a response, so a view can return (response, status) on every path."""
    response.headers["Retry-After"] = str(seconds)
    return response


def require_device_auth(roles=frozenset(ROLES)):
    """Protects device-facing endpoints with one of the device's own bearer secrets, which alone
    finds the device. The secret that matches decides the caller's role: a valid secret
    for a role not in `roles` is a 403, anything else a 401, except a secret still waiting in the
    pending pool, which is a 202. Sets g.device_id and g.role, and passes device_id to the view."""

    def decorator(view):
        @functools.wraps(view)
        def wrapped(*args, **kwargs):
            auth_header = request.headers.get("Authorization", "")
            secret = auth_header.removeprefix("Bearer ")
            if not auth_header.startswith("Bearer ") or not secret:
                return jsonify(error="unauthorized"), 401

            device, role = _find_by_secret(secret)
            if device is None and db.session.get(PendingRegistration, hash_secret(secret)) is not None:
                return with_retry_after(jsonify(status="waiting"), WAITING_RETRY_AFTER), 202
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
            _mark_seen(device, role)
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


def _mark_seen(device: Device, role: str) -> None:
    now = datetime.now(UTC)
    stale = {"last_seen_at": device.last_seen_at}
    if role == "display":
        stale["display_last_seen_at"] = device.display_last_seen_at
    changed = False
    for column, seen in stale.items():
        if seen is None or now - LAST_SEEN_RESOLUTION >= seen:
            setattr(device, column, now)
            changed = True
    if changed:
        db.session.commit()


def _require_bearer_token(config_key: str):
    """Protects an endpoint with the token in the broker's setting `config_key`. With none set the
    endpoint is off and answers 404, as if it didn't exist."""

    def decorator(view):
        @functools.wraps(view)
        def wrapped(*args, **kwargs):
            expected = current_app.config[config_key]
            if not expected:
                return jsonify(error="not found"), 404

            auth_header = request.headers.get("Authorization", "")
            secret = auth_header.removeprefix("Bearer ")
            if not auth_header.startswith("Bearer ") or not hmac.compare_digest(secret, expected):
                return jsonify(error="unauthorized"), 401
            return view(*args, **kwargs)

        return wrapped

    return decorator


# The read-only status endpoint's own token, and the orchestrator's: neither is a device secret.
require_status_token = _require_bearer_token("STATUS_TOKEN")
require_orchestrator_token = _require_bearer_token("ORCHESTRATOR_TOKEN")


def current_user() -> str | None:
    """The signed-in user's id: the header the login proxy sets on every browser-facing request,
    which the broker trusts because only the proxy can reach it (see caddy/Caddyfile)."""
    return request.headers.get(current_app.config["USER_HEADER"], "").strip() or None


def paired_device_id() -> str | None:
    """The device the signed-in user is paired with (the one paired most recently, if several)."""
    user = current_user()
    if user is None:
        return None
    return db.session.scalar(
        select(Device.device_id)
        .where(Device.owner == user, Device.paired_at.is_not(None))
        .order_by(Device.paired_at.desc())
        .limit(1)
    )


def require_user(view):
    """Protects browser pages that only need someone signed in. Without the proxy's header the
    request didn't come through the login, so it's a 401."""

    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if current_user() is None:
            return "Sign in required.", 401
        return view(*args, **kwargs)

    return wrapped


def require_paired_user(view):
    """Protects browser pages that act on 'the signed-in user's device': a user with none goes to
    /pair. Sets g.device_id. Looked up on every request, so unpairing and forgetting take effect at once."""

    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if current_user() is None:
            return "Sign in required.", 401
        g.device_id = paired_device_id()
        if g.device_id is None:
            return redirect(url_for("pages.pair"))
        return view(*args, **kwargs)

    return wrapped
