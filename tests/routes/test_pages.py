from unittest.mock import Mock, patch

import pytest

from broker.db import db
from broker.models import Device, DeviceConfig, GlowmarktCredentials, SpotifyToken
from broker.routes.pages import normalize_postcode


@pytest.fixture(autouse=True)
def weather_location_lookup():
    """Stands in for the Open-Meteo check on save so no test hits the network; finds every location by default."""
    with patch("broker.clients.weather.location_found", return_value=True) as lookup:
        yield lookup


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
        config = db.session.get(DeviceConfig, device_id)
        assert config.interval == 20
        assert config.weather_location == "London"
        assert config.spotify_enabled is True
        assert config.tfl_stop_ids == ["940GZZLUEUS", "490000173F"]


def test_device_config_post_falls_back_to_default_interval_for_garbage_input(app, paired_client):
    client, device_id, _ = paired_client
    client.post("/device", data={"interval": "not-a-number"})

    with app.app_context():
        assert db.session.get(DeviceConfig, device_id).interval == 15


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
    with patch("broker.clients.tfl.requests.Session.get", return_value=_mock_get(stop_detail)):
        response = client.get("/device")

    assert b"Euston" in response.data
    assert b"940GZZLUEUS" in response.data


def test_disconnect_removes_stored_token(app, paired_client):
    client, device_id, _ = paired_client
    with app.app_context():
        db.session.add(SpotifyToken(device_id=device_id, refresh_token="refresh"))
        db.session.commit()

    response = client.post("/device/spotify/disconnect")
    assert response.status_code == 302

    with app.app_context():
        assert db.session.get(SpotifyToken, device_id) is None


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
        creds = db.session.get(GlowmarktCredentials, device_id)
        assert creds.username == "someone@example.com"
        assert creds.password_encrypted not in (None, "hunter2")


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
        creds = db.session.get(GlowmarktCredentials, device_id)
        assert creds.username == "someone-else@example.com"
        assert creds.password_encrypted is not None


def _redirects_to_pair(response):
    return response.status_code == 302 and response.headers["Location"] == "/pair"


def test_unpairing_a_device_revokes_the_browser_that_paired_it(paired_client, cli):
    client, device_id, _ = paired_client
    assert client.get("/device").status_code == 200

    cli("unpair", device_id)

    assert _redirects_to_pair(client.get("/device"))
    assert _redirects_to_pair(client.post("/device/spotify/disconnect"))
    assert _redirects_to_pair(client.post("/device/pairing-code"))
    assert _redirects_to_pair(client.get("/"))  # the stale session was cleared, not just refused


def test_a_forgotten_device_sends_the_browser_to_pair_instead_of_erroring(paired_client, cli):
    client, device_id, _ = paired_client

    cli("forget", device_id)

    assert _redirects_to_pair(client.get("/device"))
    assert _redirects_to_pair(client.get("/"))


def test_pairing_again_after_an_unpair_restores_access(paired_client, cli):
    client, device_id, secret = paired_client
    cli("unpair", device_id)
    code = client.get(f"/api/devices/{device_id}/config", headers={"Authorization": f"Bearer {secret}"}).get_json()[
        "pairing_code"
    ]

    assert client.post("/pair", data={"code": code}).status_code == 302

    assert client.get("/device").status_code == 200


def test_device_config_offers_to_link_another_browser_when_no_code_is_live(paired_client):
    client, _, _ = paired_client
    page = client.get("/device").data
    assert b"Link another browser" in page


def test_linking_another_browser_shows_a_code_it_can_pair_with(app, paired_client):
    client, device_id, secret = paired_client

    response = client.post("/device/pairing-code")

    assert response.status_code == 302 and response.headers["Location"] == "/device"
    code = client.get(f"/api/devices/{device_id}/config", headers={"Authorization": f"Bearer {secret}"}).get_json()[
        "pairing_code"
    ]
    page = client.get("/device").data
    assert code.encode() in page and b"Link another browser" not in page

    new_browser = app.test_client()
    assert new_browser.post("/pair", data={"code": code}).status_code == 302
    assert new_browser.get("/device").status_code == 200
    assert client.get("/device").status_code == 200  # the first browser keeps its access


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("SW1A 1AA", "SW1A 1AA"),
        ("sw1a1aa", "SW1A 1AA"),
        ("  n1  9gu ", "N1 9GU"),
        ("EC2V 7HH", "EC2V 7HH"),
        ("M1 1AE", "M1 1AE"),
        ("B33 8TH", "B33 8TH"),
        ("", ""),
        ("   ", ""),
        ("London", None),
        ("SW1A", None),
        ("12345", None),
        ("SW1A 1AAA", None),
    ],
)
def test_normalize_postcode(raw, expected):
    assert normalize_postcode(raw) == expected


def test_device_config_post_saves_normalized_postcode(app, paired_client):
    client, device_id, _ = paired_client
    response = client.post("/device", data={"interval": "15", "postcode": " sw1a1aa "})
    assert response.status_code == 302

    with app.app_context():
        assert db.session.get(DeviceConfig, device_id).postcode == "SW1A 1AA"


def test_device_config_post_rejects_invalid_postcode_and_saves_nothing(app, paired_client):
    client, device_id, _ = paired_client
    client.post("/device", data={"interval": "15", "postcode": "SW1A 1AA", "weather_location": "London"})

    response = client.post(
        "/device", data={"interval": "30", "postcode": "not a postcode", "weather_location": "Leeds"}
    )
    assert response.status_code == 400
    assert b"valid UK postcode" in response.data

    with app.app_context():
        config = db.session.get(DeviceConfig, device_id)
        assert (config.interval, config.postcode, config.weather_location) == (15, "SW1A 1AA", "London")


def test_device_config_post_rejects_weather_location_open_meteo_cant_find(app, paired_client, weather_location_lookup):
    client, device_id, _ = paired_client
    client.post("/device", data={"interval": "15", "weather_location": "London"})

    weather_location_lookup.return_value = False
    response = client.post("/device", data={"interval": "30", "weather_location": "SE17 2PX"})

    assert response.status_code == 400
    assert b"find &#34;SE17 2PX&#34;" in response.data
    assert b"Nothing was saved" in response.data
    weather_location_lookup.assert_called_with("SE17 2PX")
    with app.app_context():
        config = db.session.get(DeviceConfig, device_id)
        assert (config.interval, config.weather_location) == (15, "London")


def test_device_config_post_refuses_an_unverified_weather_location_when_open_meteo_is_unreachable(
    app, paired_client, weather_location_lookup
):
    client, device_id, _ = paired_client
    weather_location_lookup.return_value = None

    response = client.post("/device", data={"interval": "15", "weather_location": "London"})

    assert response.status_code == 400
    assert b"try again in a minute" in response.data
    with app.app_context():
        assert db.session.get(DeviceConfig, device_id).weather_location == ""


def test_device_config_post_blank_weather_location_skips_the_lookup(paired_client, weather_location_lookup):
    client, _, _ = paired_client
    assert client.post("/device", data={"interval": "15", "weather_location": "  "}).status_code == 302
    weather_location_lookup.assert_not_called()


def test_device_config_post_reports_every_invalid_field_at_once(paired_client, weather_location_lookup):
    client, _, _ = paired_client
    weather_location_lookup.return_value = False

    response = client.post("/device", data={"interval": "15", "weather_location": "Nowhere", "postcode": "nope"})

    assert response.status_code == 400
    assert b"find &#34;Nowhere&#34;" in response.data
    assert b"valid UK postcode" in response.data


def _wrong_codes(client, times, ip="203.0.113.7"):
    """Posts `times` wrong codes as if from `ip` (as Caddy forwards it) and returns the last response."""
    response = None
    for _ in range(times):
        response = client.post("/pair", data={"code": "NOPE12"}, headers={"X-Forwarded-For": ip})
    return response


def test_pair_locks_out_an_ip_after_ten_wrong_codes(client):
    assert _wrong_codes(client, 9).status_code == 200

    response = _wrong_codes(client, 1)

    assert response.status_code == 429
    assert response.headers["Retry-After"] == "60"
    assert b"Too many incorrect codes" in response.data
    assert b"wait a minute" in response.data


def test_a_locked_out_ip_is_refused_even_with_the_right_code(client, register_device):
    device_id, secret = register_device()
    code = client.post(
        f"/api/devices/{device_id}/pairing-code", headers={"Authorization": f"Bearer {secret}"}
    ).get_json()["code"]
    _wrong_codes(client, 10)

    response = client.post("/pair", data={"code": code}, headers={"X-Forwarded-For": "203.0.113.7"})

    assert response.status_code == 429
    assert _redirects_to_pair(client.get("/device"))  # no session was set
    # The code wasn't used up: the same browser can pair with it from a different IP.
    assert client.post("/pair", data={"code": code}, headers={"X-Forwarded-For": "198.51.100.2"}).status_code == 302


def test_pair_lockout_tells_the_visitor_how_many_minutes_to_wait(app, client):
    clock = [0.0]
    app.extensions["pair_throttle"]._clock = lambda: clock[0]
    _wrong_codes(client, 10)
    clock[0] += 60

    response = _wrong_codes(client, 10)

    assert response.status_code == 429
    assert response.headers["Retry-After"] == "120"
    assert b"wait 2 minutes" in response.data


def test_pair_throttles_each_forwarded_ip_separately(client):
    _wrong_codes(client, 10, ip="203.0.113.7")

    assert _wrong_codes(client, 1, ip="198.51.100.2").status_code == 200
    assert client.get("/pair").status_code == 200  # the form itself is never throttled


def test_pair_logs_wrong_codes_and_lockouts(client, caplog):
    with caplog.at_level("WARNING"):
        _wrong_codes(client, 10)

    messages = [r.getMessage() for r in caplog.records]
    assert messages.count("Wrong pairing code from 203.0.113.7") == 10
    assert "Too many wrong pairing codes from 203.0.113.7; locked out for 60s" in messages
    assert not any("NOPE12" in m for m in messages)  # never log the guesses themselves


# --- device ID and screen ----------------------------------------------------------------------------


def test_device_page_shows_the_device_id(paired_client):
    client, device_id, _ = paired_client
    page = client.get("/device").get_data(as_text=True)
    assert device_id in page
    assert "Unlink screen" not in page


def test_unlink_screen_clears_the_display_secret(app, paired_client, register_device):
    client, device_id, _ = paired_client
    _, display_secret = register_device("display-secret-0123456789", role="display", device_id=device_id)
    status_url = f"/api/devices/{device_id}/status"
    headers = {"Authorization": f"Bearer {display_secret}"}
    assert "Unlink screen" in client.get("/device").get_data(as_text=True)
    assert client.get(status_url, headers=headers).status_code == 200

    response = client.post("/device/display/unlink")

    assert response.status_code == 302
    with app.app_context():
        assert db.session.get(Device, device_id).display_secret_hash is None
    assert client.get(status_url, headers=headers).status_code == 401
    # The slot is free again for a replacement screen.
    register_device("new-display-secret-0123456789", role="display", device_id=device_id)


def test_unlink_screen_requires_pairing(client):
    response = client.post("/device/display/unlink")
    assert response.status_code == 302
    assert response.headers["Location"] == "/pair"
