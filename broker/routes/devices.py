"""A device's own lifecycle: registering on first boot, pairing codes, and the config poll it checks in with.

Everything but /register needs one of the device's secrets.
"""

from typing import TYPE_CHECKING

from countdown_api.blueprints import ApiHTTPError
from countdown_api.models import (
    AppleMusic,
    ConfigResponse,
    Departures,
    DepartureStop,
    Developer,
    Glowmarkt,
    NoticeBoard,
    Screen,
    Spotify,
    Tfl,
    Weather,
)
from cryptography.fernet import InvalidToken
from flask import Blueprint, current_app, jsonify, request
from sqlalchemy.exc import IntegrityError

from broker.api import api
from broker.auth import hash_secret, require_device_auth, with_retry_after
from broker.db import db
from broker.models import BASE_INTERVAL, Device, DeviceConfig, GlowmarktCredentials
from broker.services import pairing, registration, stops, weather_place

from .pages import postcode_country

if TYPE_CHECKING:
    from flask.typing import ResponseReturnValue

bp = Blueprint("devices", __name__, url_prefix="/api/devices")

# The shortest secret a screen or renderer may register with.
MIN_SECRET_LENGTH = 16


@bp.post("/register")
def register() -> ResponseReturnValue:
    """Give a screen its identity.

    The screen generates its own secret, keeps it locally forever after, and sends it with every
    request; only its hash is stored here, and that hash alone identifies the device.
    Safe to call again with the same secret, e.g. on every boot.

    {"secret": ..., "width": ..., "height": ...}, the screen's size in pixels, becomes a device at once
    (201 {device_id}), which its owner pairs with and names a renderer for; the orchestrator then starts
    that renderer, which claims the device itself (see routes/renderers.py). A repeat is a 200.

    Throttled per IP, every call counting, as anyone can call it.
    """
    throttle = current_app.extensions["register_throttle"]
    ip = request.remote_addr or "unknown"
    retry_after = throttle.retry_after(ip)
    if retry_after is not None:
        return with_retry_after(jsonify(error="too many registrations"), retry_after), 429
    # The call that reaches the limit still goes through; it's the next one that's refused.
    lockout = throttle.record_failure(ip)
    if lockout is not None:
        current_app.logger.warning("Too many registrations from %s; locked out for %ss", ip, lockout)

    body = request.get_json(silent=True) or {}
    secret = body.get("secret", "")
    width, height = body.get("width"), body.get("height")
    if not isinstance(secret, str) or len(secret) < MIN_SECRET_LENGTH:
        return jsonify(error="secret must be a random string of at least 16 characters"), 400
    if not all(type(size) is int and size > 0 for size in (width, height)):
        return jsonify(error="width and height must be positive integers"), 400

    secret_hash = hash_secret(secret)
    try:
        found = registration.register_screen(secret_hash, width, height)
    except IntegrityError:
        # The same secret registered by a simultaneous request (e.g. a quick retry) that got in
        # first: answer as for any repeat.
        db.session.rollback()
        found = registration.register_screen(secret_hash, width, height)

    if found is None:
        return with_retry_after(jsonify(error="too many devices waiting"), 60), 503
    if found.role_conflict:
        return jsonify(error="that secret is already registered as a renderer"), 409
    return jsonify(device_id=found.device_id), 201 if found.created else 200


@bp.post("/pairing-code")
@require_device_auth()
def create_pairing_code(device_id: str) -> ResponseReturnValue:
    """Force a fresh code, e.g. to link a new browser to an already-paired device.

    A screen can ask too, since a device that has no renderer yet has nothing else to show one.
    """
    code = pairing.issue_code(device_id)
    return jsonify(code=code, expires_in_seconds=int(pairing.CODE_TTL.total_seconds()))


def _update_device_name(device_id: str, name: str | None) -> None:
    """Opportunistically keep devices.device_name in sync with the Pi's own DEVICE_ID.

    It is sent as X-Device-Name on every config poll -- a purely cosmetic
    label for logs/admin output, never the device's real identity, so a rename
    on the Pi just needs its next poll to take effect here, no re-pairing.
    """
    name = (name or "").strip()
    if name:
        db.session.get(Device, device_id).device_name = name
        db.session.commit()


@api.get_config()
def get_config(*, device_id: str, x_device_name: str | None = None) -> ConfigResponse:
    """Answer the renderer's config poll (GET /api/config in the contract, which also says that only its role may).

    Bundles the device's own settings together with the shared app-level API keys, so a key rotation doesn't require
    re-flashing every gifted device. Carries the device_id, so a renderer needn't keep it.
    """
    _update_device_name(device_id, x_device_name)
    config = db.session.get(DeviceConfig, device_id)
    if config is None:
        raise ApiHTTPError(404, message="not found")

    creds = db.session.get(GlowmarktCredentials, device_id)
    country = postcode_country(config.postcode)
    stop_ids = config.tfl_stop_ids
    return ConfigResponse(
        device_id=device_id,
        # Renderers that predate the pace still read it; they sleep this, and a faster refresh comes from the frames.
        interval=BASE_INTERVAL,
        clock_format=config.clock_format,
        language=config.language,
        timezone=_timezone(country, stop_ids),
        screen=Screen(orientation=config.orientation, width=config.width, height=config.height),
        # The key is what the weather is subscribed under, so it's what the renderer reads (v1:weather:<key>).
        weather=Weather(
            api_key=current_app.config["WEATHER_API_KEY"],
            location=config.weather_location,
            key=weather_place.weather_key(config),
        ),
        style=config.style,
        # The panels' own styles, for renderers that predate `style`, in the names they know.
        notice_board=NoticeBoard(postcode=config.postcode or None, country=country, style=config.style),
        # For renderers that predate `departures`: only the stops they can show.
        tfl=Tfl(
            app_key=current_app.config["TFL_APP_KEY"],
            stop_ids=[s for s in stop_ids if stops.source_of(s) == "tfl"],
            style=_departures_style(config.style),
        ),
        departures=Departures(
            stops=[DepartureStop(source=stops.source_of(s), id=s) for s in stop_ids],
            style=_departures_style(config.style),
        ),
        spotify=Spotify(enabled=config.spotify_enabled, style=config.style),
        # Only the switch: the Glowmarkt provider signs in and fills the store, which the screen reads.
        glowmarkt=Glowmarkt(enabled=_glowmarkt_enabled(creds)),
        # Only the switch: the GitHub token stays here, and the developer provider fills the store the screen reads.
        developer=Developer(enabled=config.developer_enabled),
        # Only the switch, like the developer panel's: the screen gets Apple Music from /api/apple-music.
        apple_music=AppleMusic(enabled=config.apple_music_enabled),
        pairing_code=pairing.current_code_for(device_id),
    )


def _glowmarkt_enabled(creds: GlowmarktCredentials | None) -> bool:
    """Return whether the device has a Glowmarkt account the provider signs in with (GlowmarktCredentials.sign_in)."""
    if creds is None:
        return False
    try:
        return creds.sign_in() is not None
    except InvalidToken:
        # The provider skips it too, so there would be nothing in the store to show.
        current_app.logger.warning("Can't decrypt the Glowmarkt password of device %s", creds.device_id)
        return False


def _departures_style(style: str) -> str:
    """Return `style` as the departures' own field names it: airy was called grouped there."""
    return "grouped" if style == "airy" else style


def _timezone(country: str | None, stop_ids: list[str]) -> str:
    """Return the screen's time zone: its postcode's country's, or, without a postcode, its first stop's."""
    if country is None and stop_ids:
        country = "NO" if stops.source_of(stop_ids[0]) == "entur" else "GB"
    return "Europe/Oslo" if country == "NO" else "Europe/London"
