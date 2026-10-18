"""A screen's weather place: picking it from Open-Meteo, storing it, and the readable key it is known by.

A place is stored as Open-Meteo's id, with its name, label, country and coordinates as Open-Meteo gave them when
it was picked (never as the browser sent them). weather_location keeps the place's name, which renderers are sent
as the Weather config's `location`; its `key` is weather_key(), which the weather is subscribed and stored under.
"""

import re
import unicodedata
from typing import TYPE_CHECKING, Any

from broker.clients import weather

if TYPE_CHECKING:
    from broker.models import DeviceConfig


class PlaceNotFoundError(Exception):
    """Open-Meteo has no place by that id, or nothing matching that name."""


def slugify(text: str) -> str:
    """Return text as lowercase ASCII words joined by single hyphens: "Tromsø" is "tromso", "St. Albans" "st-albans".

    Accents are stripped (NFKD, then the combining marks dropped); a letter with no ASCII form (ø, ß, æ) is spelt
    the way it usually is in ASCII.
    """
    text = text.casefold().translate(_NON_DECOMPOSING)
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", "-", ascii_text).strip("-")


# Letters NFKD leaves whole, so the ASCII encode would drop them: "Tromsø" would be "troms".
_NON_DECOMPOSING = str.maketrans({"ø": "o", "æ": "ae", "ß": "ss", "đ": "d", "ł": "l", "þ": "th", "ð": "d", "œ": "oe"})


# The country part of the key of a place Open-Meteo gave no country code: one ISO 3166-1 leaves for users to assign,
# so no country has it.
NO_COUNTRY = "xx"


def weather_key(config: DeviceConfig) -> str | None:
    """Return the readable key a screen's weather is known by, e.g. "london-gb-2643743"; None for no location.

    It is <slug of the name>-<country code>-<Open-Meteo id>: the id makes it unique, the rest makes it readable.
    The country is always there (NO_COUNTRY if Open-Meteo gave none), as a number after a two-letter country is how
    the weather provider tells a place's key from the slug of text that ends in a number ("oslo-0150").

    A config saved before places were picked has only its free text, which gives the slug of that text instead
    ("london", or the text itself, case and spaces folded, if it has nothing to slug), so it still has a key and its
    screen still has weather (the provider geocodes it by name) until `flask weather backfill` gives it a place.

    It is the key the weather's subscriptions and data are kept under in Redis (members of v1:sub:weather, and
    v1:weather:<key>), and renderers are sent it as the Weather config's `key`.
    """
    if config.weather_place_id is not None:
        slug = slugify(config.weather_place_name or config.weather_location or "")
        country = (config.weather_country_code or "").lower() or NO_COUNTRY
        return "-".join(part for part in (slug, country, str(config.weather_place_id)) if part)
    normalized = " ".join((config.weather_location or "").casefold().split())
    return slugify(normalized) or normalized or None


def resolve(place_id: str, text: str, current: DeviceConfig) -> dict[str, Any] | None:
    """Return the place a settings form picked: by its id if it has one, else Open-Meteo's best match for the text.

    The form sends the id of the place picked in the search, and the text in the box. Without JavaScript the id is
    the saved place's and the text whatever was typed, so an id is only trusted while the text is still that place's
    label or name; otherwise the text is searched. Blank text means no place (None). The place already saved is
    returned as saved, without asking Open-Meteo again.

    Raises PlaceNotFoundError for an unknown id or text that matches nothing, and
    weather.GeocodingUnavailableError if Open-Meteo couldn't be asked.
    """
    text = text.strip()
    if not text:
        return None
    place_id = place_id.strip()
    if place_id.isascii() and place_id.isdecimal():
        if int(place_id) == current.weather_place_id and _names(
            text, current.weather_place_label, current.weather_place_name
        ):
            return stored_place(current)
        place = weather.get_place(int(place_id))
        if place is None:
            raise PlaceNotFoundError(text)
        if _names(text, place["label"], place["name"]):
            return place
    results = weather.search_places(text, count=1)
    if not results:
        raise PlaceNotFoundError(text)
    return results[0]


def _names(text: str, *names: str | None) -> bool:
    return text.casefold() in {name.casefold() for name in names if name}


def stored_place(config: DeviceConfig) -> dict[str, Any]:
    """Return the place saved in a config, in the shape Open-Meteo's are."""
    return {
        "id": config.weather_place_id,
        "name": config.weather_place_name,
        "label": config.weather_place_label,
        "country_code": config.weather_country_code,
        "latitude": config.weather_lat,
        "longitude": config.weather_lon,
    }


def store(config: DeviceConfig, place: dict[str, Any] | None, *, keep_location: bool = False) -> None:
    """Set a config's weather place, or clear it for None.

    weather_location becomes the place's name, unless keep_location (for the backfill, which leaves what
    renderers are sent alone).
    """
    config.weather_place_id = place["id"] if place else None
    config.weather_place_name = place["name"] if place else None
    config.weather_place_label = place["label"] if place else None
    config.weather_country_code = place["country_code"] if place else None
    config.weather_lat = place["latitude"] if place else None
    config.weather_lon = place["longitude"] if place else None
    if not keep_location:
        config.weather_location = place["name"] if place else ""
