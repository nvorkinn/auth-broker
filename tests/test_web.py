from unittest.mock import Mock, patch

from broker import admin
from broker.db import get_db


def _mock_get(json_data, status_code=200):
    response = Mock()
    response.status_code = status_code
    response.json.return_value = json_data
    response.raise_for_status = Mock()
    return response


def test_index_redirects_to_pair_when_unpaired(client):
    response = client.get("/")
    assert response.status_code == 302
    assert response.headers["Location"] == "/pair"


def test_index_redirects_to_device_when_paired(paired_client):
    client, _, _ = paired_client
    response = client.get("/")
    assert response.headers["Location"] == "/device"


def test_pair_get_renders_form(client):
    response = client.get("/pair")
    assert response.status_code == 200
    assert b"pairing code" in response.data.lower()


def test_pair_rejects_unknown_code(client):
    response = client.post("/pair", data={"code": "NOPE12"})
    assert response.status_code == 200
    assert b"invalid or has expired" in response.data


def test_pair_code_is_single_use(client, register_device):
    device_id, secret = register_device()
    code = client.post(
        f"/api/devices/{device_id}/pairing-code", headers={"Authorization": f"Bearer {secret}"}
    ).get_json()["code"]

    first = client.post("/pair", data={"code": code})
    assert first.status_code == 302

    second = client.post("/pair", data={"code": code})
    assert b"invalid or has expired" in second.data


def test_pair_accepts_lowercase_code(client, register_device):
    device_id, secret = register_device()
    code = client.post(
        f"/api/devices/{device_id}/pairing-code", headers={"Authorization": f"Bearer {secret}"}
    ).get_json()["code"]

    response = client.post("/pair", data={"code": code.lower()})
    assert response.status_code == 302


def test_device_config_requires_pairing(client):
    response = client.get("/device")
    assert response.status_code == 302
    assert response.headers["Location"] == "/pair"


def test_device_config_get_renders_settings(paired_client):
    client, device_id, _ = paired_client
    response = client.get("/device")
    assert response.status_code == 200
    assert b"Not connected" in response.data


def test_device_config_post_persists_settings(app, paired_client):
    client, device_id, _ = paired_client
    response = client.post(
        "/device",
        data={
            "stops_order": "940GZZLUEUS,490000173F",
            "interval": "20",
            "weather_location": "London",
            "spotify_enabled": "on",
        },
    )
    assert response.status_code == 302

    with app.app_context():
        row = get_db().execute("SELECT * FROM device_config WHERE device_id = ?", (device_id,)).fetchone()
        assert row["interval"] == 20
        assert row["weather_location"] == "London"
        assert row["spotify_enabled"] == 1
        assert row["tfl_stop_ids"] == '["940GZZLUEUS", "490000173F"]'


def test_device_config_post_falls_back_to_default_interval_for_garbage_input(app, paired_client):
    client, device_id, _ = paired_client
    client.post("/device", data={"interval": "not-a-number"})

    with app.app_context():
        row = get_db().execute("SELECT interval FROM device_config WHERE device_id = ?", (device_id,)).fetchone()
        assert row["interval"] == 15


def test_device_config_shows_resolved_stop_names(paired_client):
    client, device_id, _ = paired_client
    client.post("/device", data={"stops_order": "940GZZLUEUS", "interval": "15"})

    stop_detail = {
        "id": "940GZZLUEUS",
        "commonName": "Euston Underground Station",
        "stopType": "NaptanMetroStation",
        "modes": ["tube"],
        "lines": [{"name": "Victoria"}],
    }
    with patch("broker.web.requests.Session.get", return_value=_mock_get(stop_detail)):
        response = client.get("/device")

    assert b"Euston" in response.data
    assert b"940GZZLUEUS" in response.data


def test_disconnect_removes_stored_token(app, paired_client):
    client, device_id, _ = paired_client
    with app.app_context():
        db = get_db()
        db.execute("INSERT INTO spotify_tokens (device_id, refresh_token) VALUES (?, ?)", (device_id, "refresh"))
        db.commit()

    response = client.post("/device/spotify/disconnect")
    assert response.status_code == 302

    with app.app_context():
        row = get_db().execute("SELECT 1 FROM spotify_tokens WHERE device_id = ?", (device_id,)).fetchone()
        assert row is None


def test_disconnect_requires_pairing(client):
    response = client.post("/device/spotify/disconnect")
    assert response.status_code == 302
    assert response.headers["Location"] == "/pair"


def test_device_config_post_stores_encrypted_glowmarkt_password(app, paired_client):
    client, device_id, _ = paired_client
    response = client.post(
        "/device",
        data={"interval": "15", "glowmarkt_username": "someone@example.com", "glowmarkt_password": "hunter2"},
    )
    assert response.status_code == 302

    with app.app_context():
        row = (
            get_db()
            .execute("SELECT username, password_encrypted FROM glowmarkt_credentials WHERE device_id = ?", (device_id,))
            .fetchone()
        )
        assert row["username"] == "someone@example.com"
        assert row["password_encrypted"] not in (None, "hunter2")


def test_device_config_get_never_shows_saved_password(paired_client):
    client, device_id, _ = paired_client
    client.post(
        "/device", data={"interval": "15", "glowmarkt_username": "someone@example.com", "glowmarkt_password": "hunter2"}
    )

    response = client.get("/device")
    assert b"hunter2" not in response.data
    assert b"someone@example.com" in response.data
    assert b"Saved" in response.data


def test_device_config_post_blank_password_keeps_existing_one(app, paired_client):
    client, device_id, _ = paired_client
    client.post(
        "/device", data={"interval": "15", "glowmarkt_username": "someone@example.com", "glowmarkt_password": "hunter2"}
    )
    client.post("/device", data={"interval": "15", "glowmarkt_username": "someone-else@example.com"})

    with app.app_context():
        row = (
            get_db()
            .execute("SELECT username, password_encrypted FROM glowmarkt_credentials WHERE device_id = ?", (device_id,))
            .fetchone()
        )
        assert row["username"] == "someone-else@example.com"
        assert row["password_encrypted"] is not None


def test_search_stops_empty_query_returns_empty_list(client):
    response = client.get("/api/tfl/search?q=a")
    assert response.status_code == 200
    assert response.get_json() == []


def test_search_stops_finds_tube_and_bus_matches(client):
    search_result = {"matches": [{"id": "1", "commonName": "Kings Cross"}]}
    stop_detail = {
        "id": "1",
        "naptanId": "1",
        "commonName": "Kings Cross Underground Station",
        "stopType": "NaptanMetroStation",
        "lines": [{"name": "Piccadilly"}],
        "children": [],
    }

    def fake_get(self, url, params=None, timeout=None):
        if "Search" in url:
            return _mock_get(search_result)
        return _mock_get(stop_detail)

    with patch("broker.web.requests.Session.get", fake_get):
        response = client.get("/api/tfl/search?q=kings")

    assert response.status_code == 200
    results = response.get_json()
    assert len(results) == 1
    assert results[0]["mode"] == "tube"
    assert results[0]["name"] == "Kings Cross"


def _redirects_to_pair(response):
    return response.status_code == 302 and response.headers["Location"] == "/pair"


def test_unpairing_a_device_revokes_the_browser_that_paired_it(paired_client):
    client, device_id, _ = paired_client
    assert client.get("/device").status_code == 200

    admin.main(["unpair", device_id])

    assert _redirects_to_pair(client.get("/device"))
    assert _redirects_to_pair(client.post("/device/spotify/disconnect"))
    assert _redirects_to_pair(client.get("/"))  # the stale session was cleared, not just refused


def test_a_forgotten_device_sends_the_browser_to_pair_instead_of_erroring(paired_client):
    client, device_id, _ = paired_client

    admin.main(["forget", device_id])

    assert _redirects_to_pair(client.get("/device"))
    assert _redirects_to_pair(client.get("/"))


def test_pairing_again_after_an_unpair_restores_access(paired_client):
    client, device_id, secret = paired_client
    admin.main(["unpair", device_id])
    code = client.get(f"/api/devices/{device_id}/config", headers={"Authorization": f"Bearer {secret}"}).get_json()[
        "pairing_code"
    ]

    assert client.post("/pair", data={"code": code}).status_code == 302

    assert client.get("/device").status_code == 200
