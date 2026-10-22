"""The owners' site: pairing the signed-in user with a device, and the device's settings page."""

import re
from http import HTTPStatus
from typing import TYPE_CHECKING, Any

from countdown_api import keys as store_keys
from cryptography.fernet import InvalidToken
from flask import Blueprint, Response, current_app, g, jsonify, redirect, render_template, request, url_for
from redis import Redis, RedisError

from broker.auth import (
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
    GitHubToken,
    GlowmarktCredentials,
    SpotifyToken,
)
from broker.services import (
    assignments,
    config_store,
    device_keys,
    frame_image,
    frame_record,
    geoip,
    pace,
    pairing,
    render_jobs,
    stops,
    weather_place,
)
from broker.services.postcodes import normalize_postcode

if TYPE_CHECKING:
    from flask.typing import ResponseReturnValue

bp = Blueprint("pages", __name__)

PAIRING_TOKEN_RE = re.compile(r"^[0-9a-f]{64}$")


def _write_subscriptions_to_redis() -> None:
    configs = db.session.scalars(db.select(DeviceConfig)).all()
    subs: dict[str, set[str]] = {
        "tfl": set(),
        "entur": set(),
        "weather": set(),
        "glow": set(),
        "spotify": set(),
        "developer": set(),
        # The notice board's place, for the notice providers: each postcode in canonical form ("SW1A 1AA", "0150").
        "postcode": set(),
    }

    for config in configs:
        for stop_id in config.tfl_stop_ids:
            subs["entur" if stop_id.startswith("NSR:") else "tfl"].add(stop_id)
        # By the place picked, not the text typed, so screens that picked the same place share its forecast.
        if weather_key := weather_place.weather_key(config):
            subs["weather"].add(weather_key)
        if config.spotify_enabled:
            subs["spotify"].add(config.device_id)
        if config.developer_enabled:
            subs["developer"].add(config.device_id)
        if config.postcode:
            subs["postcode"].add(config.postcode)
    subs["glow"] = set(
        db.session.scalars(
            db.select(GlowmarktCredentials.device_id).where(GlowmarktCredentials.password_encrypted.is_not(None))
        )
    )
    redis: Redis = current_app.extensions["redis"]
    try:
        with redis.pipeline(transaction=True) as pipe:
            for name, members in subs.items():
                key = store_keys.subscription(name)
                pipe.delete(key)
                if members:
                    pipe.sadd(key, *members)
            pipe.publish(store_keys.SUB_CHANGED, "")
            pipe.execute()
    except RedisError as e:
        # The error's type only: a failed pipeline's text quotes its commands, and SADD v1:sub:postcode's has postcodes.
        current_app.logger.warning("Couldn't publish subscriptions to Redis (%s)", type(e).__name__)


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
def pair(user_id: str) -> ResponseReturnValue:
    """Pair the signed-in user with a device.

    A device that has just finished Wi-Fi setup sends its owner's browser here with a pairing token (?t=, or the URL
    fragment): the page waits for the device to register, then goes to its settings. Without a token it asks for the
    code the device shows. A code in ?code= is filled in and sent at once, through the same POST as a typed one, so it's
    throttled the same.
    """
    if request.method == "GET":
        code = request.args.get("code", "")
        code = code.strip().upper() if pairing.looks_like_code(code) else ""
        return _render_pair(error=None, token=request.args.get("t", ""), code=code)

    # Codes are guessable given enough tries, so wrong ones are throttled per IP; a locked-out IP is
    # refused even with the right code, or the lockout wouldn't slow guessing down.
    throttle = current_app.extensions["pair_throttle"]
    ip = request.remote_addr or "unknown"
    retry_after = throttle.retry_after(ip)
    if retry_after is not None:
        return _too_many_attempts(retry_after)

    device_id = pairing.redeem_code(request.form.get("code", "").strip().upper(), user_id)
    if device_id is None:
        lockout = throttle.record_failure(ip)
        current_app.logger.warning("Wrong pairing code from %s", ip)
        if lockout is not None:
            current_app.logger.warning("Too many wrong pairing codes from %s; locked out for %ss", ip, lockout)
            return _too_many_attempts(lockout)
        return _render_pair(error="That code is invalid or has expired.")

    # Paired, and the code spent: its config goes back in the store, for the render pool to draw, and what it shows
    # (the weather location registration gave it) is subscribed to.
    config_store.publish(device_id)
    _write_subscriptions_to_redis()
    return redirect(url_for("pages.device_config"))


def _too_many_attempts(retry_after: int) -> ResponseReturnValue:
    minutes = -(-retry_after // 60)
    wait = "a minute" if minutes == 1 else f"{minutes} minutes"
    error = f"Too many incorrect codes. Please wait {wait} and try again."
    return _render_pair(error=error), 429, {"Retry-After": str(retry_after)}


def _render_pair(error: str | None, token: str = "", code: str = "") -> str:
    return render_template("pair.html", error=error, token=token, code=code)


@bp.post("/pair/token")
@require_user
def pair_by_token(user_id: str) -> ResponseReturnValue:
    """Pair the signed-in user with the device a pairing token is for.

    The pairing page polls this: 200 once the user is paired, 202 while the device hasn't registered yet,
    409 if someone else owns it. The token is 256 random-looking bits, so there's nothing to throttle.
    """
    token = (request.get_json(silent=True) or {}).get("token", "")
    if not isinstance(token, str) or not PAIRING_TOKEN_RE.match(token):
        return jsonify(status="invalid"), 400
    status = pairing.pair_by_token(user_id, token)
    if status == "paired":
        # A paired screen's config goes in the store (all of them: this one is known here only by its token), and
        # what it shows is subscribed to.
        config_store.publish_all()
        _write_subscriptions_to_redis()
    return jsonify(status=status), {"paired": 200, "waiting": 202, "taken": 409}[status]


# Every page about a screen is at /device/<id>/..., and at /device/... for the user's most recently paired one.
@bp.route("/device", methods=["GET", "POST"], defaults={"device_id": None})
@bp.route("/device/<device_id>", methods=["GET", "POST"])
@require_paired_user
def device_config() -> ResponseReturnValue:
    """Show a screen's settings page, and save the settings posted from it."""
    device_id = g.device_id
    # A paired screen without a slug gets one now, which lists it in the pool.
    if assignments.auto_name(db.session.get(Device, device_id)) is not None:
        db.session.commit()
        config_store.publish(device_id)

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
    try:
        screen_name = assignments.clean_name(request.form["screen_name"]) if "screen_name" in request.form else None
    except assignments.NameTooLongError:
        errors.append(f"The screen's name can be at most {assignments.NAME_MAX} characters.")
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
    if "screen_name" in request.form:
        db.session.get(Device, device_id).device_name = screen_name
    glowmarkt_changed = _save_glowmarkt_credentials(device_id)
    github_changed = _save_github_token(device_id)

    db.session.commit()
    # What the store has from an account that's gone or been replaced, before the providers are told to write the new
    # one's, so the screen never shows the old account's data.
    stale = device_keys.energy(device_id) if glowmarkt_changed else []
    stale += device_keys.developer(device_id) if github_changed else []
    if stale:
        device_keys.delete(stale)
    _write_subscriptions_to_redis()
    # Every screen's, not just this one's: it costs little, and puts back any copy the store lost.
    config_store.publish_all()
    # A render of this screen, if the render pool draws it, even if its copy didn't change (an account replaced).
    render_jobs.enqueue(current_app.extensions["redis"], [device_id])
    return redirect(url_for("pages.device_config", device_id=device_id))


def _weather_place(config: DeviceConfig) -> tuple[dict[str, Any] | None, list[str]]:
    """Return the weather place posted (None for none) and what's wrong with it, as a list of at most one error.

    The picker posts the id of the place picked (weather_place_id) beside the text in its box (weather_location);
    weather_place.resolve decides which counts.
    """
    text = request.form.get("weather_location", "").strip()
    # Only a place Open-Meteo knows is ever stored; if it can't be checked right now, that's refused too rather than
    # saved unverified.
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


def _save_glowmarkt_credentials(device_id: str) -> bool:
    """Stage the posted Glowmarkt username, and password unless its field was left blank.

    Return whether that changes the account the glow-provider signs in with, or leaves it none.
    """
    glowmarkt_password = request.form.get("glowmarkt_password", "")
    stored = db.session.get(GlowmarktCredentials, device_id)
    before = _glowmarkt_account(stored) if stored is not None else None
    creds = stored or GlowmarktCredentials(device_id=device_id)
    creds.username = request.form.get("glowmarkt_username", "").strip()
    if glowmarkt_password:
        creds.password = glowmarkt_password
    db.session.add(creds)
    return _glowmarkt_account(creds) != before


def _glowmarkt_account(creds: GlowmarktCredentials) -> tuple[str, str] | str | None:
    """Return the account the provider signs in with (GlowmarktCredentials.sign_in); None for none.

    "undecryptable" for a password that won't decrypt, which the provider can't sign in with either.
    """
    try:
        return creds.sign_in()
    except InvalidToken:
        return "undecryptable"


def _save_github_token(device_id: str) -> bool:
    """Stage the posted GitHub token, unless its field was left blank (which keeps the stored one).

    Return whether that replaces the token the developer provider uses.
    """
    token = request.form.get("github_token", "").strip()
    if not token:
        return False
    stored = db.session.get(GitHubToken, device_id)
    try:
        unchanged = stored is not None and stored.token == token
    except InvalidToken:
        unchanged = False
    if unchanged:
        return False
    stored = stored or GitHubToken(device_id=device_id)
    stored.token = token
    db.session.add(stored)
    return True


@bp.route("/device/name", methods=["GET", "POST"], defaults={"device_id": None})
@bp.route("/device/<device_id>/name", methods=["GET", "POST"])
@require_paired_user
def name_screen() -> ResponseReturnValue:
    """Redirect to the screen's settings page, where its name is set."""
    return redirect(url_for("pages.device_config", device_id=g.device_id))


@bp.get("/device/screen", defaults={"device_id": None})
@bp.get("/device/<device_id>/screen")
@require_paired_user
def screen_view() -> ResponseReturnValue:
    """Show the screen's latest frame, and say how often the page checks for a new one (the screen's own poll rate)."""
    config = db.session.get(DeviceConfig, g.device_id)
    shown = frame_record.head(g.device_id)
    upright = config.orientation != "horizontal"
    width, height = (frame_image.HEIGHT, frame_image.WIDTH) if upright else (frame_image.WIDTH, frame_image.HEIGHT)
    now = pace.clock()
    return render_template(
        "screen.html",
        device_id=g.device_id,
        has_frame=shown is not None,
        rendered_at=shown.rendered_at if shown else None,
        # As often as the screen polls, right now.
        interval=pace.poll_interval_s(now, pace.screen_hints(g.device_id, now)),
        width=width,
        height=height,
    )


@bp.get("/device/screen/frame.png", defaults={"device_id": None})
@bp.get("/device/<device_id>/screen/frame.png")
@require_paired_user
def screen_frame() -> ResponseReturnValue:
    """Serve the frame the screen is served as a PNG, the right way up for how the screen is hung.

    Conditional on the frame's ETag and the orientation, so a page polling it only downloads a frame that changed.
    X-Rendered-At is when the pool first drew the picture.
    """
    frame = frame_record.read(g.device_id)
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
        screen_name=device.device_name or "",
        name_max=assignments.NAME_MAX,
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
        # What it was playing, and its top lists: the music provider writes them again within a minute for whatever is
        # still linked.
        device_keys.delete(device_keys.music(g.device_id))
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
        # The developer provider rewrites the panel without the GitHub summary within a cycle.
        device_keys.delete(device_keys.developer(g.device_id))
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
