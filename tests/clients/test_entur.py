from unittest.mock import Mock, patch

import pytest

from broker.clients import entur


def _response(json_data):
    response = Mock()
    response.json.return_value = json_data
    response.raise_for_status = Mock()
    return response


def test_resolve_stops_names_stations_and_platforms(app):
    stop_places = {
        "data": {
            "stopPlaces": [
                {"id": "NSR:StopPlace:6505", "name": "Jernbanetorget", "transportMode": ["bus"], "quays": []}
            ]
        }
    }
    quays = {
        "data": {
            "quays": [
                {
                    "id": "NSR:Quay:11145",
                    "publicCode": "2",
                    "lines": [{"publicCode": "5"}],
                    "estimatedCalls": [],
                    "stopPlace": {"id": "NSR:StopPlace:58366", "name": "Majorstuen", "transportMode": ["metro"]},
                },
                None,
            ]
        }
    }

    def fake_post(self, url, json=None, headers=None, timeout=None):
        return _response(stop_places if "StopPlaces" in json["query"] else quays)

    with app.app_context(), patch("broker.clients.entur.requests.Session.post", fake_post):
        resolved = entur.resolve_stops(["NSR:StopPlace:6505", "NSR:Quay:11145", "NSR:Quay:1"])

    assert set(resolved) == {"NSR:StopPlace:6505", "NSR:Quay:11145"}
    assert resolved["NSR:StopPlace:6505"]["subtitle"] == "Bus"
    assert resolved["NSR:Quay:11145"] | {"lines": None} == {
        "id": "NSR:Quay:11145",
        "source": "entur",
        "name": "Majorstuen",
        "area": "",
        "mode": "metro",
        "letter": "2",
        "lines": None,
        "subtitle": "Platform 2 • 5",
        "indent": True,
    }


def test_resolve_stops_gives_up_quietly_when_entur_is_down(app):
    with app.app_context(), patch("broker.clients.entur.requests.Session.post", side_effect=ConnectionError("down")):
        assert entur.resolve_stops(["NSR:StopPlace:6505"]) == {}


def test_a_bus_stop_pole_is_a_stop_not_a_platform(app):
    quay = {"id": "NSR:Quay:7", "publicCode": "C", "lines": [], "estimatedCalls": []}
    assert entur._quay_result(quay, "Jernbanetorget", "bus")["subtitle"] == "Stop C"


def test_query_raises_on_graphql_errors():
    http = Mock()
    http.post.return_value = _response({"errors": [{"message": "bad id"}]})
    with pytest.raises(RuntimeError) as excinfo:
        entur._query("query", {}, http)
    assert str(excinfo.value) == "bad id"
