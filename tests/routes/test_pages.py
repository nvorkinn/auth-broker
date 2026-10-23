import re
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

import pytest
import redis

from broker.clients import weather
from broker.db import db
from broker.models import Device, DeviceConfig, GitHubToken, GlowmarktCredentials, SpotifyToken
from broker.services import pairing, weather_place
from broker.services.postcodes import normalize_postcode, postcode_country


def _place(name, place_id=1, **fields):
    """Return a place as the weather client gives Open-Meteo's: one with only a name has the name as its label."""
    return weather._place({"id": place_id, "name": name, "latitude": 1.0, "longitude": 2.0, **fields})


@pytest.fixture(autouse=True)
def weather_location_lookup():
    """Stands in for Open-Meteo's search on save so no test hits the network; finds every location by default."""
    with patch("broker.clients.weather.search_places", side_effect=lambda name, **_: [_place(name)]) as lookup:
        yield lookup


@pytest.fixture(autouse=True)
def weather_place_lookup():
    """Stands in for Open-Meteo's lookup of a picked place by id; finds none by default."""
    with patch("broker.clients.weather.get_place", return_value=None) as lookup:
        yield lookup


def _issue(app, device_id):
    """Issue the device a pairing code, as `flask devices code` does; return it."""
    with app.app_context():
        return pairing.issue_code(device_id)


def _mock_get(json_data, status_code=200):
    response = Mock()
    response.status_code = status_code
    response.json.return_value = json_data
    response.raise_for_status = Mock()
    return response


REPO = Path(__file__).parents[2]


def test_pages_show_the_logo_the_sign_in_shows(client):
    """The header's pill is a copy of the one Authentik serves (authentik-assets/), so the two can't drift apart."""
    logo = REPO / "broker" / "static" / "countdown-pill.svg"

    assert logo.read_bytes() == (REPO / "authentik-assets" / "countdown-pill.svg").read_bytes()
    assert 'src="/static/countdown-pill.svg" alt="Countdown"' in client.get("/pair").get_data(as_text=True)


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


def test_pair_code_is_single_use(app, client, register_device):
    device_id, _secret = register_device()
    code = _issue(app, device_id)

    first = client.post("/pair", data={"code": code})
    assert first.status_code == 302

    second = client.post("/pair", data={"code": code})
    assert b"invalid or has expired" in second.data


def test_pair_accepts_lowercase_code(app, client, register_device):
    device_id, _secret = register_device()
    code = _issue(app, device_id)

    response = client.post("/pair", data={"code": code.lower()})
    assert response.status_code == 302


def _prefilled_code(response):
    match = re.search(rb'name="code"[^>]*value="([^"]*)"', response.data, re.DOTALL)
    assert match is not None
    return match[1].decode()


def test_pair_fills_in_a_code_given_in_the_link(client):
    response = client.get("/pair?code=abc234")
    assert response.status_code == 200
    assert _prefilled_code(response) == "ABC234"


@pytest.mark.parametrize("code", ["", "a" * 64, "ABC12", "ABC2345", "ABCD0O", "<b>12"])
def test_pair_fills_in_nothing_that_isnt_a_code(client, code):
    assert _prefilled_code(client.get("/pair", query_string={"code": code})) == ""


def test_pair_takes_the_code_only_from_code_not_t(client):
    assert _prefilled_code(client.get("/pair?t=ABC234")) == ""


def test_pair_sends_the_code_to_pair_itself_so_a_wrong_one_isnt_sent_again(client):
    response = client.get("/pair?code=ABC234")
    assert b'action="/pair"' in response.data


def test_a_code_from_the_link_pairs_like_a_typed_one(app, client, register_device):
    device_id, _secret = register_device()
    code = _issue(app, device_id)

    page = client.get(f"/pair?code={code.lower()}")
    response = client.post("/pair", data={"code": _prefilled_code(page)})

    assert response.status_code == 302
    with app.app_context():
        assert db.session.get_one(Device, device_id).owner == "user-1"


def test_wrong_codes_lock_out_the_ip_even_for_the_right_code(app, client, register_device):
    device_id, _secret = register_device()
    code = _issue(app, device_id)

    responses = [client.post("/pair", data={"code": "NOPE22"}) for _ in range(10)]
    locked = client.post("/pair", data={"code": code})

    assert [r.status_code for r in responses] == [200] * 9 + [429]
    assert locked.status_code == 429
    assert locked.headers["Retry-After"] == "60"
    with app.app_context():
        assert db.session.get_one(Device, device_id).owner is None


def test_device_config_requires_pairing(client):
    response = client.get("/device")
    assert response.status_code == 302
    assert response.headers["Location"] == "/pair"


def test_device_config_get_renders_settings(paired_client):
    client, _device_id, _ = paired_client
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
        assert db.session.get_one(DeviceConfig, device_id).weather_location == ""


def test_device_config_keeps_a_saved_weather_location_over_a_suggestion(paired_client):
    client, _, _ = paired_client
    client.post("/device", data={"interval": "15", "weather_location": "Leeds"})
    with patch("broker.services.geoip.suggest_weather_location", return_value="London") as suggest:
        response = client.get("/device")
    suggest.assert_not_called()
    assert b'name="weather_location" value="Leeds"' in response.data
    assert b"Suggested from your location" not in response.data


def test_device_config_has_the_guided_tour(paired_client):
    """The tour's script, driver.js and the sample screen it draws on are all served from here, not a CDN."""
    client, _, _ = paired_client
    html = client.get("/device").get_data(as_text=True)
    assert 'src="/static/vendor/driver.js/driver.js.iife.js"' in html
    assert 'href="/static/vendor/driver.js/driver.css"' in html
    assert 'src="/static/config_tour.js" data-picture="/static/display/tour.png"' in html
    assert 'id="tourStart"' in html
    assert client.get("/static/display/tour.png").status_code == 200
    # Each section is named for the tour, in the order the page shows them.
    sections = re.findall(
        r'<div class="card" data-tour="([a-z_]+)">\s*<div class="card-header">\s*<h2 class="card-title">([^<]+)', html
    )
    assert [name for name, _ in sections] == [
        "general",
        "style",
        "weather",
        "notice_board",
        "departures",
        "music",
        "developer",
        "energy",
        "screen",
    ]
    assert [title for _, title in sections] == re.findall(r'<h2 class="card-title">([^<]+)</h2>', html)


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
    assert titles[:4] == ["General", "Style", "Weather", "Notice board"]
    general, style, weather, notice_board = re.split(r'<h2 class="card-title">', html)[1:5]
    assert 'id="interval"' not in html
    assert 'name="style"' in style
    assert 'name="orientation"' in general
    assert 'name="clock_format"' in general
    assert 'id="weather_location"' in weather
    assert 'id="postcode"' in notice_board


def test_device_config_shows_vertical_even_when_another_orientation_is_stored(app, paired_client):
    client, device_id, _ = paired_client
    with app.app_context():
        db.session.get_one(DeviceConfig, device_id).orientation = "horizontal"
        db.session.commit()
    html = client.get("/device").get_data(as_text=True)
    assert re.search(r'id="orientation_vertical"[^>]*checked', html)
    assert '<input type="hidden" name="orientation" value="vertical">' in html


@pytest.mark.parametrize("stored", ["horizontal", "vertical"])
def test_device_config_lets_an_hd_screen_pick_its_orientation(app, paired_client, stored):
    """An HD screen's page shows the stored orientation, editable, with no hidden input overriding it."""
    client, device_id, _ = paired_client
    _make_hd_screen(app, device_id)
    with app.app_context():
        db.session.get_one(DeviceConfig, device_id).orientation = stored
        db.session.commit()
    html = client.get("/device").get_data(as_text=True)
    assert '<fieldset class="form-group" disabled>' not in html
    assert re.findall(r'id="orientation_(\w+)"[^>]*checked', html) == [stored]
    assert '<input type="hidden" name="orientation"' not in html


def test_device_config_post_saves_an_hd_screens_orientation(app, paired_client):
    client, device_id, _ = paired_client
    _make_hd_screen(app, device_id, 1404, 1872)
    response = client.post("/device", data={"interval": "15", "orientation": "horizontal"})
    assert response.status_code == 302
    with app.app_context():
        assert db.session.get_one(DeviceConfig, device_id).orientation == "horizontal"
    html = client.get("/device").get_data(as_text=True)
    assert re.findall(r'id="orientation_(\w+)"[^>]*checked', html) == ["horizontal"]


def test_device_config_post_persists_orientation(app, paired_client):
    client, device_id, _ = paired_client
    response = client.post("/device", data={"interval": "15", "orientation": "horizontal"})
    assert response.status_code == 302
    with app.app_context():
        assert db.session.get_one(DeviceConfig, device_id).orientation == "horizontal"


def test_device_config_post_without_orientation_keeps_the_stored_one(app, paired_client):
    client, device_id, _ = paired_client
    with app.app_context():
        db.session.get_one(DeviceConfig, device_id).orientation = "horizontal"
        db.session.commit()
    response = client.post("/device", data={"interval": "15"})
    assert response.status_code == 302
    with app.app_context():
        assert db.session.get_one(DeviceConfig, device_id).orientation == "horizontal"


def test_device_config_post_rejects_an_unknown_orientation(app, paired_client):
    client, device_id, _ = paired_client
    response = client.post("/device", data={"interval": "15", "orientation": "diagonal"})
    assert response.status_code == 400
    assert b"Orientation must be one of horizontal, vertical." in response.data
    with app.app_context():
        assert db.session.get_one(DeviceConfig, device_id).orientation == "vertical"


def test_device_config_shows_both_clock_formats_and_defaults_to_24_hour(paired_client):
    client, _, _ = paired_client
    html = client.get("/device").get_data(as_text=True)
    assert re.findall(r'name="clock_format" value="([^"]+)"', html) == ["24h", "12h"]
    assert re.search(r'id="clock_format_24h"[^>]*checked', html)
    assert not re.search(r'id="clock_format_12h"[^>]*checked', html)
    assert "24-hour (13:00)" in html
    assert "12-hour (1:00 PM)" in html


@pytest.mark.parametrize("clock_format", ["12h", "24h"])
def test_device_config_post_persists_and_shows_the_clock_format(app, paired_client, clock_format):
    client, device_id, _ = paired_client
    with app.app_context():
        db.session.get_one(DeviceConfig, device_id).clock_format = "24h" if clock_format == "12h" else "12h"
        db.session.commit()

    response = client.post("/device", data={"clock_format": clock_format})

    assert response.status_code == 302
    with app.app_context():
        assert db.session.get_one(DeviceConfig, device_id).clock_format == clock_format
    html = client.get(response.headers["Location"]).get_data(as_text=True)
    assert re.search(rf'id="clock_format_{clock_format}"[^>]*checked', html)
    assert len(re.findall(r'name="clock_format"[^>]*checked', html)) == 1


def test_device_config_post_without_clock_format_keeps_the_stored_one(app, paired_client):
    client, device_id, _ = paired_client
    with app.app_context():
        db.session.get_one(DeviceConfig, device_id).clock_format = "12h"
        db.session.commit()

    assert client.post("/device", data={"interval": "20"}).status_code == 302

    with app.app_context():
        config = db.session.get_one(DeviceConfig, device_id)
        assert config.clock_format == "12h"


@pytest.mark.parametrize("clock_format", ["", "invalid", "12", "24"])
def test_device_config_post_rejects_invalid_clock_formats_without_saving_anything(app, paired_client, clock_format):
    client, device_id, _ = paired_client
    with app.app_context():
        db.session.get_one(DeviceConfig, device_id).clock_format = "12h"
        db.session.commit()

    response = client.post(
        "/device",
        data={
            "clock_format": clock_format,
            "interval": "20",
            "weather_location": "London",
            "spotify_enabled": "on",
            "style": "card",
            "glowmarkt_username": "new-account",
            "glowmarkt_password": "new-password",
        },
    )

    assert response.status_code == 400
    assert b"The clock format must be one of 24h, 12h." in response.data
    assert b"Nothing was saved." in response.data
    with app.app_context():
        config = db.session.get_one(DeviceConfig, device_id)
        assert (config.clock_format, config.weather_location, config.spotify_enabled) == ("12h", "", False)
        assert config.style == "airy"
        assert db.session.get(GlowmarktCredentials, device_id) is None


def test_device_config_does_not_save_clock_format_when_another_field_is_invalid(app, paired_client):
    client, device_id, _ = paired_client

    response = client.post("/device", data={"clock_format": "12h", "postcode": "invalid"})

    assert response.status_code == 400
    assert b"Nothing was saved." in response.data
    with app.app_context():
        assert db.session.get_one(DeviceConfig, device_id).clock_format == "24h"


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
        config = db.session.get_one(DeviceConfig, device_id)
        assert config.weather_location == "London"
        assert config.spotify_enabled is True
        assert config.tfl_stop_ids == ["940GZZLUEUS", "490000173F"]


def test_device_config_page_has_no_refresh_interval_to_set(paired_client):
    client, _, _ = paired_client

    html = client.get("/device").get_data(as_text=True)

    assert 'name="interval"' not in html
    assert "Refresh interval" not in html


def test_device_config_shows_resolved_stop_names(paired_client):
    client, _device_id, _ = paired_client
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

    html = response.get_data(as_text=True)
    assert "Euston" in html
    assert "Victoria" in html
    # The card keeps the id to save the order with, but doesn't show it.
    assert 'data-id="940GZZLUEUS"' in html
    assert "NaPTAN" not in html
    assert "<code>940GZZLUEUS</code>" not in html


def test_device_config_shows_a_norwegian_platform_without_its_id(paired_client):
    client, _device_id, _ = paired_client
    client.post("/device", data={"stops_order": "NSR:Quay:11145", "interval": "15"})

    platform = {
        "id": "NSR:Quay:11145",
        "source": "entur",
        "name": "Majorstuen",
        "mode": "metro",
        "letter": "",
        "lines": ["1", "3"],
        "indent": True,
        "subtitle": "Platform 2 • 1, 3",
    }
    with patch("broker.services.stops.entur.resolve_stops", return_value={"NSR:Quay:11145": platform}):
        html = client.get("/device").get_data(as_text=True)

    assert "Platform 2" in html
    assert 'data-id="NSR:Quay:11145"' in html
    assert "<code>NSR:Quay:11145</code>" not in html


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
        creds = db.session.get_one(GlowmarktCredentials, device_id)
        assert creds.username == "someone@example.com"
        assert creds.password_encrypted not in (None, "hunter2")


def test_device_config_get_never_shows_saved_password(paired_client):
    client, _device_id, _ = paired_client
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
        creds = db.session.get_one(GlowmarktCredentials, device_id)
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


def test_pairing_again_after_an_unpair_restores_access(app, paired_client, cli):
    client, device_id, _secret = paired_client
    cli("unpair", device_id)  # which issues it a code to show
    with app.app_context():
        code = pairing.live_code(device_id)

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
    client, device_id, _secret = paired_client
    code = _issue(app, device_id)
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
        config = db.session.get_one(DeviceConfig, device_id)
        assert (config.postcode, config.language) == ("0150", "nb")


def test_device_config_post_rejects_an_unknown_language(app, paired_client):
    client, device_id, _ = paired_client
    response = client.post("/device", data={"interval": "15", "language": "sv"})
    assert response.status_code == 400
    assert b"language must be one of en, nb" in response.data

    with app.app_context():
        assert db.session.get_one(DeviceConfig, device_id).language == "en"


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
        assert db.session.get_one(DeviceConfig, device_id).postcode == "SW1A 1AA"


def test_device_config_post_rejects_invalid_postcode_and_saves_nothing(app, paired_client):
    client, device_id, _ = paired_client
    client.post("/device", data={"interval": "20", "postcode": "SW1A 1AA", "weather_location": "London"})

    response = client.post(
        "/device", data={"interval": "30", "postcode": "not a postcode", "weather_location": "Leeds"}
    )
    assert response.status_code == 400
    assert b"valid UK or Norwegian postcode" in response.data

    with app.app_context():
        config = db.session.get_one(DeviceConfig, device_id)
        assert (config.postcode, config.weather_location) == ("SW1A 1AA", "London")


def test_device_config_post_rejects_weather_location_open_meteo_cant_find(app, paired_client, weather_location_lookup):
    client, device_id, _ = paired_client
    client.post("/device", data={"interval": "20", "weather_location": "London"})

    weather_location_lookup.side_effect, weather_location_lookup.return_value = None, []
    response = client.post("/device", data={"interval": "30", "weather_location": "SE17 2PX"})

    assert response.status_code == 400
    assert b"find &#34;SE17 2PX&#34;" in response.data
    assert b"Nothing was saved" in response.data
    weather_location_lookup.assert_called_with("SE17 2PX", count=1)
    with app.app_context():
        config = db.session.get_one(DeviceConfig, device_id)
        assert config.weather_location == "London"


def test_device_config_post_refuses_an_unverified_weather_location_when_open_meteo_is_unreachable(
    app, paired_client, weather_location_lookup
):
    client, device_id, _ = paired_client
    weather_location_lookup.side_effect = weather.GeocodingUnavailableError

    response = client.post("/device", data={"interval": "15", "weather_location": "London"})

    assert response.status_code == 400
    assert b"try again in a minute" in response.data
    with app.app_context():
        assert db.session.get_one(DeviceConfig, device_id).weather_location == ""


def test_device_config_post_blank_weather_location_skips_the_lookup(paired_client, weather_location_lookup):
    client, _, _ = paired_client
    assert client.post("/device", data={"interval": "15", "weather_location": "  "}).status_code == 302
    weather_location_lookup.assert_not_called()


def test_device_config_post_reports_every_invalid_field_at_once(paired_client, weather_location_lookup):
    client, _, _ = paired_client
    weather_location_lookup.side_effect, weather_location_lookup.return_value = None, []

    response = client.post("/device", data={"interval": "15", "weather_location": "Nowhere", "postcode": "nope"})

    assert response.status_code == 400
    assert b"find &#34;Nowhere&#34;" in response.data
    assert b"valid UK or Norwegian postcode" in response.data


def _wrong_codes(client, times, ip="203.0.113.7"):
    """Post `times` wrong codes as if from `ip` (as Caddy forwards it) and return the last response."""
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


def test_a_locked_out_ip_is_refused_even_with_the_right_code(app, client, register_device):
    device_id, _secret = register_device()
    code = _issue(app, device_id)
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


def test_unlink_screen_clears_the_display_secret(app, client, paired_client):
    client, device_id, secret = paired_client
    frame_headers = {"Authorization": f"Bearer {secret}"}
    assert "Unlink screen" in client.get("/device").get_data(as_text=True)
    assert client.post("/api/frame", headers=frame_headers).status_code == 404  # authenticated, no frame yet

    response = client.post("/device/display/unlink")

    assert response.status_code == 302
    with app.app_context():
        assert db.session.get_one(Device, device_id).display_secret_hash is None
    assert client.post("/api/frame", headers=frame_headers).status_code == 401
    assert "Unlink screen" not in client.get("/device").get_data(as_text=True)


def test_unlink_screen_requires_pairing(client):
    response = client.post("/device/display/unlink")
    assert response.status_code == 302
    assert response.headers["Location"] == "/pair"


def test_device_config_shows_one_style_picker_with_the_stored_style_selected(app, paired_client):
    client, device_id, _ = paired_client
    html = client.get("/device").get_data(as_text=True)
    assert re.findall(r'name="style" value="(\w+)"', html) == ["airy", "hero", "card"]
    assert re.findall(r'name="style" value="(\w+)" checked', html) == ["airy"]  # the default
    # One style for every panel: no picker per panel.
    assert not re.search(r'name="(tfl|spotify|notices)_style"', html)
    with app.app_context():
        db.session.get_one(DeviceConfig, device_id).style = "card"
        db.session.commit()
    html = client.get("/device").get_data(as_text=True)
    assert re.findall(r'name="style" value="(\w+)" checked', html) == ["card"]


def test_device_config_shows_a_picture_of_every_style(app, paired_client):
    client, _, _ = paired_client
    html = client.get("/device").get_data(as_text=True)
    pictures = re.findall(r'<img src="(/static/styles/\w+\.png)"', html)
    assert pictures == [f"/static/styles/{style}.png" for style in ("airy", "hero", "card")]
    for picture in pictures:
        response = client.get(picture)
        assert response.status_code == 200
        assert response.mimetype == "image/png"


def test_device_config_post_persists_the_style(app, paired_client):
    client, device_id, _ = paired_client
    response = client.post("/device", data={"interval": "15", "style": "card"})
    assert response.status_code == 302
    with app.app_context():
        assert db.session.get_one(DeviceConfig, device_id).style == "card"


def test_device_config_post_without_a_style_keeps_the_stored_one(app, paired_client):
    client, device_id, _ = paired_client
    with app.app_context():
        db.session.get_one(DeviceConfig, device_id).style = "card"
        db.session.commit()
    client.post("/device", data={"interval": "15"})
    with app.app_context():
        assert db.session.get_one(DeviceConfig, device_id).style == "card"


@pytest.mark.parametrize("style", ["fancy", "grouped", "classic", "boxed"])
def test_device_config_post_rejects_an_unknown_or_old_style(app, paired_client, style):
    client, device_id, _ = paired_client
    response = client.post("/device", data={"interval": "15", "style": style})
    assert response.status_code == 400
    assert b"The style must be one of airy, hero, card." in response.data
    with app.app_context():
        assert db.session.get_one(DeviceConfig, device_id).style == "airy"


# --- the weather design, which only an HD screen has ------------------------------------------------------


def _make_hd_screen(app, device_id, width=1872, height=1404):
    """Give the device the HD screen's size, as registering one would."""
    with app.app_context():
        config = db.session.get_one(DeviceConfig, device_id)
        config.width, config.height = width, height
        db.session.commit()


def test_device_config_shows_no_weather_design_picker_for_a_small_screen(paired_client):
    client, _, _ = paired_client
    html = client.get("/device").get_data(as_text=True)
    assert 'name="weather_design"' not in html
    assert "/static/weather/" not in html


@pytest.mark.parametrize(("width", "height"), [(1872, 1404), (1404, 1872)])
def test_device_config_shows_the_weather_design_picker_for_an_hd_screen(app, paired_client, width, height):
    """Whichever way round the screen reported its size: the picker sits under the style's, with a picture of each."""
    client, device_id, _ = paired_client
    _make_hd_screen(app, device_id, width, height)
    html = client.get("/device").get_data(as_text=True)
    designs = ["columns", "timeline", "overview", "poster"]
    assert re.findall(r'name="weather_design" value="(\w+)"', html) == designs
    assert re.findall(r'name="weather_design" value="(\w+)" checked', html) == ["columns"]  # the default
    assert "How your HD screen draws the weather, which spans its whole width." in html
    style = re.split(r'<h2 class="card-title">', html)[2]
    assert 'name="style"' in style
    assert 'name="weather_design"' in style
    pictures = re.findall(r'<img src="(/static/weather/\w+\.png)" alt="" width="468" height="230"', html)
    assert pictures == [f"/static/weather/{design}.png" for design in designs]
    for picture in pictures:
        response = client.get(picture)
        assert response.status_code == 200
        assert response.mimetype == "image/png"


def test_device_config_shows_the_stored_weather_design_selected(app, paired_client):
    client, device_id, _ = paired_client
    _make_hd_screen(app, device_id)
    with app.app_context():
        db.session.get_one(DeviceConfig, device_id).weather_design = "poster"
        db.session.commit()
    html = client.get("/device").get_data(as_text=True)
    assert re.findall(r'name="weather_design" value="(\w+)" checked', html) == ["poster"]


def test_device_config_post_persists_the_weather_design(app, paired_client):
    client, device_id, _ = paired_client
    _make_hd_screen(app, device_id)
    response = client.post("/device", data={"interval": "15", "weather_design": "timeline"})
    assert response.status_code == 302
    with app.app_context():
        assert db.session.get_one(DeviceConfig, device_id).weather_design == "timeline"


def test_device_config_post_without_a_weather_design_keeps_the_stored_one(app, paired_client):
    """A small screen's page has no picker, so its saves don't post one; what's stored stays."""
    client, device_id, _ = paired_client
    with app.app_context():
        db.session.get_one(DeviceConfig, device_id).weather_design = "overview"
        db.session.commit()
    response = client.post("/device", data={"interval": "15", "style": "card"})
    assert response.status_code == 302
    with app.app_context():
        assert db.session.get_one(DeviceConfig, device_id).weather_design == "overview"


@pytest.mark.parametrize("design", ["fancy", "tabs", "Columns", ""])
def test_device_config_post_rejects_an_unknown_weather_design(app, paired_client, design):
    client, device_id, _ = paired_client
    _make_hd_screen(app, device_id)
    response = client.post("/device", data={"interval": "15", "weather_design": design, "style": "card"})
    assert response.status_code == 400
    assert b"The weather design must be one of columns, timeline, overview, poster." in response.data
    assert b"Nothing was saved." in response.data
    with app.app_context():
        config = db.session.get_one(DeviceConfig, device_id)
        assert (config.weather_design, config.style) == ("columns", "airy")


LAYOUTS = ["classic", "masthead", "column", "lockscreen", "timeline", "board", "pages"]
LANDSCAPE_LAYOUTS = ["sidebar", "board", "column", "focus", "newspaper", "pages"]


def _hang_landscape(app, device_id):
    with app.app_context():
        db.session.get_one(DeviceConfig, device_id).orientation = "horizontal"
        db.session.commit()


def test_device_config_shows_no_layout_picker_for_a_small_screen(paired_client):
    client, _, _ = paired_client
    html = client.get("/device").get_data(as_text=True)
    assert 'name="layout"' not in html
    assert "/static/layouts/" not in html


def test_device_config_shows_no_layout_picker_for_a_small_screen_hung_landscape(app, paired_client):
    """The 7.5" screens have one layout either way round."""
    client, device_id, _ = paired_client
    _hang_landscape(app, device_id)
    html = client.get("/device").get_data(as_text=True)
    assert 'name="layout"' not in html
    assert "/static/layouts/" not in html


@pytest.mark.parametrize(("width", "height"), [(1872, 1404), (1404, 1872)])
def test_device_config_shows_the_layout_picker_for_an_hd_landscape_screen(app, paired_client, width, height):
    """Whichever way round the screen reported its size: sidebar first and picked, with a landscape picture of each."""
    client, device_id, _ = paired_client
    _make_hd_screen(app, device_id, width, height)
    _hang_landscape(app, device_id)
    html = client.get("/device").get_data(as_text=True)
    assert re.findall(r'name="layout" value="(\w+)"', html) == LANDSCAPE_LAYOUTS
    assert re.findall(r'name="layout" value="(\w+)" checked', html) == ["sidebar"]  # the default, stored as null
    assert "How your HD screen arranges its panels, hung landscape." in html
    assert "hung portrait" not in html
    for name in ("Sidebar", "Departure board", "Weather column", "Focus", "Newspaper", "Two pages"):
        assert f'<span class="style-name">{name}</span>' in html
    for name in ("Classic", "Route board", "Lock screen"):
        assert f'<span class="style-name">{name}</span>' not in html
    pictures = re.findall(r'<img src="(/static/layouts/[\w-]+\.png)" alt="" width="468" height="351"', html)
    assert pictures == [f"/static/layouts/landscape-{layout}.png" for layout in LANDSCAPE_LAYOUTS]
    for picture in pictures:
        response = client.get(picture)
        assert response.status_code == 200
        assert response.mimetype == "image/png"


@pytest.mark.parametrize(
    ("prefix", "size", "layouts"),
    [("", (351, 468), LAYOUTS), ("landscape-", (468, 351), LANDSCAPE_LAYOUTS)],
)
def test_the_layout_pictures_are_the_hd_screen_at_a_quarter_size(prefix, size, layouts):
    """Each is the screen the way it hangs, a quarter of 1872x1404, in grey: what the picker's <img> says it is."""
    folder = Path(__file__).parents[2] / "broker" / "static" / "layouts"
    for layout in layouts:
        header = (folder / f"{prefix}{layout}.png").read_bytes()[:26]
        # The PNG signature, then IHDR: width, height, bit depth and colour type (0, greyscale).
        assert header[:8] == b"\x89PNG\r\n\x1a\n"
        width, height = int.from_bytes(header[16:20]), int.from_bytes(header[20:24])
        assert ((width, height), header[25]) == (size, 0), layout


@pytest.mark.parametrize(("width", "height"), [(1872, 1404), (1404, 1872)])
def test_device_config_shows_the_layout_picker_for_an_hd_portrait_screen(app, paired_client, width, height):
    """Whichever way round the screen reported its size: classic first and picked, with a picture of each."""
    client, device_id, _ = paired_client
    _make_hd_screen(app, device_id, width, height)
    html = client.get("/device").get_data(as_text=True)
    assert re.findall(r'name="layout" value="(\w+)"', html) == LAYOUTS
    assert re.findall(r'name="layout" value="(\w+)" checked', html) == ["classic"]  # the default, stored as null
    assert "How your HD screen arranges its panels, hung portrait." in html
    for name in ("Classic", "Masthead", "Column", "Lock screen", "Timeline", "Route board", "Two pages"):
        assert f'<span class="style-name">{name}</span>' in html
    style = re.split(r'<h2 class="card-title">', html)[2]
    assert 'name="layout"' in style
    pictures = re.findall(r'<img src="(/static/layouts/\w+\.png)" alt="" width="351" height="468"', html)
    assert pictures == [f"/static/layouts/{layout}.png" for layout in LAYOUTS]
    for picture in pictures:
        response = client.get(picture)
        assert response.status_code == 200
        assert response.mimetype == "image/png"


def test_device_config_shows_the_stored_layout_selected(app, paired_client):
    client, device_id, _ = paired_client
    _make_hd_screen(app, device_id)
    with app.app_context():
        db.session.get_one(DeviceConfig, device_id).layout = "board"
        db.session.commit()
    html = client.get("/device").get_data(as_text=True)
    assert re.findall(r'name="layout" value="(\w+)" checked', html) == ["board"]


@pytest.mark.parametrize("layout", ["newspaper", "board"])
def test_device_config_shows_the_stored_landscape_layout_selected(app, paired_client, layout):
    client, device_id, _ = paired_client
    _make_hd_screen(app, device_id)
    _hang_landscape(app, device_id)
    with app.app_context():
        db.session.get_one(DeviceConfig, device_id).layout = layout
        db.session.commit()
    html = client.get("/device").get_data(as_text=True)
    assert re.findall(r'name="layout" value="(\w+)" checked', html) == [layout]


@pytest.mark.parametrize(
    ("landscape", "stored", "shown"),
    [
        (True, "masthead", "sidebar"),
        (True, "classic", "sidebar"),
        (False, "focus", "classic"),
        (False, "sidebar", "classic"),
    ],
)
def test_device_config_shows_the_default_for_a_layout_the_way_it_hangs_lacks(
    app, paired_client, landscape, stored, shown
):
    """A layout picked hung the other way round, which the screen draws as its default now, shows as that default."""
    client, device_id, _ = paired_client
    _make_hd_screen(app, device_id)
    if landscape:
        _hang_landscape(app, device_id)
    with app.app_context():
        db.session.get_one(DeviceConfig, device_id).layout = stored
        db.session.commit()
    html = client.get("/device").get_data(as_text=True)
    assert re.findall(r'name="layout" value="(\w+)" checked', html) == [shown]


@pytest.mark.parametrize("layout", LANDSCAPE_LAYOUTS)
def test_device_config_post_persists_the_landscape_layout(app, paired_client, layout):
    client, device_id, _ = paired_client
    _make_hd_screen(app, device_id)
    _hang_landscape(app, device_id)
    response = client.post("/device", data={"interval": "15", "layout": layout})
    assert response.status_code == 302
    with app.app_context():
        assert db.session.get_one(DeviceConfig, device_id).layout == layout


def test_device_config_post_saves_a_layout_and_the_orientation_together(app, paired_client):
    """Turning the screen and posting the other way's picker in one save: any contract layout is taken."""
    client, device_id, _ = paired_client
    _make_hd_screen(app, device_id)
    response = client.post("/device", data={"interval": "15", "orientation": "horizontal", "layout": "timeline"})
    assert response.status_code == 302
    with app.app_context():
        config = db.session.get_one(DeviceConfig, device_id)
        assert (config.orientation, config.layout) == ("horizontal", "timeline")


@pytest.mark.parametrize("layout", LAYOUTS)
def test_device_config_post_persists_the_layout(app, paired_client, layout):
    client, device_id, _ = paired_client
    _make_hd_screen(app, device_id)
    response = client.post("/device", data={"interval": "15", "layout": layout})
    assert response.status_code == 302
    with app.app_context():
        assert db.session.get_one(DeviceConfig, device_id).layout == layout


def test_device_config_post_with_an_empty_layout_stores_the_default(app, paired_client):
    client, device_id, _ = paired_client
    _make_hd_screen(app, device_id)
    with app.app_context():
        db.session.get_one(DeviceConfig, device_id).layout = "pages"
        db.session.commit()
    response = client.post("/device", data={"interval": "15", "layout": ""})
    assert response.status_code == 302
    with app.app_context():
        assert db.session.get_one(DeviceConfig, device_id).layout is None


def test_device_config_post_without_a_layout_keeps_the_stored_one(app, paired_client):
    """A page without the picker doesn't post one; what's stored stays."""
    client, device_id, _ = paired_client
    with app.app_context():
        db.session.get_one(DeviceConfig, device_id).layout = "timeline"
        db.session.commit()
    response = client.post("/device", data={"interval": "15", "style": "card"})
    assert response.status_code == 302
    with app.app_context():
        assert db.session.get_one(DeviceConfig, device_id).layout == "timeline"


@pytest.mark.parametrize("layout", ["fancy", "Classic", "lock screen", "default"])
def test_device_config_post_rejects_an_unknown_layout(app, paired_client, layout):
    client, device_id, _ = paired_client
    _make_hd_screen(app, device_id)
    response = client.post("/device", data={"interval": "15", "layout": layout, "style": "card"})
    assert response.status_code == 400
    assert (
        b"The layout must be one of classic, masthead, column, lockscreen, timeline, board, pages, sidebar, focus, "
        b"newspaper." in response.data
    )
    assert b"Nothing was saved." in response.data
    with app.app_context():
        config = db.session.get_one(DeviceConfig, device_id)
        assert (config.layout, config.style) == (None, "airy")


# --- the public page about the display ------------------------------------------------------------------


def test_about_page_needs_no_login(anonymous_client):
    response = anonymous_client.get("/about")
    assert response.status_code == 200
    assert b"<title>Countdown Display</title>" in response.data


def test_about_page_pictures_are_all_there(anonymous_client):
    html = anonymous_client.get("/about").get_data(as_text=True)
    pictures = set(re.findall(r'"(/static/display/\w+\.(?:png|jpg))"', html))
    # The weather tabs' pictures are named by the page's script.
    pictures |= {
        f"/static/display/weather_{tab}_{lang}.png"
        for tab in ("now", "tonight", "tomorrow", "weekend")
        for lang in ("en", "nb")
    }
    assert len(pictures) > 20
    for picture in sorted(pictures):
        response = anonymous_client.get(picture)
        assert response.status_code == 200, picture
        assert response.mimetype in {"image/png", "image/jpeg"}


def test_device_config_post_switches_the_developer_panel_and_stores_the_github_token_encrypted(app, paired_client):
    client, device_id, _ = paired_client

    client.post("/device", data={"interval": "15", "developer_enabled": "on", "github_token": " github_pat_abc "})

    with app.app_context():
        assert db.session.get_one(DeviceConfig, device_id).developer_enabled
        token = db.session.get_one(GitHubToken, device_id)
        assert token.token_encrypted != "github_pat_abc"
        assert token.token == "github_pat_abc"


def test_device_config_post_blank_github_token_keeps_the_saved_one(app, paired_client):
    client, device_id, _ = paired_client
    client.post("/device", data={"interval": "15", "github_token": "github_pat_abc"})

    client.post("/device", data={"interval": "15"})

    with app.app_context():
        assert not db.session.get_one(DeviceConfig, device_id).developer_enabled
        assert db.session.get_one(GitHubToken, device_id).token == "github_pat_abc"


def test_device_config_get_never_shows_the_github_token(paired_client):
    client, _device_id, _ = paired_client
    client.post("/device", data={"interval": "15", "github_token": "github_pat_abc"})

    response = client.get("/device")

    assert b"github_pat_abc" not in response.data
    assert b"GitHub connected" in response.data


def test_github_disconnect_forgets_the_token(app, paired_client):
    client, device_id, _ = paired_client
    client.post("/device", data={"interval": "15", "github_token": "github_pat_abc"})

    response = client.post("/device/github/disconnect")

    assert response.status_code == 302
    with app.app_context():
        assert db.session.get(GitHubToken, device_id) is None


def _subscriptions(fake_redis):
    return {key.removeprefix("v1:sub:"): members for key, members in fake_redis.sets.items()}


def test_saving_the_config_publishes_subscriptions_to_redis(paired_client, fake_redis, weather_location_lookup):
    client, device_id, _ = paired_client
    weather_location_lookup.side_effect = lambda name, **_: [_place(name, 2643743, country_code="gb")]
    client.post(
        "/device",
        data={
            "stops_order": "940GZZLUEUS,NSR:StopPlace:58366",
            "interval": "20",
            "weather_location": "London",
            "spotify_enabled": "on",
        },
    )

    assert _subscriptions(fake_redis) == {
        "tfl": {"940GZZLUEUS"},
        "entur": {"NSR:StopPlace:58366"},
        "weather": {"london-gb-2643743"},
        "spotify": {device_id},
    }
    # The providers are nudged, and the screen gets a render job (tests/services/test_config_store.py).
    assert fake_redis.published == [("v1:sub:changed", "")]
    assert device_id in fake_redis.jobs


def _give_another_screen_weather(app, register_device, location, place=None):
    """Register another screen (no one's) and give its config `location`, picked as `place` if one is given."""
    device_id, _ = register_device("another-device-secret-value-0123")
    with app.app_context():
        config = db.session.get_one(DeviceConfig, device_id)
        weather_place.store(config, place)
        config.weather_location = location
        db.session.commit()
    return device_id


def test_screens_that_picked_the_same_place_subscribe_it_once_however_they_typed_it(
    app, paired_client, register_device, fake_redis, weather_location_lookup
):
    client, _, _ = paired_client
    london = _place("London", 2643743, country_code="gb")
    weather_location_lookup.side_effect, weather_location_lookup.return_value = None, [london]
    _give_another_screen_weather(app, register_device, "london", london)

    client.post("/device", data={"interval": "20", "weather_location": "London"})

    assert _subscriptions(fake_redis)["weather"] == {"london-gb-2643743"}


def test_a_location_with_no_place_picked_is_still_subscribed_by_its_slug(
    app, paired_client, register_device, fake_redis, weather_location_lookup
):
    """Saved before places were picked, and not backfilled yet: its screen keeps its weather."""
    client, _, _ = paired_client
    weather_location_lookup.side_effect = lambda name, **_: [_place(name, 2643743, country_code="gb")]
    _give_another_screen_weather(app, register_device, " St. Albans ")

    client.post("/device", data={"interval": "20", "weather_location": "London"})

    assert _subscriptions(fake_redis)["weather"] == {"london-gb-2643743", "st-albans"}


def test_saving_the_config_subscribes_a_device_with_the_developer_panel_on(paired_client, fake_redis):
    client, device_id, _ = paired_client
    client.post("/device", data={"developer_enabled": "on"})

    assert _subscriptions(fake_redis) == {"developer": {device_id}}


def test_saving_the_config_subscribes_the_notice_boards_postcode_in_canonical_form(paired_client, fake_redis):
    client, _, _ = paired_client
    client.post("/device", data={"interval": "15", "postcode": "sw1a1aa"})

    assert _subscriptions(fake_redis) == {"postcode": {"SW1A 1AA"}}


def test_saving_the_config_subscribes_a_device_with_a_glowmarkt_password_to_glow(paired_client, fake_redis):
    client, device_id, _ = paired_client
    client.post(
        "/device",
        data={"interval": "15", "glowmarkt_username": "someone@example.com", "glowmarkt_password": "hunter2"},
    )

    assert _subscriptions(fake_redis) == {"glow": {device_id}}


def test_saving_the_config_drops_what_the_device_no_longer_wants(paired_client, fake_redis):
    client, _, _ = paired_client
    client.post("/device", data={"stops_order": "940GZZLUEUS", "interval": "20", "spotify_enabled": "on"})
    client.post("/device", data={"interval": "20"})

    assert _subscriptions(fake_redis) == {}
    assert [channel for channel, _ in fake_redis.published].count("v1:sub:changed") == 2


def test_a_rejected_config_save_publishes_nothing(paired_client, fake_redis):
    client, _, _ = paired_client
    response = client.post("/device", data={"interval": "20", "postcode": "not a postcode"})

    assert response.status_code == 400
    assert fake_redis.sets == {}
    assert fake_redis.published == []


def test_the_config_is_saved_even_when_redis_is_down(app, paired_client, fake_redis):
    client, device_id, _ = paired_client
    fake_redis.fail = True

    response = client.post("/device", data={"interval": "30", "weather_location": "London"})

    assert response.status_code == 302
    with app.app_context():
        assert db.session.get_one(DeviceConfig, device_id).weather_location == "London"


def test_a_failed_subscription_publish_logs_the_errors_type_not_its_text(
    app, paired_client, fake_redis, weather_location_lookup
):
    """A failed pipeline's text quotes its commands, SADD v1:sub:postcode <postcode> among them: not logged."""
    client, _, _ = paired_client
    weather_location_lookup.side_effect = lambda name, **_: [_place(name, 2643743, country_code="gb")]
    failing = MagicMock()
    failing.__enter__.return_value.execute.side_effect = redis.ResponseError(
        "Command # 9 (SADD v1:sub:postcode SE17 3LL) of pipeline caused error: OOM command not allowed"
    )
    fake_redis.pipeline = lambda **_: failing

    with patch.object(app, "logger") as logger:
        response = client.post("/device", data={"postcode": "SE17 3LL", "weather_location": "London"})

    assert response.status_code == 302
    [call] = [c for c in logger.method_calls if c.args and "subscriptions" in c.args[0]]
    assert call.args == ("Couldn't publish subscriptions to Redis (%s)", "ResponseError")
    assert not call.kwargs.get("exc_info")


# --- The weather place picker ---------------------------------------------------------------------------------------

LONDON_ON = _place("London", 6058560, admin1="Ontario", country="Canada", country_code="CA")


def _weather_columns(app, device_id):
    with app.app_context():
        config = db.session.get_one(DeviceConfig, device_id)
        return (
            config.weather_location,
            config.weather_place_id,
            config.weather_place_label,
            config.weather_country_code,
        )


def test_device_config_post_stores_the_picked_place_as_open_meteo_has_it(
    app, paired_client, weather_place_lookup, weather_location_lookup
):
    client, device_id, _ = paired_client
    weather_place_lookup.return_value = LONDON_ON

    # Only the id is trusted; Open-Meteo is asked for the rest.
    response = client.post(
        "/device",
        data={"interval": "20", "weather_place_id": "6058560", "weather_location": "London, Ontario, Canada"},
    )

    assert response.status_code == 302
    weather_place_lookup.assert_called_once_with(6058560)
    weather_location_lookup.assert_not_called()
    assert _weather_columns(app, device_id) == ("London", 6058560, "London, Ontario, Canada", "CA")
    with app.app_context():
        config = db.session.get_one(DeviceConfig, device_id)
        assert (config.weather_lat, config.weather_lon) == (1.0, 2.0)


def test_device_config_post_refuses_a_picked_id_open_meteo_doesnt_have(app, paired_client):
    client, device_id, _ = paired_client
    response = client.post(
        "/device", data={"interval": "30", "weather_place_id": "999999999", "weather_location": "Atlantis"}
    )

    assert response.status_code == 400
    assert b"find &#34;Atlantis&#34;" in response.data
    assert b"Nothing was saved" in response.data
    assert _weather_columns(app, device_id) == ("", None, None, None)


def test_device_config_post_without_a_pick_stores_open_meteos_first_match(app, paired_client, weather_location_lookup):
    client, device_id, _ = paired_client
    weather_location_lookup.side_effect, weather_location_lookup.return_value = None, [LONDON_ON]

    assert client.post("/device", data={"interval": "20", "weather_location": "london"}).status_code == 302

    weather_location_lookup.assert_called_once_with("london", count=1)
    assert _weather_columns(app, device_id) == ("London", 6058560, "London, Ontario, Canada", "CA")


def test_device_config_post_keeps_the_saved_place_without_asking_open_meteo(
    app, paired_client, weather_place_lookup, weather_location_lookup
):
    client, device_id, _ = paired_client
    weather_place_lookup.return_value = LONDON_ON
    form = {"interval": "20", "weather_place_id": "6058560", "weather_location": "London, Ontario, Canada"}
    client.post("/device", data=form)
    weather_place_lookup.reset_mock()

    assert client.post("/device", data=form | {"interval": "40"}).status_code == 302

    weather_place_lookup.assert_not_called()
    weather_location_lookup.assert_not_called()
    assert _weather_columns(app, device_id) == ("London", 6058560, "London, Ontario, Canada", "CA")


def test_device_config_post_blank_weather_location_clears_the_place(app, paired_client, weather_place_lookup):
    client, device_id, _ = paired_client
    weather_place_lookup.return_value = LONDON_ON
    client.post("/device", data={"interval": "20", "weather_place_id": "6058560", "weather_location": "London"})

    # Without JavaScript the saved id is still posted; the empty box is what counts.
    client.post("/device", data={"interval": "20", "weather_place_id": "6058560", "weather_location": ""})

    assert _weather_columns(app, device_id) == ("", None, None, None)


def test_device_config_shows_the_saved_place_and_its_id_for_the_picker(app, paired_client, weather_place_lookup):
    client, _, _ = paired_client
    weather_place_lookup.return_value = LONDON_ON
    client.post("/device", data={"interval": "20", "weather_place_id": "6058560", "weather_location": "London"})

    html = client.get("/device").get_data(as_text=True)

    assert 'name="weather_location" value="London, Ontario, Canada"' in html
    assert 'name="weather_place_id" value="6058560"' in html
    assert "Now: <span" in html
    assert "/api/places/search" in html


def test_device_config_shows_a_location_saved_before_places_with_no_id(app, paired_client):
    client, device_id, _ = paired_client
    with app.app_context():
        db.session.get_one(DeviceConfig, device_id).weather_location = "Leeds"
        db.session.commit()

    html = client.get("/device").get_data(as_text=True)

    assert 'name="weather_location" value="Leeds"' in html
    assert 'name="weather_place_id" value=""' in html
