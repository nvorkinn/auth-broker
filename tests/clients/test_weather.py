from unittest.mock import Mock, patch

import requests

from broker.clients.weather import location_found


def _geocoding_response(json_data):
    response = Mock()
    response.json.return_value = json_data
    response.raise_for_status = Mock()
    return response


def test_weather_location_found_asks_open_meteo_the_same_way_the_device_does(app):
    found = {"results": [{"name": "London", "latitude": 51.5, "longitude": -0.13}]}
    with (
        app.app_context(),
        patch("broker.clients.weather.requests.get", return_value=_geocoding_response(found)) as get,
    ):
        assert location_found("London") is True

    get.assert_called_once_with(
        "https://geocoding-api.open-meteo.com/v1/search",
        params={"name": "London", "count": 1, "format": "json"},
        timeout=5,
    )


def test_weather_location_found_is_false_when_open_meteo_has_no_results(app):
    not_found = {"generationtime_ms": 0.4}  # Open-Meteo leaves "results" out entirely when nothing matches
    with app.app_context(), patch("broker.clients.weather.requests.get", return_value=_geocoding_response(not_found)):
        assert location_found("SE17 2PX") is False


def test_weather_location_found_is_none_when_open_meteo_is_unreachable(app):
    with app.app_context(), patch("broker.clients.weather.requests.get", side_effect=requests.ConnectionError):
        assert location_found("London") is None
