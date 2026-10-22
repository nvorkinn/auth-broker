"""How the broker knows who is asking: devices by their bearer secrets, browsers by the login proxy's headers."""

import functools
import hashlib
import hmac
from collections.abc import Callable, Collection
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from flask import Response, abort, current_app, g, jsonify, redirect, request, url_for
from sqlalchemy import select

from .db import db
from .models import Device

if TYPE_CHECKING:
    from flask.typing import ResponseReturnValue

# A Flask view function, as the decorators here take and return it.
type View = Callable[..., ResponseReturnValue]

# How stale a device's last-seen times may get before a request updates them, so a screen's polls aren't each a
# database write.
LAST_SEEN_RESOLUTION = timedelta(minutes=1)


# The roles a device's secrets can hold, in the order a presented secret is checked against them, each with its own
# column, devices.<role>_secret_hash. There is one: the screen's.
ROLES = ("display",)


def hash_secret(secret: str) -> str:
    """Return the stored form of a device secret.

    A plain unsalted SHA-256 is enough because secrets are long random strings, not passwords, and being
    deterministic lets a secret find its device.
    """
    return hashlib.sha256(secret.encode()).hexdigest()


def with_retry_after(response: Response, seconds: int) -> Response:
    """Set Retry-After on a response, so a view can return (response, status) on every path."""
    response.headers["Retry-After"] = str(seconds)
    return response


class DeviceAuthError(Exception):
    """A request that device auth turns away.

    `status` is 401 for a missing, malformed or unknown secret and 403 for a valid one whose role isn't allowed
    here; `message` is the `error` it is answered with.
    """

    def __init__(self, status: int, message: str) -> None:
        """Turn the request away with `status`, answering `message`."""
        super().__init__(message)
        self.status = status
        self.message = message


def authenticate_device(authorization: str, roles: Collection[str] = frozenset(ROLES)) -> Device:
    """Identify the device behind an `Authorization: Bearer <secret>` header, whose secret alone finds it.

    The secret that matches decides the caller's role: a valid secret for a role not in `roles` is a 403,
    anything else a 401 (DeviceAuthError). Sets g.device_id and g.role, logs the request and records the
    device as seen. The prefix is case-sensitive: `bearer x` is refused.
    """
    secret = authorization.removeprefix("Bearer ")
    if not authorization.startswith("Bearer ") or not secret:
        raise DeviceAuthError(401, "unauthorized")

    device, role = _find_by_secret(secret)
    if role is None:
        raise DeviceAuthError(401, "unauthorized")
    if role not in roles:
        raise DeviceAuthError(403, "forbidden")

    g.device_id, g.role = device.device_id, role
    # The slug, not the friendly name (device_name): that's whatever its owner typed, which could name a place, and
    # nothing logged may.
    current_app.logger.info(
        "%s %s device=%s (%s) role=%s",
        request.method,
        request.path,
        device.assignment.slug if device.assignment else "unnamed",
        device.device_id,
        role,
    )
    _mark_seen(device, role)
    return device


def require_device_auth(roles: Collection[str] = frozenset(ROLES)) -> Callable[[View], View]:
    """Protect a device-facing endpoint with the device's bearer secret (authenticate_device), passing device_id."""

    def decorator(view: View) -> View:
        @functools.wraps(view)
        def wrapped(*args: object, **kwargs: object) -> ResponseReturnValue:
            try:
                device = authenticate_device(request.headers.get("Authorization", ""), roles)
            except DeviceAuthError as error:
                return jsonify(error=error.message), error.status
            return view(*args, device_id=device.device_id, **kwargs)

        # For the test that keeps caddy/Caddyfile from putting a device route behind the browser login.
        wrapped.device_auth = True  # type: ignore[attr-defined]
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


def _require_bearer_token(config_key: str) -> Callable[[View], View]:
    """Protect an endpoint with the token in the broker's setting `config_key`.

    With none set the endpoint is off and answers 404, as if it didn't exist.
    """

    def decorator(view: View) -> View:
        @functools.wraps(view)
        def wrapped(*args: object, **kwargs: object) -> ResponseReturnValue:
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


# The read-only status endpoint's own token, which isn't a device secret.
require_status_token = _require_bearer_token("STATUS_TOKEN")


def current_user() -> str | None:
    """Return the signed-in user's id, from the header the login proxy sets.

    The broker trusts it because only the proxy can reach it (see caddy/Caddyfile).
    """
    return request.headers.get(current_app.config["USER_HEADER"], "").strip() or None


def require_current_user() -> str:
    """Return the signed-in user's id, or turn the request away with a 401 if there is none.

    For a view that needs the id as a `str`: current_user can't promise one, though behind require_user it
    always has. The 401 is the one require_user answers.
    """
    user = current_user()
    if user is None:
        abort(Response("Sign in required.", 401))
    return user


def current_username() -> str | None:
    """Return the signed-in user's username, from the header the login proxy sets next to the user id.

    It's only for display: it's not the user's identity (that's current_user) and it can change.
    """
    return request.headers.get(current_app.config["USERNAME_HEADER"], "").strip() or None


def user_devices() -> list[Device]:
    """Return the devices the signed-in user is paired with, the one paired most recently first."""
    user = current_user()
    if user is None:
        return []
    return list(
        db.session.scalars(
            select(Device).where(Device.owner == user, Device.paired_at.is_not(None)).order_by(Device.paired_at.desc())
        )
    )


def paired_device_id() -> str | None:
    """Return the device the signed-in user is paired with (the one paired most recently, if several)."""
    devices = user_devices()
    return devices[0].device_id if devices else None


def screen_label(device: Device) -> str:
    """Return what a screen is called on the pages.

    That is its friendly name, else a slug its owner chose, else "Screen" and a piece of its id (a slug that is the
    device id is never shown).
    """
    if device.device_name:
        return device.device_name
    if device.assignment and device.assignment.slug != device.device_id:
        return device.assignment.slug
    return f"Screen {device.device_id[:6]}"


def require_user(view: View) -> View:
    """Protect browser pages that only need someone signed in.

    Without the proxy's header the request didn't come through the login, so it's a 401.
    """

    @functools.wraps(view)
    def wrapped(*args: object, **kwargs: object) -> ResponseReturnValue:
        if current_user() is None:
            return "Sign in required.", 401
        return view(*args, **kwargs)

    return wrapped


def require_paired_user(view: View) -> View:
    """Protect browser pages that act on one of the signed-in user's devices.

    A page with a `device_id` in its URL is for that device, and 404s unless it's the user's (never admits it
    exists); one without is for the one paired most recently. A user with none goes to /pair. Sets g.device_id,
    and the view doesn't get the URL's device_id. Looked up on every request, so unpairing and forgetting take
    effect at once.
    """

    @functools.wraps(view)
    def wrapped(*args: object, **kwargs: object) -> ResponseReturnValue:
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
