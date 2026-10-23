"""A screen's config as the render pool reads it: the countdown API contract's ConfigResponse, made from its settings.

The store keeps it per screen (services/config_store.py, `v1:config:<device id>`, without a pairing code), and with
the screen's live code while it has one (services/pairing_key.py, `v1:pairing:<device id>`). Both are made here, the
same way. The contract (vendor/countdown-api) is the shape pi-dashboard's renderer reads them with.
"""

from typing import Literal

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
from broker.models import Device, DeviceConfig, GlowmarktCredentials, Style
from broker.services import pace, pairing, stops, weather_place
from broker.services.postcodes import postcode_country


def build(config: DeviceConfig, *, pairing_code: str | None) -> ConfigResponse:
    """Return the config a renderer is sent for the device `config` is the settings of.

    It is the device's own settings, and no key of any kind: the renderer reads everything from the store, whose
    providers have their own keys (TfL's), or need none (Open-Meteo's weather). The broker's TfL key is only for its
    stop lookups. Carries the device_id, so a renderer needn't keep it, and `pairing_code`, the code it is to show
    (None for none).
    """
    device_id = config.device_id
    creds = db.session.get(GlowmarktCredentials, device_id)
    country = postcode_country(config.postcode)
    stop_ids = config.tfl_stop_ids
    return ConfigResponse(
        device_id=device_id,
        # How often the render pool draws (faster while a provider asks). The screen's polls aren't paced by it: the
        # broker's answers are (services/pace.py).
        interval=pace.RENDER_INTERVAL_S,
        clock_format=config.clock_format,
        language=config.language,
        timezone=timezone(country, stop_ids),
        # The layout is None for the screen's default; a renderer draws a layout the screen's size doesn't have as its
        # default too.
        screen=Screen(
            orientation=config.orientation,
            width=config.width,
            height=config.height,
            format=config.format,
            layout=config.layout,
        ),
        # The key is what the weather is subscribed under, so it's what the renderer reads (v1:weather:<key>). The
        # design is only drawn on an HD screen (models.is_hd_screen); other screens ignore it.
        weather=Weather(
            location=config.weather_location, key=weather_place.weather_key(config), design=config.weather_design
        ),
        style=config.style,
        # The panels' own styles, for renderers that predate `style`, in the names they know.
        notice_board=NoticeBoard(postcode=config.postcode or None, country=country, style=config.style),
        # For renderers that predate `departures`: only the stops they can show.
        tfl=Tfl(
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
        # Only the switch, like the developer panel's: the Apple token stays here, and the music provider fills the
        # store the screen reads.
        apple_music=AppleMusic(enabled=config.apple_music_enabled),
        pairing_code=pairing_code,
    )


def for_store(device: Device) -> ConfigResponse | None:
    """Return the copy of the device's config the store keeps for the render pool, or None if it keeps none.

    It is build()'s config with no pairing code (null): the code goes in its own key (services/pairing_key.py), so
    it's never in a copy that outlives it.

    None while the device isn't paired, or has a live code to show: the pool draws the pairing screen from the code's
    key then, or nothing.
    """
    if device.config is None or device.paired_at is None or pairing.live_code(device.device_id):
        return None
    return build(device.config, pairing_code=None)


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


def _departures_style(style: Style) -> Literal["grouped", "hero", "card"]:
    """Return `style` as the departures' own field names it: airy was called grouped there."""
    return "grouped" if style == "airy" else style
