"""The recipient-facing site: pairing the signed-in user with a device, and the device's settings page."""

import re

from flask import Blueprint, current_app, g, jsonify, redirect, render_template, request, url_for

from ..auth import (
    current_user,
    current_username,
    paired_device_id,
    require_paired_user,
    require_user,
    screen_label,
    user_devices,
)
from ..clients import tfl, weather
from ..db import db
from ..models import ORIENTATIONS, Device, DeviceConfig, GlowmarktCredentials, SpotifyToken
from ..services import assignments, pairing

bp = Blueprint("pages", __name__)

PAIRING_TOKEN_RE = re.compile(r"^[0-9a-f]{64}$")

# UK postcode shape: outward code (e.g. SW1A, N1, EC2V) then inward code (digit + two letters).
UK_POSTCODE_RE = re.compile(r"^([A-Z]{1,2}[0-9][A-Z0-9]?)([0-9][A-Z]{2})$")


def normalize_postcode(value: str) -> str | None:
    """Returns the postcode in canonical form ("SW1A 1AA"), "" if blank, or None if it isn't a valid UK postcode."""
    compact = re.sub(r"\s+", "", value).upper()
    if not compact:
        return ""
    match = UK_POSTCODE_RE.match(compact)
    return f"{match[1]} {match[2]}" if match else None


@bp.app_context_processor
def screens():
    """The signed-in user's screens, for the switcher at the top of every page: [{id, label, current}]."""
    devices = user_devices()
    if not devices:
        return {"screens": []}
    current = g.get("device_id") or devices[0].device_id
    return {
        "screens": [{"id": d.device_id, "label": screen_label(d), "current": d.device_id == current} for d in devices]
    }


@bp.get("/")
def index():
    return redirect(url_for("pages.device_config") if paired_device_id() else url_for("pages.pair"))


@bp.route("/pair", methods=["GET", "POST"])
@require_user
def pair():
    """Pairs the signed-in user with a device. A device that has just finished WiFi setup sends its
    owner's browser here with a pairing token in the URL fragment (or, if it can't set one, ?t=):
    the page waits for the device to register, then goes to its settings. Without a token it asks for
    the code the device shows."""
    if request.method == "GET":
        return render_template("pair.html", error=None, token=request.args.get("t", ""))

    # Codes are guessable given enough tries, so wrong ones are throttled per IP; a locked-out IP is
    # refused even with the right code, or the lockout wouldn't slow guessing down.
    throttle = current_app.extensions["pair_throttle"]
    ip = request.remote_addr or "unknown"
    retry_after = throttle.retry_after(ip)
    if retry_after is not None:
        return _too_many_attempts(retry_after)

    device_id = pairing.redeem_code(request.form.get("code", "").strip().upper(), current_user())
    if device_id is None:
        lockout = throttle.record_failure(ip)
        current_app.logger.warning("Wrong pairing code from %s", ip)
        if lockout is not None:
            current_app.logger.warning("Too many wrong pairing codes from %s; locked out for %ss", ip, lockout)
            return _too_many_attempts(lockout)
        return render_template("pair.html", error="That code is invalid or has expired.", token="")

    return redirect(url_for("pages.device_config"))


def _too_many_attempts(retry_after: int):
    minutes = -(-retry_after // 60)
    wait = "a minute" if minutes == 1 else f"{minutes} minutes"
    error = f"Too many incorrect codes. Please wait {wait} and try again."
    return render_template("pair.html", error=error, token=""), 429, {"Retry-After": str(retry_after)}


@bp.post("/pair/token")
@require_user
def pair_by_token():
    """Polled by the pairing page: 200 once the user is paired, 202 while the device hasn't
    registered yet, 409 if someone else owns it. The token is 256 random-looking bits, so there's
    nothing to throttle."""
    token = (request.get_json(silent=True) or {}).get("token", "")
    if not isinstance(token, str) or not PAIRING_TOKEN_RE.match(token):
        return jsonify(status="invalid"), 400
    status = pairing.pair_by_token(current_user(), token)
    return jsonify(status=status), {"paired": 200, "waiting": 202, "taken": 409}[status]


# Every page about a screen is at /device/<id>/..., and at /device/... for the user's most recently paired one.
@bp.route("/device", methods=["GET", "POST"], defaults={"device_id": None})
@bp.route("/device/<device_id>", methods=["GET", "POST"])
@require_paired_user
def device_config():
    device_id = g.device_id
    # A screen with no renderer yet is named first: nothing else on this page works until it has one.
    if assignments.needs_renderer(db.session.get(Device, device_id)):
        return redirect(url_for("pages.name_screen", device_id=device_id))

    if request.method == "POST":
        raw_stops = request.form.get("stops_order", "")
        stop_ids = [s.strip() for s in raw_stops.split(",") if s.strip()]

        interval_val = request.form.get("interval", "15").strip()
        interval = int(interval_val) if interval_val.isdigit() and int(interval_val) > 0 else 15

        weather_location = request.form.get("weather_location", "").strip()
        spotify_enabled = request.form.get("spotify_enabled") == "on"

        raw_postcode = request.form.get("postcode", "")
        postcode = normalize_postcode(raw_postcode)

        config = db.session.get(DeviceConfig, device_id)
        orientation = request.form.get("orientation", config.orientation)

        errors = []
        # Only a location Open-Meteo resolves is ever stored, so a stored one is one the device can use;
        # if it can't be checked right now, that's refused too rather than saved unverified.
        found = weather.location_found(weather_location) if weather_location else True
        if found is False:
            errors.append(
                f'We couldn\'t find "{weather_location}" for the weather: try just the town or city name, '
                'not a postcode (e.g. "Kennington" or "London").'
            )
        elif found is None:
            errors.append("We couldn't check the weather location just now. Please try again in a minute.")
        if postcode is None:
            errors.append(f'"{raw_postcode.strip()}" isn\'t a valid UK postcode.')
        if orientation not in ORIENTATIONS:
            errors.append(f"Orientation must be one of {', '.join(ORIENTATIONS)}.")
        if errors:
            return _render_device_config(device_id, errors=[*errors, "Nothing was saved."]), 400

        config.interval = interval
        config.weather_location = weather_location
        config.postcode = postcode
        config.spotify_enabled = spotify_enabled
        config.tfl_stop_ids = stop_ids
        config.orientation = orientation

        # A blank password field keeps the stored password.
        glowmarkt_password = request.form.get("glowmarkt_password", "")
        creds = db.session.get(GlowmarktCredentials, device_id) or GlowmarktCredentials(device_id=device_id)
        creds.username = request.form.get("glowmarkt_username", "").strip()
        if glowmarkt_password:
            creds.password = glowmarkt_password
        db.session.add(creds)

        db.session.commit()
        return redirect(url_for("pages.device_config", device_id=device_id))

    return _render_device_config(device_id)


@bp.route("/device/name", methods=["GET", "POST"], defaults={"device_id": None})
@bp.route("/device/<device_id>/name", methods=["GET", "POST"])
@require_paired_user
def name_screen():
    """Where a new screen is named, which asks for its renderer to be started (see
    services/assignments.py). The user is sent here right after pairing, and never again once it's done."""
    device = db.session.get(Device, g.device_id)
    if not assignments.needs_renderer(device):
        return redirect(url_for("pages.device_config", device_id=g.device_id))

    error = None
    slug = request.form.get("slug", "").strip().lower()
    if request.method == "POST":
        try:
            assignments.create(device, slug, current_username())
            return redirect(url_for("pages.device_config", device_id=g.device_id))
        except assignments.InvalidSlug:
            error = (
                f"Use up to {assignments.max_suffix_length(current_username())} lowercase letters, numbers, "
                "dashes or underscores, starting with a letter or number."
            )
        except assignments.SlugTaken:
            error = "That name is taken. Try another."
        except assignments.NotNeeded:
            return redirect(url_for("pages.device_config", device_id=g.device_id))
    return render_template(
        "name_screen.html",
        device_id=g.device_id,
        slug=slug,
        prefix=assignments.slug_prefix(current_username()),
        max_length=assignments.max_suffix_length(current_username()),
        error=error,
    ), 400 if error else 200


@bp.post("/api/displays/<device_id>/renderer")
@require_user
def request_renderer(device_id):
    """{"slug": "lisa"}: names the signed-in user's screen, which asks for its renderer (the same as
    the form above). 201 {id, slug, status}; 400 for a bad slug, 404 for a device that isn't the
    user's, 409 for a slug that's taken or a screen that already has its renderer."""
    device = db.session.get(Device, device_id)
    if device is None or device.owner != current_user() or device.paired_at is None:
        return jsonify(error="not found"), 404
    slug = (request.get_json(silent=True) or {}).get("slug", "")
    try:
        assignment = assignments.create(device, slug if isinstance(slug, str) else "", current_username())
    except assignments.InvalidSlug:
        return jsonify(error="slug must match [a-z0-9][a-z0-9_-]* and fit in 32 characters with the prefix"), 400
    except (assignments.SlugTaken, assignments.NotNeeded) as e:
        return jsonify(error="slug is taken" if isinstance(e, assignments.SlugTaken) else "already has a renderer"), 409
    return jsonify(id=assignment.id, slug=assignment.slug, status=assignment.status), 201


# The page always sets this orientation and doesn't let it be changed, whatever is stored.
PAGE_ORIENTATION = "vertical"


def _render_device_config(device_id: str, errors: list[str] | None = None):
    config = db.session.get(DeviceConfig, device_id)
    creds = db.session.get(GlowmarktCredentials, device_id)
    glowmarkt = {
        "username": creds.username if creds else "",
        "has_password": bool(creds and creds.password_encrypted),
    }

    device = db.session.get(Device, device_id)
    return render_template(
        "device.html",
        device_id=device_id,
        display_attached=device.display_secret_hash is not None,
        assignment=device.assignment,
        config=config,
        orientations=ORIENTATIONS,
        page_orientation=PAGE_ORIENTATION,
        stops=tfl.resolve_stops(config.tfl_stop_ids),
        spotify_linked=db.session.get(SpotifyToken, device_id) is not None,
        glowmarkt=glowmarkt,
        errors=errors or [],
    )


@bp.post("/device/spotify/disconnect", defaults={"device_id": None})
@bp.post("/device/<device_id>/spotify/disconnect")
@require_paired_user
def spotify_disconnect():
    token = db.session.get(SpotifyToken, g.device_id)
    if token is not None:
        db.session.delete(token)
        db.session.commit()
    return redirect(url_for("pages.device_config", device_id=g.device_id))


@bp.post("/device/display/unlink", defaults={"device_id": None})
@bp.post("/device/<device_id>/display/unlink")
@require_paired_user
def display_unlink():
    """Forgets the screen's secret, so it gets a 401 from then on: for a screen that's lost, stolen or
    retired. Registering again puts it in the pending pool as a new device."""
    db.session.get(Device, g.device_id).display_secret_hash = None
    db.session.commit()
    return redirect(url_for("pages.device_config", device_id=g.device_id))
