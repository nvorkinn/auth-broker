from unittest.mock import Mock, patch

import requests


def _response(json_data):
    response = Mock()
    response.status_code = 200
    response.json.return_value = json_data
    response.raise_for_status = Mock()
    return response


LONDONS = {
    "results": [
        {
            "id": 2643743,
            "name": "London",
            "latitude": 51.50853,
            "longitude": -0.12574,
            "country_code": "GB",
            "admin1": "England",
            "country": "United Kingdom",
        },
        {
            "id": 6058560,
            "name": "London",
            "latitude": 42.98339,
            "longitude": -81.23304,
            "country_code": "CA",
            "admin1": "Ontario",
            "country": "Canada",
        },
    ]
}


def test_place_search_requires_a_paired_browser(client):
    with patch("broker.clients.weather.requests.get") as get:
        response = client.get("/api/places/search?q=london")

    assert response.status_code == 302
    assert response.headers["Location"] == "/pair"
    get.assert_not_called()


def test_place_search_returns_open_meteos_matches_best_first(paired_client):
    client, _, _ = paired_client
    with patch("broker.clients.weather.requests.get", return_value=_response(LONDONS)) as get:
        response = client.get("/api/places/search?q=%20London%20")

    assert response.status_code == 200
    assert get.call_args.kwargs["params"]["name"] == "London"
    assert response.get_json() == [
        {
            "id": 2643743,
            "name": "London",
            "admin1": "England",
            "country": "United Kingdom",
            "country_code": "GB",
            "latitude": 51.50853,
            "longitude": -0.12574,
            "label": "London, England, United Kingdom",
        },
        {
            "id": 6058560,
            "name": "London",
            "admin1": "Ontario",
            "country": "Canada",
            "country_code": "CA",
            "latitude": 42.98339,
            "longitude": -81.23304,
            "label": "London, Ontario, Canada",
        },
    ]


def test_place_search_ignores_a_query_too_short_to_search(paired_client):
    client, _, _ = paired_client
    with patch("broker.clients.weather.requests.get") as get:
        for query in ("", "l", " l "):
            response = client.get(f"/api/places/search?q={query}")
            assert (response.status_code, response.get_json()) == (200, [])
    get.assert_not_called()


def test_place_search_is_empty_rather_than_an_error_when_open_meteo_is_down(paired_client):
    client, _, _ = paired_client
    with patch("broker.clients.weather.requests.get", side_effect=requests.Timeout):
        response = client.get("/api/places/search?q=london")

    assert (response.status_code, response.get_json()) == (200, [])
