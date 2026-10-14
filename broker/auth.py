import functools
import hashlib
import hmac
from datetime import UTC, datetime, timedelta

from flask import current_app, g, jsonify, redirect, request, url_for
from sqlalchemy import select

from .db import db
from .models import Device

# How stale devices.last_seen_at may get before a request updates it. Some device endpoints are
# polled every few seconds; this keeps that from being a database write each time.
LAST_SEEN_RESOLUTION = timedelta(minutes=1)


# The roles a device's secrets can hold, in the order a presented secret is checked against them.
# Each has its own column, devices.<role>_secret_hash.
ROLES = ("renderer", "display")


def hash_secret(secret: str) -> str:
    """The stored form of a device secret. A plain unsalted SHA-256 is enough because secrets are
    long random strings, not passwords, and being deterministic lets a secret find its device."""
    return hashlib.sha256(secret.encode()).hexdigest()


def with_retry_after(response, seconds):
    """Sets Retry-After on a response, so a view can return (response, status) on every path."""
    response.headers["Retry-After"] = str(seconds)
    return response


class DeviceAuthError(Exception):
    """A request that didn't authenticate: `status` is 401 (no valid secret) or 403 (a valid secret for a role
    the endpoint doesn't allow)."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def authenticate_device(roles=frozenset(ROLES)) -> str:
    """Checks the request's bearer secret, which alone finds the device, and returns the device's id. The
    secret that matches decides the caller's role: a valid secret for a role not in `roles` is a 403, anything
    else a 401 (DeviceAuthError). Sets g.device_id and g.role."""
    auth_header = request.headers.get("Authorization", "")
    secret = auth_header.removeprefix("Bearer ")
    if not auth_header.startswith("Bearer ") or not secret:
        raise DeviceAuthError(401, "unauthorized")

    device, role = _find_by_secret(secret)
    if role is None:
        raise DeviceAuthError(401, "unauthorized")
    if role not in roles:
        raise DeviceAuthError(403, "forbidden")

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
    return device.device_id


def require_device_auth(roles=frozenset(ROLES)):
    """Protects device-facing endpoints with one of the device's own bearer secrets (see authenticate_device)
    and passes device_id to the view."""

    def decorator(view):
        @functools.wraps(view)
        def wrapped(*args, **kwargs):
            try:
                device_id = authenticate_device(roles)
            except DeviceAuthError as error:
                return jsonify(error=error.message), error.status
            return view(*args, device_id=device_id, **kwargs)

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


def current_username() -> str | None:
    """The signed-in user's username, from the header the login proxy sets next to the user id. It's only
    for display and for naming their screens: it's not the user's identity (that's current_user) and it
    can change."""
    return request.headers.get(current_app.config["USERNAME_HEADER"], "").strip() or None


def user_devices() -> list[Device]:
    """The devices the signed-in user is paired with, the one paired most recently first."""
    user = current_user()
    if user is None:
        return []
    return list(
        db.session.scalars(
            select(Device).where(Device.owner == user, Device.paired_at.is_not(None)).order_by(Device.paired_at.desc())
        )
    )


def paired_device_id() -> str | None:
    """The device the signed-in user is paired with (the one paired most recently, if several)."""
    devices = user_devices()
    return devices[0].device_id if devices else None


def screen_label(device: Device) -> str:
    """What a screen is called on the pages: its name (the slug), "New screen" while it's waiting to be named,
    or what it reports, or a piece of its id."""
    if device.assignment:
        return device.assignment.slug
    if device.renderer_secret_hash is None:
        return "New screen"  # not named yet, so it has no renderer to report a name
    return device.device_name or device.device_id[:6]


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
    """Protects browser pages that act on one of 'the signed-in user's devices'. A page with a `device_id`
    in its URL is for that device, and 404s unless it's the user's (never admits it exists); one without
    is for the one paired most recently. A user with none goes to /pair. Sets g.device_id, and the
    view doesn't get the URL's device_id. Looked up on every request, so unpairing and forgetting take
    effect at once."""

    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if current_user() is None:
            return "Sign in required.", 401
        requested = kwargs.pop("device_id", None)
        owned = [device.device_id for device in user_devices()]
        if requested is None:
            if not owned:
                return redirect(url_for("pages.pair"))
            g.device_id = owned[0]
        elif requested in owned:
            g.device_id = requested
        else:
            return jsonify(error="not found"), 404
        return view(*args, **kwargs)

    return wrapped
