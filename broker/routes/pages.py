"""The recipient-facing site: pairing the signed-in user with a device, and the device's settings page."""

import re
from http import HTTPStatus
from typing import TYPE_CHECKING, Any

from flask import Blueprint, Response, current_app, g, jsonify, redirect, render_template, request, url_for
from redis import Redis, RedisError

from broker.auth import (
    current_user,
    current_username,
    paired_device_id,
    require_paired_user,
    require_user,
    screen_label,
    user_devices,
)
from broker.clients import apple_music, weather
from broker.db import db
from broker.models import (
    CLOCK_FORMATS,
    LANGUAGES,
    ORIENTATIONS,
    STYLES,
    Device,
    DeviceConfig,
    Frame,
    GitHubToken,
    GlowmarktCredentials,
    SpotifyToken,
)
from broker.services import assignments, frame_image, geoip, pace, pairing, stops, weather_place

if TYPE_CHECKING:
    from flask.typing import ResponseReturnValue

bp = Blueprint("pages", __name__)

PAIRING_TOKEN_RE = re.compile(r"^[0-9a-f]{64}$")

# UK postcode shape: outward code (e.g. SW1A, N1, EC2V) then inward code (digit + two letters).
UK_POSTCODE_RE = re.compile(r"^([A-Z]{1,2}[0-9][A-Z0-9]?)([0-9][A-Z]{2})$")
# A Norwegian postcode is four digits ("0150"); a UK one always has letters, so the two can't be confused.
NORWEGIAN_POSTCODE_RE = re.compile(r"^[0-9]{4}$")


def normalize_postcode(value: str) -> str | None:
    """Return the postcode in canonical form ("SW1A 1AA", "0150").

    Return "" if it is blank, or None if it is neither a UK nor a Norwegian postcode.
    """
    compact = re.sub(r"\s+", "", value).upper()
    if not compact:
        return ""
    if NORWEGIAN_POSTCODE_RE.match(compact):
        return compact
    match = UK_POSTCODE_RE.match(compact)
    return f"{match[1]} {match[2]}" if match else None


def postcode_country(postcode: str) -> str | None:
    """Return the country of a normalized postcode: "NO", "GB", or None for none."""
    if not postcode:
        return None
    return "NO" if NORWEGIAN_POSTCODE_RE.match(postcode) else "GB"


def _write_subscriptions_to_redis() -> None:
    configs = db.session.scalars(db.select(DeviceConfig)).all()
    subs: dict[str, set[str]] = {
        "tfl": set(),
        "entur": set(),
        "weather": set(),
        "glow": set(),
        "spotify": set(),
    }

    for config in configs:
        for stop_id in config.tfl_stop_ids:
            subs["entur" if stop_id.startswith("NSR:") else "tfl"].add(stop_id)
        if config.weather_location:
            subs["weather"].add(config.weather_location)
        if config.spotify_enabled:
            subs["spotify"].add(config.device_id)
    subs["glow"] = set(
        db.session.scalars(
            db.select(GlowmarktCredentials.device_id).where(GlowmarktCredentials.password_encrypted.is_not(None))
        )
    )
    redis: Redis = current_app.extensions["redis"]
    try:
        with redis.pipeline(transaction=True) as pipe:
            for name, members in subs.items():
                key = f"v1:sub:{name}"
                pipe.delete(key)
                if members:
                    pipe.sadd(key, *members)
            pipe.publish("v1:sub:changed", "")
            pipe.execute()
    except RedisError:
        current_app.logger.warning("Couldn't publish subscriptions to Redis", exc_info=True)


@bp.app_context_processor
def screens() -> dict[str, list[dict[str, str | bool]]]:
    """List the signed-in user's screens, for the switcher at the top of every page: [{id, label, current}]."""
    devices = user_devices()
    if not devices:
        return {"screens": []}
    current = g.get("device_id") or devices[0].device_id
    return {
        "screens": [{"id": d.device_id, "label": screen_label(d), "current": d.device_id == current} for d in devices]
    }


@bp.get("/")
def index() -> ResponseReturnValue:
    """Send the user to their screen's settings, or to pairing if they have none."""
    return redirect(url_for("pages.device_config") if paired_device_id() else url_for("pages.pair"))


@bp.get("/about")
def about() -> ResponseReturnValue:
    """Show the public page about the display, to anyone: no login, and nothing about the visitor or their screens."""
    return render_template("about.html")


@bp.route("/pair", methods=["GET", "POST"])
@require_user
def pair() -> ResponseReturnValue:
    """Pair the signed-in user with a device.

    A device that has just finished WiFi setup sends its owner's browser here with a pairing token in the URL
    fragment (or, if it can't set one, ?t=): the page waits for the device to register, then goes to its settings.
    Without a token it asks for the code the device shows.
    """
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


def _too_many_attempts(retry_after: int) -> ResponseReturnValue:
    minutes = -(-retry_after // 60)
    wait = "a minute" if minutes == 1 else f"{minutes} minutes"
    error = f"Too many incorrect codes. Please wait {wait} and try again."
    return render_template("pair.html", error=error, token=""), 429, {"Retry-After": str(retry_after)}


@bp.post("/pair/token")
@require_user
def pair_by_token() -> ResponseReturnValue:
    """Pair the signed-in user with the device a pairing token is for.

    The pairing page polls this: 200 once the user is paired, 202 while the device hasn't registered yet,
    409 if someone else owns it. The token is 256 random-looking bits, so there's nothing to throttle.
    """
    token = (request.get_json(silent=True) or {}).get("token", "")
    if not isinstance(token, str) or not PAIRING_TOKEN_RE.match(token):
        return jsonify(status="invalid"), 400
    status = pairing.pair_by_token(current_user(), token)
    return jsonify(status=status), {"paired": 200, "waiting": 202, "taken": 409}[status]


# Every page about a screen is at /device/<id>/..., and at /device/... for the user's most recently paired one.
@bp.route("/device", methods=["GET", "POST"], defaults={"device_id": None})
@bp.route("/device/<device_id>", methods=["GET", "POST"])
@require_paired_user
def device_config() -> ResponseReturnValue:
    """Show a screen's settings page, and save the settings posted from it."""
    device_id = g.device_id
    # A screen with no renderer yet is named first: nothing else on this page works until it has one.
    if assignments.needs_renderer(db.session.get(Device, device_id)):
        return redirect(url_for("pages.name_screen", device_id=device_id))

    if request.method == "POST":
        return _save_device_config(device_id)
    return _render_device_config(device_id)


# The settings picked from a fixed list, each by its form field (also its DeviceConfig attribute): the values
# allowed, and what an error message calls it. Errors are listed in this order.
CHOICES = {
    "language": (LANGUAGES, "The language"),
    "clock_format": (CLOCK_FORMATS, "The clock format"),
    "orientation": (ORIENTATIONS, "Orientation"),
    "style": (STYLES, "The style"),
}


def _save_device_config(device_id: str) -> ResponseReturnValue:
    """Save the settings posted from the page, or show it again with what's wrong and nothing saved."""
    raw_stops = request.form.get("stops_order", "")
    stop_ids = [s.strip() for s in raw_stops.split(",") if s.strip()]

    spotify_enabled = request.form.get("spotify_enabled") == "on"
    developer_enabled = request.form.get("developer_enabled") == "on"
    apple_music_enabled = request.form.get("apple_music_enabled") == "on"

    raw_postcode = request.form.get("postcode", "")
    postcode = normalize_postcode(raw_postcode)

    config = db.session.get(DeviceConfig, device_id)
    choices = {field: request.form.get(field, getattr(config, field)) for field in CHOICES}

    place, errors = _weather_place(config)
    if postcode is None:
        errors.append(f'"{raw_postcode.strip()}" isn\'t a valid UK or Norwegian postcode.')
    errors += [
        f"{label} must be one of {', '.join(allowed)}."
        for field, (allowed, label) in CHOICES.items()
        if choices[field] not in allowed
    ]
    if errors:
        return _render_device_config(device_id, errors=[*errors, "Nothing was saved."]), 400

    weather_place.store(config, place)
    config.postcode = postcode
    config.spotify_enabled = spotify_enabled
    config.developer_enabled = developer_enabled
    config.apple_music_enabled = apple_music_enabled
    config.tfl_stop_ids = stop_ids
    for field, value in choices.items():
        setattr(config, field, value)
    _save_glowmarkt_credentials(device_id)
    _save_github_token(device_id)

    db.session.commit()
    _write_subscriptions_to_redis()
    return redirect(url_for("pages.device_config", device_id=device_id))


def _weather_place(config: DeviceConfig) -> tuple[dict[str, Any] | None, list[str]]:
    """Return the weather place posted (None for none) and what's wrong with it, as a list of at most one error.

    The picker posts the id of the place picked (weather_place_id) beside the text in its box (weather_location);
    without JavaScript there's only the text, which is looked up instead (see weather_place.resolve).
    """
    text = request.form.get("weather_location", "").strip()
    # Only a place Open-Meteo knows is ever stored, so a stored one is one the device can use; if it can't be
    # checked right now, that's refused too rather than saved unverified.
    try:
        return weather_place.resolve(request.form.get("weather_place_id", ""), text, config), []
    except weather_place.PlaceNotFoundError:
        return None, [
            (
                f'We couldn\'t find "{text}" for the weather: try just the town or city name, '
                'not a postcode (e.g. "Kennington" or "Oslo").'
            )
        ]
    except weather.GeocodingUnavailableError:
        return None, ["We couldn't check the weather location just now. Please try again in a minute."]


def _save_glowmarkt_credentials(device_id: str) -> None:
    """Stage the posted Glowmarkt username, and password unless its field was left blank."""
    # A blank password field keeps the stored password.
    glowmarkt_password = request.form.get("glowmarkt_password", "")
    creds = db.session.get(GlowmarktCredentials, device_id) or GlowmarktCredentials(device_id=device_id)
    creds.username = request.form.get("glowmarkt_username", "").strip()
    if glowmarkt_password:
        creds.password = glowmarkt_password
    db.session.add(creds)


def _save_github_token(device_id: str) -> None:
    """Stage the posted GitHub token, unless its field was left blank (which keeps the stored one)."""
    token = request.form.get("github_token", "").strip()
    if token:
        stored = db.session.get(GitHubToken, device_id) or GitHubToken(device_id=device_id)
        stored.token = token
        db.session.add(stored)


@bp.route("/device/name", methods=["GET", "POST"], defaults={"device_id": None})
@bp.route("/device/<device_id>/name", methods=["GET", "POST"])
@require_paired_user
def name_screen() -> ResponseReturnValue:
    """Name a new screen, which asks for its renderer to be started (see services/assignments.py).

    The user is sent here right after pairing, and never again once it's done.
    """
    device = db.session.get(Device, g.device_id)
    if not assignments.needs_renderer(device):
        return redirect(url_for("pages.device_config", device_id=g.device_id))

    error = None
    slug = request.form.get("slug", "").strip().lower()
    if request.method == "POST":
        try:
            assignments.create(device, slug, current_username())
            return redirect(url_for("pages.device_config", device_id=g.device_id))
        except assignments.InvalidSlugError:
            error = (
                f"Use up to {assignments.max_suffix_length(current_username())} lowercase letters, numbers, "
                "dashes or underscores, starting with a letter or number."
            )
        except assignments.SlugTakenError:
            error = "That name is taken. Try another."
        except assignments.NotNeededError:
            return redirect(url_for("pages.device_config", device_id=g.device_id))
    return render_template(
        "name_screen.html",
        device_id=g.device_id,
        slug=slug,
        prefix=assignments.slug_prefix(current_username()),
        max_length=assignments.max_suffix_length(current_username()),
        error=error,
    ), 400 if error else 200


@bp.get("/device/screen", defaults={"device_id": None})
@bp.get("/device/<device_id>/screen")
@require_paired_user
def screen_view() -> ResponseReturnValue:
    """Show what a screen shows, as its latest frame, and keep it current as the screen does."""
    config = db.session.get(DeviceConfig, g.device_id)
    frame = db.session.get(Frame, g.device_id)
    upright = config.orientation != "horizontal"
    width, height = (frame_image.HEIGHT, frame_image.WIDTH) if upright else (frame_image.WIDTH, frame_image.HEIGHT)
    return render_template(
        "screen.html",
        device_id=g.device_id,
        has_frame=frame is not None,
        rendered_at=frame.rendered_at if frame else None,
        interval=pace.effective_interval(frame),
        width=width,
        height=height,
    )


@bp.get("/device/screen/frame.png", defaults={"device_id": None})
@bp.get("/device/<device_id>/screen/frame.png")
@require_paired_user
def screen_frame() -> ResponseReturnValue:
    """Serve a screen's latest frame as a PNG, the right way up for how the screen is hung.

    Conditional on the frame's ETag (and the orientation, which changes the picture), so a page polling it only
    downloads a frame that changed. rendered_at moves on every upload, frame changed or not, so it rides along in a
    header for the page to say how fresh the picture is.
    """
    frame = db.session.get(Frame, g.device_id)
    if frame is None:
        return jsonify(error="no frame yet"), 404
    upright = db.session.get(DeviceConfig, g.device_id).orientation != "horizontal"
    resp = Response(mimetype="image/png")
    resp.set_etag(f"{frame.etag}-{'v' if upright else 'h'}")
    resp.headers["Cache-Control"] = "private, no-cache"
    resp.headers["X-Rendered-At"] = frame.rendered_at.isoformat(timespec="seconds")
    resp = resp.make_conditional(request)
    if resp.status_code == HTTPStatus.OK:  # a 304 needs no picture
        resp.set_data(frame_image.to_png(frame.frame, upright=upright))
    return resp


@bp.post("/api/displays/<device_id>/renderer")
@require_user
def request_renderer(device_id: str) -> ResponseReturnValue:
    """Name the signed-in user's screen, which asks for its renderer (the same as the form above).

    {"slug": "lisa"} gives 201 {id, slug, status}; 400 for a bad slug, 404 for a device that isn't the
    user's, 409 for a slug that's taken or a screen that already has its renderer.
    """
    device = db.session.get(Device, device_id)
    if device is None or device.owner != current_user() or device.paired_at is None:
        return jsonify(error="not found"), 404
    slug = (request.get_json(silent=True) or {}).get("slug", "")
    try:
        assignment = assignments.create(device, slug if isinstance(slug, str) else "", current_username())
    except assignments.InvalidSlugError:
        return jsonify(error="slug must match [a-z0-9][a-z0-9_-]* and fit in 32 characters with the prefix"), 400
    except (assignments.SlugTakenError, assignments.NotNeededError) as e:
        return jsonify(
            error="slug is taken" if isinstance(e, assignments.SlugTakenError) else "already has a renderer"
        ), 409
    return jsonify(id=assignment.id, slug=assignment.slug, status=assignment.status), 201


# The page always sets this orientation and doesn't let it be changed, whatever is stored.
PAGE_ORIENTATION = "vertical"


def _render_device_config(device_id: str, errors: list[str] | None = None) -> str:
    """Render a screen's settings page, with any errors from saving it."""
    config = db.session.get(DeviceConfig, device_id)
    creds = db.session.get(GlowmarktCredentials, device_id)
    glowmarkt = {
        "username": creds.username if creds else "",
        "has_password": bool(creds and creds.password_encrypted),
    }

    device = db.session.get(Device, device_id)
    # An empty weather location is prefilled with a guess from the visitor's IP, shown as one; it's
    # only stored if they save it.
    weather_suggestion = None if config.weather_location else geoip.suggest_weather_location(request.remote_addr)
    return render_template(
        "device.html",
        device_id=device_id,
        display_attached=device.display_secret_hash is not None,
        assignment=device.assignment,
        config=config,
        weather_suggestion=weather_suggestion,
        clock_formats=CLOCK_FORMATS,
        languages=LANGUAGES,
        orientations=ORIENTATIONS,
        styles=STYLES,
        page_orientation=PAGE_ORIENTATION,
        stops=stops.resolve(config.tfl_stop_ids),
        MODE_ICONS=stops.MODE_ICONS,
        spotify_linked=db.session.get(SpotifyToken, device_id) is not None,
        apple_music_available=apple_music.configured(),
        apple_music_linked=apple_music.linked(device_id),
        glowmarkt=glowmarkt,
        github_linked=db.session.get(GitHubToken, device_id) is not None,
        errors=errors or [],
    )


@bp.post("/device/spotify/disconnect", defaults={"device_id": None})
@bp.post("/device/<device_id>/spotify/disconnect")
@require_paired_user
def spotify_disconnect() -> ResponseReturnValue:
    """Forget the screen's Spotify tokens, unlinking its Spotify account."""
    token = db.session.get(SpotifyToken, g.device_id)
    if token is not None:
        db.session.delete(token)
        db.session.commit()
    return redirect(url_for("pages.device_config", device_id=g.device_id))


@bp.post("/device/github/disconnect", defaults={"device_id": None})
@bp.post("/device/<device_id>/github/disconnect")
@require_paired_user
def github_disconnect() -> ResponseReturnValue:
    """Forget the screen's GitHub token."""
    token = db.session.get(GitHubToken, g.device_id)
    if token is not None:
        db.session.delete(token)
        db.session.commit()
    return redirect(url_for("pages.device_config", device_id=g.device_id))


@bp.post("/device/display/unlink", defaults={"device_id": None})
@bp.post("/device/<device_id>/display/unlink")
@require_paired_user
def display_unlink() -> ResponseReturnValue:
    """Forget the screen's secret, so it gets a 401 from then on.

    For a screen that's lost, stolen or retired. Registering again makes it a new device.
    """
    db.session.get(Device, g.device_id).display_secret_hash = None
    db.session.commit()
    return redirect(url_for("pages.device_config", device_id=g.device_id))
