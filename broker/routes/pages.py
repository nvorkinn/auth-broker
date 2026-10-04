"""The recipient-facing site: pairing a browser with a device, and the device's settings page."""

import re

from flask import Blueprint, current_app, redirect, render_template, request, session, url_for

from ..auth import require_paired_session
from ..clients import tfl, weather
from ..db import db
from ..models import DeviceConfig, GlowmarktCredentials, SpotifyToken
from ..services import pairing

bp = Blueprint("pages", __name__)

# UK postcode shape: outward code (e.g. SW1A, N1, EC2V) then inward code (digit + two letters).
UK_POSTCODE_RE = re.compile(r"^([A-Z]{1,2}[0-9][A-Z0-9]?)([0-9][A-Z]{2})$")


def normalize_postcode(value: str) -> str | None:
    """Returns the postcode in canonical form ("SW1A 1AA"), "" if blank, or None if it isn't a valid UK postcode."""
    compact = re.sub(r"\s+", "", value).upper()
    if not compact:
        return ""
    match = UK_POSTCODE_RE.match(compact)
    return f"{match[1]} {match[2]}" if match else None


@bp.get("/")
def index():
    return redirect(url_for("pages.device_config") if "device_id" in session else url_for("pages.pair"))


@bp.route("/pair", methods=["GET", "POST"])
def pair():
    if request.method == "GET":
        return render_template("pair.html", error=None)

    # Codes are guessable given enough tries, so wrong ones are throttled per IP; a locked-out IP is
    # refused even with the right code, or the lockout wouldn't slow guessing down.
    throttle = current_app.extensions["pair_throttle"]
    ip = request.remote_addr or "unknown"
    retry_after = throttle.retry_after(ip)
    if retry_after is not None:
        return _too_many_attempts(retry_after)

    device_id = pairing.redeem_code(request.form.get("code", "").strip().upper())
    if device_id is None:
        lockout = throttle.record_failure(ip)
        current_app.logger.warning("Wrong pairing code from %s", ip)
        if lockout is not None:
            current_app.logger.warning("Too many wrong pairing codes from %s; locked out for %ss", ip, lockout)
            return _too_many_attempts(lockout)
        return render_template("pair.html", error="That code is invalid or has expired.")

    session.clear()
    session.permanent = True
    session["device_id"] = device_id
    return redirect(url_for("pages.device_config"))


def _too_many_attempts(retry_after: int):
    minutes = -(-retry_after // 60)
    wait = "a minute" if minutes == 1 else f"{minutes} minutes"
    error = f"Too many incorrect codes. Please wait {wait} and try again."
    return render_template("pair.html", error=error), 429, {"Retry-After": str(retry_after)}


@bp.route("/device", methods=["GET", "POST"])
@require_paired_session
def device_config():
    device_id = session["device_id"]

    if request.method == "POST":
        raw_stops = request.form.get("stops_order", "")
        stop_ids = [s.strip() for s in raw_stops.split(",") if s.strip()]

        interval_val = request.form.get("interval", "15").strip()
        interval = int(interval_val) if interval_val.isdigit() and int(interval_val) > 0 else 15

        weather_location = request.form.get("weather_location", "").strip()
        spotify_enabled = request.form.get("spotify_enabled") == "on"

        raw_postcode = request.form.get("postcode", "")
        postcode = normalize_postcode(raw_postcode)

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
        if errors:
            return _render_device_config(device_id, errors=[*errors, "Nothing was saved."]), 400

        config = db.session.get(DeviceConfig, device_id)
        config.interval = interval
        config.weather_location = weather_location
        config.postcode = postcode
        config.spotify_enabled = spotify_enabled
        config.tfl_stop_ids = stop_ids

        # A blank password field keeps the stored password.
        glowmarkt_password = request.form.get("glowmarkt_password", "")
        creds = db.session.get(GlowmarktCredentials, device_id) or GlowmarktCredentials(device_id=device_id)
        creds.username = request.form.get("glowmarkt_username", "").strip()
        if glowmarkt_password:
            creds.password = glowmarkt_password
        db.session.add(creds)

        db.session.commit()
        return redirect(url_for("pages.device_config"))

    return _render_device_config(device_id)


def _render_device_config(device_id: str, errors: list[str] | None = None):
    config = db.session.get(DeviceConfig, device_id)
    creds = db.session.get(GlowmarktCredentials, device_id)
    glowmarkt = {
        "username": creds.username if creds else "",
        "has_password": bool(creds and creds.password_encrypted),
    }

    return render_template(
        "device.html",
        device_id=device_id,
        config=config,
        stops=tfl.resolve_stops(config.tfl_stop_ids),
        spotify_linked=db.session.get(SpotifyToken, device_id) is not None,
        glowmarkt=glowmarkt,
        errors=errors or [],
    )


@bp.post("/device/spotify/disconnect")
@require_paired_session
def spotify_disconnect():
    token = db.session.get(SpotifyToken, session["device_id"])
    if token is not None:
        db.session.delete(token)
        db.session.commit()
    return redirect(url_for("pages.device_config"))
