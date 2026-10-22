from unittest.mock import Mock, patch

import pytest
import requests

from broker.clients.weather import GeocodingUnavailableError, get_place, search_places

LONDON = {
    "id": 2643743,
    "name": "London",
    "latitude": 51.50853,
    "longitude": -0.12574,
    "country_code": "GB",
    "admin1": "England",
    "country": "United Kingdom",
    "population": 8961989,
}
OSLO = {
    "id": 3143244,
    "name": "Oslo",
    "latitude": 59.91273,
    "longitude": 10.74609,
    "country_code": "NO",
    "admin1": "Oslo",
    "country": "Norway",
}


def _response(json_data, status_code=200):
    response = Mock()
    response.status_code = status_code
    response.json.return_value = json_data
    if status_code >= 400:
        response.raise_for_status = Mock(side_effect=requests.HTTPError(str(status_code)))
    else:
        response.raise_for_status = Mock()
    return response


def test_search_places_asks_open_meteo_for_english_names_and_returns_each_with_a_label(app):
    with (
        app.app_context(),
        patch("broker.clients.weather.requests.get", return_value=_response({"results": [LONDON]})) as get,
    ):
        places = search_places("London")

    get.assert_called_once_with(
        "https://geocoding-api.open-meteo.com/v1/search",
        params={"name": "London", "count": 8, "language": "en", "format": "json"},
        timeout=4,
    )
    assert places == [
        {
            "id": 2643743,
            "name": "London",
            "admin1": "England",
            "country": "United Kingdom",
            "country_code": "GB",
            "latitude": 51.50853,
            "longitude": -0.12574,
            "label": "London, England, United Kingdom",
        }
    ]


def test_search_places_leaves_a_region_named_like_the_place_out_of_its_label(app):
    with app.app_context(), patch("broker.clients.weather.requests.get", return_value=_response({"results": [OSLO]})):
        assert search_places("Oslo")[0]["label"] == "Oslo, Norway"


def test_search_places_copes_with_a_place_missing_its_region_and_country(app):
    bare = {"id": 1, "name": "Somewhere", "latitude": 1, "longitude": 2}
    with app.app_context(), patch("broker.clients.weather.requests.get", return_value=_response({"results": [bare]})):
        [place] = search_places("Somewhere")
    assert (place["label"], place["country_code"], place["admin1"]) == ("Somewhere", "", "")


def test_search_places_is_empty_when_open_meteo_has_no_results(app):
    not_found = {"generationtime_ms": 0.4}  # Open-Meteo leaves "results" out entirely when nothing matches
    with app.app_context(), patch("broker.clients.weather.requests.get", return_value=_response(not_found)):
        assert search_places("SE17 2PX") == []


@pytest.mark.parametrize(
    "failure",
    [
        {"side_effect": requests.ConnectionError},
        {"side_effect": requests.Timeout},
        {"return_value": _response({}, status_code=502)},
        {"return_value": _response({"results": [{"name": "no id"}]})},
    ],
)
def test_search_places_raises_when_open_meteo_cant_be_asked(app, failure):
    with (
        app.app_context(),
        patch("broker.clients.weather.requests.get", **failure),
        pytest.raises(GeocodingUnavailableError),
    ):
        search_places("London")


def test_get_place_looks_the_id_up(app):
    with app.app_context(), patch("broker.clients.weather.requests.get", return_value=_response(LONDON)) as get:
        place = get_place(2643743)
    assert place is not None

    get.assert_called_once_with(
        "https://geocoding-api.open-meteo.com/v1/get",
        params={"id": 2643743, "language": "en", "format": "json"},
        timeout=4,
    )
    assert (place["id"], place["label"]) == (2643743, "London, England, United Kingdom")


def test_get_place_is_none_for_an_id_open_meteo_doesnt_have(app):
    unknown = {"error": True, "reason": "Id not found"}
    with app.app_context(), patch("broker.clients.weather.requests.get", return_value=_response(unknown, 400)):
        assert get_place(999999999) is None


def test_get_place_raises_when_open_meteo_cant_be_asked(app):
    with (
        app.app_context(),
        patch("broker.clients.weather.requests.get", side_effect=requests.ConnectionError),
        pytest.raises(GeocodingUnavailableError),
    ):
        get_place(2643743)


# --- Nothing logged says where a screen is ----------------------------------------------------------------------------
# The logs go to the central log store; a failed request's text quotes its URL, with the place's name or id in it.

SEARCH_URL_FAILURE = requests.ConnectionError(
    "HTTPSConnectionPool(host='geocoding-api.open-meteo.com', port=443): Max retries exceeded with url: "
    "/v1/search?name=Little+Snoring&count=8&language=en&format=json"
)
PLACE_URL_FAILURE = requests.ConnectionError(
    "HTTPSConnectionPool(host='geocoding-api.open-meteo.com', port=443): Max retries exceeded with url: "
    "/v1/get?id=2643743&language=en&format=json"
)


def _logged(logger) -> str:
    """Return every line `logger` (a patched module logger) was asked to write, formatted, with any exc_info noted."""
    lines = []
    for call in logger.method_calls:
        message, *args = call.args
        lines.append((message % tuple(args)) + (" [exc_info]" if call.kwargs.get("exc_info") else ""))
    return "\n".join(lines)


@pytest.mark.parametrize(
    ("failure", "reason"),
    [
        ({"side_effect": SEARCH_URL_FAILURE}, "ConnectionError"),
        ({"return_value": _response({}, status_code=502)}, "HTTPError"),
    ],
)
def test_a_failed_search_logs_neither_the_place_nor_the_url(app, failure, reason):
    with (
        app.app_context(),
        patch("broker.clients.weather.requests.get", **failure),
        patch("broker.clients.weather.logger") as logger,
        pytest.raises(GeocodingUnavailableError),
    ):
        search_places("Little Snoring")

    logged = _logged(logger)
    assert logged.startswith(f"Couldn't search Open-Meteo for a weather place: {reason}")
    for trace in ("Little", "Snoring", "open-meteo", "[exc_info]"):
        assert trace not in logged


def test_a_failed_place_lookup_logs_neither_its_id_nor_the_url(app):
    with (
        app.app_context(),
        patch("broker.clients.weather.requests.get", side_effect=PLACE_URL_FAILURE),
        patch("broker.clients.weather.logger") as logger,
        pytest.raises(GeocodingUnavailableError),
    ):
        get_place(2643743)

    logged = _logged(logger)
    assert logged == "Couldn't get a weather place from Open-Meteo: ConnectionError"
