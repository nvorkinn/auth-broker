from unittest.mock import patch

import pytest
from countdown_api import fixtures

from broker.clients.weather import GeocodingUnavailableError
from broker.models import DeviceConfig
from broker.services import weather_place
from broker.services.weather_place import PlaceNotFoundError, resolve, slugify, weather_key

LONDON = {
    "id": 2643743,
    "name": "London",
    "admin1": "England",
    "country": "United Kingdom",
    "country_code": "GB",
    "latitude": 51.50853,
    "longitude": -0.12574,
    "label": "London, England, United Kingdom",
}
LONDON_ON = LONDON | {"id": 6058560, "admin1": "Ontario", "country": "Canada", "country_code": "CA"}
LONDON_ON["label"] = "London, Ontario, Canada"


@pytest.mark.parametrize(
    ("text", "slug"),
    [
        ("London", "london"),
        ("Tromsø", "tromso"),
        ("Bærum", "baerum"),
        ("São Paulo", "sao-paulo"),
        ("Zürich", "zurich"),
        ("Kraków", "krakow"),
        ("Gießen", "giessen"),
        ("St. Albans", "st-albans"),
        ("Newcastle upon Tyne", "newcastle-upon-tyne"),
        ("  --Stratford-upon-Avon!! ", "stratford-upon-avon"),
        ("Ålesund", "alesund"),
        ("東京", ""),
    ],
)
def test_slugify(text, slug):
    assert slugify(text) == slug


def _config(**columns):
    return DeviceConfig(device_id="d", **columns)


def test_weather_key_is_slug_country_and_id():
    config = _config(weather_location="London", weather_place_id=2643743, weather_place_name="London")
    config.weather_country_code = "GB"
    assert weather_key(config) == "london-gb-2643743"


def test_weather_key_strips_accents_from_the_place_name():
    config = _config(
        weather_location="Tromsø", weather_place_id=3133880, weather_place_name="Tromsø", weather_country_code="NO"
    )
    assert weather_key(config) == "tromso-no-3133880"


def test_weather_key_says_no_country_when_open_meteo_didnt_give_one():
    """A number after a two-letter country is how the weather provider knows a key has a place's id."""
    config = _config(weather_location="Atlantis", weather_place_id=42, weather_place_name="Atlantis")
    assert weather_key(config) == "atlantis-xx-42"


def test_weather_key_falls_back_to_the_free_text_before_a_place_is_picked():
    assert weather_key(_config(weather_location="  Newcastle   upon TYNE ")) == "newcastle-upon-tyne"
    assert weather_key(_config(weather_location="Tromsø")) == "tromso"


def test_weather_key_of_free_text_is_the_same_however_it_was_typed():
    keys = {weather_key(_config(weather_location=text)) for text in ("London", "london", " LONDON ", "London.")}
    assert keys == {"london"}


def test_weather_key_of_free_text_with_nothing_to_slug_is_the_text_so_the_screen_keeps_its_weather():
    assert weather_key(_config(weather_location="  東京  ")) == "東京"


# The keys pi-dashboard's weather provider is written for: its tests read the same file.
WEATHER_KEYS = fixtures.load("weather_keys.json")["keys"]


@pytest.mark.parametrize("example", WEATHER_KEYS, ids=[example["key"] for example in WEATHER_KEYS])
def test_weather_key_makes_the_keys_the_weather_provider_reads(example):
    assert weather_key(_config(**example["config"])) == example["key"]


@pytest.mark.parametrize("location", ["", "   ", None])
def test_weather_key_is_none_without_a_location(location):
    assert weather_key(_config(weather_location=location)) is None


def _saved(place):
    config = _config()
    weather_place.store(config, place)
    return config


def test_resolve_looks_a_picked_id_up_rather_than_trusting_the_form(app):
    with app.app_context(), patch("broker.clients.weather.get_place", return_value=LONDON_ON) as get:
        assert resolve("6058560", "London, Ontario, Canada", _config()) == LONDON_ON
    get.assert_called_once_with(6058560)


def test_resolve_raises_for_an_id_open_meteo_doesnt_have(app):
    with (
        app.app_context(),
        patch("broker.clients.weather.get_place", return_value=None),
        pytest.raises(PlaceNotFoundError),
    ):
        resolve("999999999", "Nowhere", _config())


def test_resolve_searches_the_text_when_it_isnt_the_picked_places_any_more(app):
    # Without JavaScript the id stays the saved place's while the text is edited.
    with (
        app.app_context(),
        patch("broker.clients.weather.get_place", return_value=LONDON),
        patch("broker.clients.weather.search_places", return_value=[LONDON_ON]) as search,
    ):
        assert resolve("2643743", "London Ontario", _config()) == LONDON_ON
    search.assert_called_once_with("London Ontario", count=1)


def test_resolve_keeps_the_saved_place_without_asking_open_meteo(app):
    with (
        app.app_context(),
        patch("broker.clients.weather.get_place") as get,
        patch("broker.clients.weather.search_places") as search,
    ):
        place = resolve("2643743", "london, england, united kingdom", _saved(LONDON))
    assert place is not None
    assert (place["id"], place["label"], place["country_code"]) == (2643743, LONDON["label"], "GB")
    get.assert_not_called()
    search.assert_not_called()


def test_resolve_takes_open_meteos_first_match_for_text_alone(app):
    with app.app_context(), patch("broker.clients.weather.search_places", return_value=[LONDON]) as search:
        assert resolve("", " London ", _config()) == LONDON
    search.assert_called_once_with("London", count=1)


def test_resolve_raises_for_text_that_matches_nothing(app):
    with (
        app.app_context(),
        patch("broker.clients.weather.search_places", return_value=[]),
        pytest.raises(PlaceNotFoundError),
    ):
        resolve("", "SE17 2PX", _config())


def test_resolve_passes_on_open_meteo_being_unreachable(app):
    with (
        app.app_context(),
        patch("broker.clients.weather.search_places", side_effect=GeocodingUnavailableError),
        pytest.raises(GeocodingUnavailableError),
    ):
        resolve("", "London", _config())


def test_resolve_is_none_for_blank_text_even_with_an_id(app):
    with app.app_context(), patch("broker.clients.weather.get_place") as get:
        assert resolve("2643743", "  ", _saved(LONDON)) is None
    get.assert_not_called()


def test_store_keeps_the_name_in_weather_location_and_clears_everything_for_none():
    config = _saved(LONDON)
    assert (config.weather_location, config.weather_place_id, config.weather_place_label) == (
        "London",
        2643743,
        "London, England, United Kingdom",
    )
    assert (config.weather_country_code, config.weather_lat, config.weather_lon) == ("GB", 51.50853, -0.12574)

    weather_place.store(config, None)
    assert config.weather_location == ""
    assert {
        config.weather_place_id,
        config.weather_place_name,
        config.weather_place_label,
        config.weather_country_code,
        config.weather_lat,
        config.weather_lon,
    } == {None}


@pytest.mark.parametrize("place_id", ["abc", "²", "-5", "1.5"])
def test_resolve_searches_the_text_when_the_id_isnt_a_number(app, place_id):
    with (
        app.app_context(),
        patch("broker.clients.weather.get_place") as get,
        patch("broker.clients.weather.search_places", return_value=[LONDON]),
    ):
        assert resolve(place_id, "London", _config()) == LONDON
    get.assert_not_called()
