from unittest.mock import Mock, patch


def _mock_get(json_data, status_code=200):
    response = Mock()
    response.status_code = status_code
    response.json.return_value = json_data
    response.raise_for_status = Mock()
    return response


def test_search_stops_requires_a_paired_browser(client):
    """It spends the broker's TfL key, so it isn't open to anyone who finds the URL."""
    with patch("broker.clients.tfl.requests.Session.get") as get:
        response = client.get("/api/tfl/search?q=kings")

    assert response.status_code == 302
    assert response.headers["Location"] == "/pair"
    get.assert_not_called()


def test_search_stops_empty_query_returns_empty_list(paired_client):
    client, _, _ = paired_client
    response = client.get("/api/tfl/search?q=a")
    assert response.status_code == 200
    assert response.get_json() == []


def test_search_stops_finds_tube_and_bus_matches(paired_client):
    client, _, _ = paired_client
    search_result = {"matches": [{"id": "1", "commonName": "Kings Cross"}]}
    stop_detail = {
        "id": "1",
        "naptanId": "1",
        "commonName": "Kings Cross Underground Station",
        "stopType": "NaptanMetroStation",
        "lines": [{"name": "Piccadilly"}],
        "children": [],
    }

    def fake_get(self, url, params=None, timeout=None, headers=None):
        if "Search" in url:
            return _mock_get(search_result)
        return _mock_get(stop_detail)

    with patch("broker.clients.tfl.requests.Session.get", fake_get):
        response = client.get("/api/tfl/search?q=kings")

    assert response.status_code == 200
    results = response.get_json()
    assert len(results) == 1
    assert results[0]["mode"] == "tube"
    assert results[0]["name"] == "Kings Cross"


def test_search_stops_lists_bus_stops_nested_under_a_hub_once_each(paired_client):
    client, _, _ = paired_client
    search_result = {"matches": [{"id": "hub"}, {"id": "hub"}]}
    hub = {
        "id": "hub",
        "commonName": "Elephant & Castle",
        "stopType": "TransportInterchange",
        "children": [
            {
                "naptanId": "490G1",
                "commonName": "Elephant & Castle",
                "stopType": "NaptanPublicBusCoachTram",
                "stopLetter": "F",
                "lines": [{"name": str(n)} for n in range(1, 9)],
            },
            {"naptanId": "490G2", "commonName": "Newington Butts", "stopType": "NaptanPublicBusCoachTram"},
        ],
    }

    def fake_get(self, url, params=None, timeout=None, headers=None):
        return _mock_get(search_result if "Search" in url else hub)

    with patch("broker.clients.tfl.requests.Session.get", fake_get):
        results = client.get("/api/tfl/search?q=elephant").get_json()

    assert [r["id"] for r in results] == ["490G1", "490G2"]
    assert results[0]["mode"] == "bus"
    assert results[0]["subtitle"] == "Stop F • 1, 2, 3, 4, 5, 6 (+2 more)"
    assert results[1]["subtitle"] == "Bus • "


def test_search_stops_returns_500_when_tfl_search_fails(paired_client):
    client, _, _ = paired_client
    with patch("broker.clients.tfl.requests.Session.get", return_value=_mock_get({}, status_code=503)) as get:
        get.return_value.raise_for_status.side_effect = Exception("503")
        response = client.get("/api/tfl/search?q=kings")

    assert response.status_code == 500
    assert response.get_json() == []


GEOCODER = {
    "features": [
        {"properties": {"id": "NSR:StopPlace:58366", "locality": "Oslo"}},
        {"properties": {"id": "NSR:StopPlace:58366", "locality": "Oslo"}},
        {"properties": {"id": "KVE:TopographicPlace:1", "locality": "Oslo"}},
    ]
}
STOP_PLACES = {
    "data": {
        "stopPlaces": [
            {
                "id": "NSR:StopPlace:58366",
                "name": "Majorstuen",
                "transportMode": ["metro"],
                "quays": [
                    {
                        "id": "NSR:Quay:11145",
                        "publicCode": "2",
                        "lines": [{"publicCode": "3"}, {"publicCode": "1"}],
                        "estimatedCalls": [{"destinationDisplay": {"frontText": "Mortensrud"}}],
                    },
                    {
                        "id": "NSR:Quay:11144",
                        "publicCode": "1",
                        "lines": [{"publicCode": "1"}],
                        "estimatedCalls": [{"destinationDisplay": {"frontText": "Frognerseteren"}}],
                    },
                ],
            }
        ]
    }
}


def test_search_lists_norwegian_stations_with_their_platforms_indented(paired_client):
    client, _, _ = paired_client

    def fake_get(self, url, params=None, timeout=None, headers=None):
        if "geocoder" in url:
            assert headers == {"ET-Client-Name": "nvorkinn-countdown"}
            return _mock_get(GEOCODER)
        return _mock_get({"matches": []})  # TfL knows no Majorstuen

    with (
        patch("broker.clients.tfl.requests.Session.get", fake_get),
        patch("broker.clients.entur.requests.Session.post", return_value=_mock_get(STOP_PLACES)),
    ):
        results = client.get("/api/stops/search?q=majorstuen").get_json()

    assert [(r["id"], r["indent"]) for r in results] == [
        ("NSR:StopPlace:58366", False),
        ("NSR:Quay:11144", True),
        ("NSR:Quay:11145", True),
    ]
    station, platform_1, platform_2 = results
    assert station | {"lines": None} == {
        "id": "NSR:StopPlace:58366",
        "source": "entur",
        "name": "Majorstuen",
        "area": "Oslo",
        "mode": "metro",
        "letter": "",
        "lines": None,
        "subtitle": "T-bane • All platforms • 1, 3",
        "indent": False,
    }
    assert platform_1["subtitle"] == "Platform 1 • towards Frognerseteren • 1"
    assert platform_2["subtitle"] == "Platform 2 • towards Mortensrud • 1, 3"
    assert platform_2["name"] == "Majorstuen"


def test_search_still_answers_when_one_source_fails(paired_client):
    client, _, _ = paired_client

    def fake_get(self, url, params=None, timeout=None, headers=None):
        if "geocoder" in url:
            raise ConnectionError("Entur is down")
        if "Search" in url:
            return _mock_get({"matches": [{"id": "1", "commonName": "Kings Cross"}]})
        return _mock_get({"id": "1", "commonName": "Kings Cross", "stopType": "NaptanMetroStation", "children": []})

    with patch("broker.clients.tfl.requests.Session.get", fake_get):
        response = client.get("/api/stops/search?q=kings")

    assert response.status_code == 200
    assert [(r["source"], r["area"]) for r in response.get_json()] == [("tfl", "London")]


def test_search_lists_norway_first_for_a_norwegian_looking_query(paired_client):
    client, _, _ = paired_client

    def fake_get(self, url, params=None, timeout=None, headers=None):
        if "geocoder" in url:
            return _mock_get(GEOCODER)
        if "Search" in url:
            return _mock_get({"matches": [{"id": "1", "commonName": "Kings Cross"}]})
        return _mock_get({"id": "1", "commonName": "Kings Cross", "stopType": "NaptanMetroStation", "children": []})

    with (
        patch("broker.clients.tfl.requests.Session.get", fake_get),
        patch("broker.clients.entur.requests.Session.post", return_value=_mock_get(STOP_PLACES)),
    ):
        results = client.get("/api/stops/search?q=storgåta").get_json()

    assert [r["source"] for r in results] == ["entur", "entur", "entur", "tfl"]
