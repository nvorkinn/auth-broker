"""What a renderer is sent as its config: the countdown API contract's ConfigResponse, made from a device's settings.

GET /api/config answers with it (routes/devices.py), and the store keeps a copy of it per screen for the renderers to
read instead (services/config_store.py). Both are made here, the same way, so a renderer reading either draws the same
screen; the store's copy only leaves out what mustn't go to Redis (for_store).
"""

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
from flask import current_app

from broker.db import db
from broker.models import BASE_INTERVAL, Device, DeviceConfig, GlowmarktCredentials
from broker.services import pairing, stops, weather_place
from broker.services.postcodes import postcode_country


def build(config: DeviceConfig, *, pairing_code: str | None, api_keys: bool) -> ConfigResponse:
    """Return the config a renderer is sent for the device `config` is the settings of.

    Bundles the device's own settings with the shared app-level API keys if `api_keys` (else they're sent empty), so
    a key rotation doesn't require re-flashing every gifted device. Carries the device_id, so a
    renderer needn't keep it, and `pairing_code`, the code it is to show (None for none).
    """
    device_id = config.device_id
    creds = db.session.get(GlowmarktCredentials, device_id)
    country = postcode_country(config.postcode)
    stop_ids = config.tfl_stop_ids
    return ConfigResponse(
        device_id=device_id,
        # Renderers that predate the pace still read it; they sleep this, and a faster refresh comes from the frames.
        interval=BASE_INTERVAL,
        clock_format=config.clock_format,
        language=config.language,
        timezone=timezone(country, stop_ids),
        screen=Screen(orientation=config.orientation, width=config.width, height=config.height),
        # The key is what the weather is subscribed under, so it's what the renderer reads (v1:weather:<key>).
        weather=Weather(
            api_key=current_app.config["WEATHER_API_KEY"] if api_keys else "",
            location=config.weather_location,
            key=weather_place.weather_key(config),
        ),
        style=config.style,
        # The panels' own styles, for renderers that predate `style`, in the names they know.
        notice_board=NoticeBoard(postcode=config.postcode or None, country=country, style=config.style),
        # For renderers that predate `departures`: only the stops they can show.
        tfl=Tfl(
            app_key=current_app.config["TFL_APP_KEY"] if api_keys else "",
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
        pairing_code=pairing_code,
    )


def for_store(device: Device) -> ConfigResponse | None:
    """Return the copy of the device's config the store keeps for its renderer, or None if it keeps none.

    It is the config GET /api/config answers with, less what mustn't be in Redis, which every provider and renderer
    can read: the app's API keys (sent empty; the renderers no longer use them) and the pairing code (null). A code
    proves someone is at the screen, and hands the screen to whoever types it.

    None while the renderer has a code to show (the device isn't paired, or a code was asked for and is still live):
    only GET /api/config gives it one, and issues it, so with no copy in the store the renderer asks the broker.
    """
    if device.config is None or device.paired_at is None or pairing.live_code(device.device_id):
        return None
    return build(device.config, pairing_code=None, api_keys=False)


def timezone(country: str | None, stop_ids: list[str]) -> str:
    """Return the screen's time zone: its postcode's country's, or, without a postcode, its first stop's."""
    if country is None and stop_ids:
        country = "NO" if stops.source_of(stop_ids[0]) == "entur" else "GB"
    return "Europe/Oslo" if country == "NO" else "Europe/London"


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
