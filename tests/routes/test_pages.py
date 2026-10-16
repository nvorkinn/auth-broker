import re
from unittest.mock import Mock, patch

import pytest

from broker.db import db
from broker.models import Device, DeviceConfig, GlowmarktCredentials, SpotifyToken
from broker.routes.pages import normalize_postcode, postcode_country


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
    code = client.post("/api/devices/pairing-code", headers={"Authorization": f"Bearer {secret}"}).get_json()["code"]

    first = client.post("/pair", data={"code": code})
    assert first.status_code == 302

    second = client.post("/pair", data={"code": code})
    assert b"invalid or has expired" in second.data


def test_pair_accepts_lowercase_code(client, register_device):
    device_id, secret = register_device()
    code = client.post("/api/devices/pairing-code", headers={"Authorization": f"Bearer {secret}"}).get_json()["code"]

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


def test_device_config_prefills_an_empty_weather_location_from_the_ip(app, paired_client):
    client, device_id, _ = paired_client
    with patch("broker.services.geoip.suggest_weather_location", return_value="London") as suggest:
        response = client.get("/device", environ_base={"REMOTE_ADDR": "81.2.69.1"})
    suggest.assert_called_once_with("81.2.69.1")
    assert b'name="weather_location" value="London"' in response.data
    assert b"Suggested from your location" in response.data

    # Only shown: nothing is stored until it's saved.
    with app.app_context():
        assert db.session.get(DeviceConfig, device_id).weather_location == ""


def test_device_config_keeps_a_saved_weather_location_over_a_suggestion(paired_client):
    client, _, _ = paired_client
    client.post("/device", data={"interval": "15", "weather_location": "Leeds"})
    with patch("broker.services.geoip.suggest_weather_location", return_value="London") as suggest:
        response = client.get("/device")
    suggest.assert_not_called()
    assert b'name="weather_location" value="Leeds"' in response.data
    assert b"Suggested from your location" not in response.data


def test_device_config_shows_orientation_as_vertical_and_read_only(paired_client):
    client, _, _ = paired_client
    html = client.get("/device").get_data(as_text=True)
    assert '<fieldset class="form-group" disabled>' in html
    assert re.search(r'id="orientation_vertical"[^>]*checked', html)
    assert not re.search(r'id="orientation_horizontal"[^>]*checked', html)
    assert '<input type="hidden" name="orientation" value="vertical">' in html


def test_device_config_groups_fields_into_sections(paired_client):
    client, _, _ = paired_client
    html = client.get("/device").get_data(as_text=True)
    titles = re.findall(r'<h2 class="card-title">([^<]+)</h2>', html)
    assert titles[:3] == ["General", "Weather", "Notice board"]
    general, weather, notice_board = re.split(r'<h2 class="card-title">', html)[1:4]
    assert 'id="interval"' in general and 'name="orientation"' in general
    assert 'name="clock_format"' in general
    assert 'id="weather_location"' in weather
    assert 'id="postcode"' in notice_board


def test_device_config_shows_vertical_even_when_another_orientation_is_stored(app, paired_client):
    client, device_id, _ = paired_client
    with app.app_context():
        db.session.get(DeviceConfig, device_id).orientation = "horizontal"
        db.session.commit()
    html = client.get("/device").get_data(as_text=True)
    assert re.search(r'id="orientation_vertical"[^>]*checked', html)
    assert '<input type="hidden" name="orientation" value="vertical">' in html


def test_device_config_post_persists_orientation(app, paired_client):
    client, device_id, _ = paired_client
    response = client.post("/device", data={"interval": "15", "orientation": "horizontal"})
    assert response.status_code == 302
    with app.app_context():
        assert db.session.get(DeviceConfig, device_id).orientation == "horizontal"


def test_device_config_post_without_orientation_keeps_the_stored_one(app, paired_client):
    client, device_id, _ = paired_client
    with app.app_context():
        db.session.get(DeviceConfig, device_id).orientation = "horizontal"
        db.session.commit()
    response = client.post("/device", data={"interval": "15"})
    assert response.status_code == 302
    with app.app_context():
        assert db.session.get(DeviceConfig, device_id).orientation == "horizontal"


def test_device_config_post_rejects_an_unknown_orientation(app, paired_client):
    client, device_id, _ = paired_client
    response = client.post("/device", data={"interval": "15", "orientation": "diagonal"})
    assert response.status_code == 400
    assert b"Orientation must be one of horizontal, vertical." in response.data
    with app.app_context():
        assert db.session.get(DeviceConfig, device_id).orientation == "vertical"


def test_device_config_shows_both_clock_formats_and_defaults_to_24_hour(paired_client):
    client, _, _ = paired_client
    html = client.get("/device").get_data(as_text=True)
    assert re.findall(r'name="clock_format" value="([^"]+)"', html) == ["24h", "12h"]
    assert re.search(r'id="clock_format_24h"[^>]*checked', html)
    assert not re.search(r'id="clock_format_12h"[^>]*checked', html)
    assert "24-hour (13:00)" in html and "12-hour (1:00 PM)" in html


@pytest.mark.parametrize("clock_format", ["12h", "24h"])
def test_device_config_post_persists_and_shows_the_clock_format(app, paired_client, clock_format):
    client, device_id, _ = paired_client
    with app.app_context():
        db.session.get(DeviceConfig, device_id).clock_format = "24h" if clock_format == "12h" else "12h"
        db.session.commit()

    response = client.post("/device", data={"clock_format": clock_format})

    assert response.status_code == 302
    with app.app_context():
        assert db.session.get(DeviceConfig, device_id).clock_format == clock_format
    html = client.get(response.headers["Location"]).get_data(as_text=True)
    assert re.search(rf'id="clock_format_{clock_format}"[^>]*checked', html)
    assert len(re.findall(r'name="clock_format"[^>]*checked', html)) == 1


def test_device_config_post_without_clock_format_keeps_the_stored_one(app, paired_client):
    client, device_id, _ = paired_client
    with app.app_context():
        db.session.get(DeviceConfig, device_id).clock_format = "12h"
        db.session.commit()

    assert client.post("/device", data={"interval": "20"}).status_code == 302

    with app.app_context():
        config = db.session.get(DeviceConfig, device_id)
        assert config.clock_format == "12h"
        assert config.interval == 20


@pytest.mark.parametrize("clock_format", ["", "invalid", "12", "24"])
def test_device_config_post_rejects_invalid_clock_formats_without_saving_anything(app, paired_client, clock_format):
    client, device_id, _ = paired_client
    with app.app_context():
        db.session.get(DeviceConfig, device_id).clock_format = "12h"
        db.session.commit()

    response = client.post(
        "/device",
        data={
            "clock_format": clock_format,
            "interval": "20",
            "weather_location": "London",
            "spotify_enabled": "on",
            "spotify_style": "hero",
            "glowmarkt_username": "new-account",
            "glowmarkt_password": "new-password",
        },
    )

    assert response.status_code == 400
    assert b"The clock format must be one of 24h, 12h." in response.data
    assert b"Nothing was saved." in response.data
    with app.app_context():
        config = db.session.get(DeviceConfig, device_id)
        assert (config.clock_format, config.interval, config.weather_location, config.spotify_enabled) == (
            "12h",
            15,
            "",
            False,
        )
        assert config.spotify_style == "classic"
        assert db.session.get(GlowmarktCredentials, device_id) is None


def test_device_config_does_not_save_clock_format_when_another_field_is_invalid(app, paired_client):
    client, device_id, _ = paired_client

    response = client.post("/device", data={"clock_format": "12h", "postcode": "invalid"})

    assert response.status_code == 400
    assert b"Nothing was saved." in response.data
    with app.app_context():
        assert db.session.get(DeviceConfig, device_id).clock_format == "24h"


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


def test_unpairing_a_device_revokes_the_user_that_paired_it(paired_client, cli):
    client, device_id, _ = paired_client
    assert client.get("/device").status_code == 200

    cli("unpair", device_id)

    assert _redirects_to_pair(client.get("/device"))
    assert _redirects_to_pair(client.post("/device/spotify/disconnect"))
    assert _redirects_to_pair(client.get("/"))


def test_a_forgotten_device_sends_the_browser_to_pair_instead_of_erroring(paired_client, cli):
    client, device_id, _ = paired_client

    cli("forget", device_id)

    assert _redirects_to_pair(client.get("/device"))
    assert _redirects_to_pair(client.get("/"))


def test_pairing_again_after_an_unpair_restores_access(paired_client, cli):
    client, device_id, secret = paired_client
    cli("unpair", device_id)
    code = client.get("/api/config", headers={"Authorization": f"Bearer {secret}"}).get_json()["pairing_code"]

    assert client.post("/pair", data={"code": code}).status_code == 302

    assert client.get("/device").status_code == 200


def test_another_user_cannot_see_the_settings(app, paired_client):
    other = app.test_client()
    other.environ_base["HTTP_X_AUTHENTIK_UID"] = "someone-else"

    assert _redirects_to_pair(other.get("/device"))


def test_pages_need_the_login_proxy_header(anonymous_client, paired_client):
    assert anonymous_client.get("/device").status_code == 401
    assert anonymous_client.get("/pair").status_code == 401
    assert anonymous_client.post("/pair", data={"code": "ABC234"}).status_code == 401


def test_a_code_hands_a_device_to_the_user_who_enters_it(app, paired_client):
    client, device_id, secret = paired_client
    code = client.post("/api/devices/pairing-code", headers={"Authorization": f"Bearer {secret}"}).get_json()["code"]
    new_owner = app.test_client()
    new_owner.environ_base["HTTP_X_AUTHENTIK_UID"] = "someone-else"

    assert new_owner.post("/pair", data={"code": code}).status_code == 302

    assert new_owner.get("/device").status_code == 200
    assert _redirects_to_pair(client.get("/device"))


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
        ("0150", "0150"),
        (" 9 0 1 0 ", "9010"),
        ("150", None),
    ],
)
def test_normalize_postcode(raw, expected):
    assert normalize_postcode(raw) == expected


@pytest.mark.parametrize(("postcode", "country"), [("SW1A 1AA", "GB"), ("0150", "NO"), ("", None)])
def test_postcode_country(postcode, country):
    assert postcode_country(postcode) == country


def test_device_config_post_saves_a_norwegian_postcode_and_language(app, paired_client):
    client, device_id, _ = paired_client
    response = client.post("/device", data={"interval": "15", "postcode": " 0150 ", "language": "nb"})
    assert response.status_code == 302

    with app.app_context():
        config = db.session.get(DeviceConfig, device_id)
        assert (config.postcode, config.language) == ("0150", "nb")


def test_device_config_post_rejects_an_unknown_language(app, paired_client):
    client, device_id, _ = paired_client
    response = client.post("/device", data={"interval": "15", "language": "sv"})
    assert response.status_code == 400
    assert b"language must be one of en, nb" in response.data

    with app.app_context():
        assert db.session.get(DeviceConfig, device_id).language == "en"


def test_device_page_offers_the_languages(paired_client):
    client, _, _ = paired_client
    page = client.get("/device").get_data(as_text=True)
    assert 'id="language_nb" name="language" value="nb"' in page
    assert "Departures" in page


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
    assert b"valid UK or Norwegian postcode" in response.data

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
    assert b"valid UK or Norwegian postcode" in response.data


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
    code = client.post("/api/devices/pairing-code", headers={"Authorization": f"Bearer {secret}"}).get_json()["code"]
    _wrong_codes(client, 10)

    response = client.post("/pair", data={"code": code}, headers={"X-Forwarded-For": "203.0.113.7"})

    assert response.status_code == 429
    assert _redirects_to_pair(client.get("/device"))  # not paired
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


def test_unlink_screen_clears_the_display_secret(app, client, split_device):
    device_id, renderer_secret, display_secret = split_device
    code = client.get("/api/config", headers={"Authorization": f"Bearer {renderer_secret}"}).get_json()["pairing_code"]
    client.post("/pair", data={"code": code})
    frame_headers = {"Authorization": f"Bearer {display_secret}"}
    assert "Unlink screen" in client.get("/device").get_data(as_text=True)
    assert client.get("/api/frame", headers=frame_headers).status_code == 404  # authenticated, no frame yet

    response = client.post("/device/display/unlink")

    assert response.status_code == 302
    with app.app_context():
        assert db.session.get(Device, device_id).display_secret_hash is None
    assert client.get("/api/frame", headers=frame_headers).status_code == 401
    assert "Unlink screen" not in client.get("/device").get_data(as_text=True)


def test_unlink_screen_requires_pairing(client):
    response = client.post("/device/display/unlink")
    assert response.status_code == 302
    assert response.headers["Location"] == "/pair"


def test_device_config_shows_the_stored_tfl_style_selected(app, paired_client):
    client, device_id, _ = paired_client
    with app.app_context():
        db.session.get(DeviceConfig, device_id).tfl_style = "card"
        db.session.commit()
    html = client.get("/device").get_data(as_text=True)
    assert re.findall(r'name="tfl_style" value="(\w+)"', html) == ["classic", "hero", "grouped", "card"]
    assert re.search(r'name="tfl_style" value="card" checked', html)
    assert not re.search(r'name="tfl_style" value="classic" checked', html)


def test_device_config_shows_a_picture_of_every_tfl_style(app, paired_client):
    client, _, _ = paired_client
    html = client.get("/device").get_data(as_text=True)
    pictures = re.findall(r'<img src="(/static/tfl-styles/\w+\.png)"', html)
    assert pictures == [f"/static/tfl-styles/{style}.png" for style in ("classic", "hero", "grouped", "card")]
    for picture in pictures:
        response = client.get(picture)
        assert response.status_code == 200
        assert response.mimetype == "image/png"


def test_device_config_post_persists_the_tfl_style(app, paired_client):
    client, device_id, _ = paired_client
    response = client.post("/device", data={"interval": "15", "tfl_style": "hero"})
    assert response.status_code == 302
    with app.app_context():
        assert db.session.get(DeviceConfig, device_id).tfl_style == "hero"


def test_device_config_post_without_a_tfl_style_keeps_the_stored_one(app, paired_client):
    client, device_id, _ = paired_client
    with app.app_context():
        db.session.get(DeviceConfig, device_id).tfl_style = "grouped"
        db.session.commit()
    client.post("/device", data={"interval": "15"})
    with app.app_context():
        assert db.session.get(DeviceConfig, device_id).tfl_style == "grouped"


def test_device_config_post_rejects_an_unknown_tfl_style(app, paired_client):
    client, device_id, _ = paired_client
    response = client.post("/device", data={"interval": "15", "tfl_style": "fancy"})
    assert response.status_code == 400
    assert b"The TfL style must be one of classic, hero, grouped, card." in response.data
    with app.app_context():
        assert db.session.get(DeviceConfig, device_id).tfl_style == "classic"


def test_device_config_shows_the_stored_spotify_style_selected(app, paired_client):
    client, device_id, _ = paired_client
    with app.app_context():
        db.session.get(DeviceConfig, device_id).spotify_style = "hero"
        db.session.commit()
    html = client.get("/device").get_data(as_text=True)
    assert re.findall(r'name="spotify_style" value="(\w+)"', html) == ["classic", "airy", "hero", "card"]
    assert re.search(r'name="spotify_style" value="hero" checked', html)
    assert not re.search(r'name="spotify_style" value="classic" checked', html)
    # The TfL picker beside it keeps its own choice.
    assert re.search(r'name="tfl_style" value="classic" checked', html)


def test_device_config_shows_a_picture_of_every_spotify_style(app, paired_client):
    client, _, _ = paired_client
    html = client.get("/device").get_data(as_text=True)
    pictures = re.findall(r'<img src="(/static/spotify-styles/\w+\.png)"', html)
    assert pictures == [f"/static/spotify-styles/{style}.png" for style in ("classic", "airy", "hero", "card")]
    for picture in pictures:
        response = client.get(picture)
        assert response.status_code == 200
        assert response.mimetype == "image/png"


def test_device_config_post_persists_the_spotify_style(app, paired_client):
    client, device_id, _ = paired_client
    response = client.post("/device", data={"interval": "15", "spotify_style": "airy"})
    assert response.status_code == 302
    with app.app_context():
        assert db.session.get(DeviceConfig, device_id).spotify_style == "airy"


def test_device_config_post_without_a_spotify_style_keeps_the_stored_one(app, paired_client):
    client, device_id, _ = paired_client
    with app.app_context():
        db.session.get(DeviceConfig, device_id).spotify_style = "card"
        db.session.commit()
    client.post("/device", data={"interval": "15"})
    with app.app_context():
        assert db.session.get(DeviceConfig, device_id).spotify_style == "card"


def test_device_config_post_rejects_an_unknown_spotify_style(app, paired_client):
    client, device_id, _ = paired_client
    response = client.post("/device", data={"interval": "15", "spotify_style": "fancy"})
    assert response.status_code == 400
    assert b"The Spotify style must be one of classic, airy, hero, card." in response.data
    with app.app_context():
        assert db.session.get(DeviceConfig, device_id).spotify_style == "classic"
