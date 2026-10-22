"""A user can own several screens: each has its own pages, and nobody else can reach them."""

from unittest.mock import patch

import pytest

from broker.auth import user_devices
from broker.db import db
from broker.models import Device, DeviceConfig, SpotifyToken
from broker.services import pairing
from tests.conftest import USER, name_screen

FIRST_SECRET = "first-screen-secret-0123456789"
SECOND_SECRET = "second-screen-secret-0123456789"
OTHER_SECRET = "someone-elses-secret-0123456789"


def _pair(app, client, secret):
    device_id = client.post(
        "/api/devices/register", json={"secret": secret, "width": 480, "height": 800, "format": "mono-1bpp-packed"}
    ).get_json()["device_id"]
    with app.app_context():
        name_screen(device_id)
        code = pairing.issue_code(device_id)
    assert client.post("/pair", data={"code": code}).status_code == 302
    return device_id


@pytest.fixture
def two_screens(app, client):
    """Pair the signed-in user's two screens: return (first_id, second_id), the second paired most recently."""
    return _pair(app, client, FIRST_SECRET), _pair(app, client, SECOND_SECRET)


@pytest.fixture
def other_screen(app):
    stranger = app.test_client()
    stranger.environ_base["HTTP_X_AUTHENTIK_UID"] = "user-2"
    stranger.environ_base["HTTP_X_AUTHENTIK_USERNAME"] = "stranger"
    return _pair(app, stranger, OTHER_SECRET)


def _weather(app, device_id):
    with app.app_context():
        return db.session.get_one(DeviceConfig, device_id).weather_location


def _set_weather(app, device_id, location):
    with app.app_context():
        db.session.get_one(DeviceConfig, device_id).weather_location = location
        db.session.commit()


# --- which screen a page is for ---------------------------------------------------------------------


def test_plain_device_is_the_screen_paired_most_recently(app, client, two_screens):
    first, second = two_screens
    _set_weather(app, first, "Firstville")
    _set_weather(app, second, "Secondtown")

    page = client.get("/device").data.decode()

    assert "Secondtown" in page
    assert "Firstville" not in page


def test_each_screen_has_its_own_page(app, client, two_screens):
    first, second = two_screens
    _set_weather(app, first, "Firstville")
    _set_weather(app, second, "Secondtown")

    assert "Firstville" in client.get(f"/device/{first}").data.decode()
    assert "Secondtown" in client.get(f"/device/{second}").data.decode()


def test_the_forms_on_a_page_point_at_that_screen(client, two_screens):
    first, _ = two_screens

    page = client.get(f"/device/{first}").data.decode()

    assert f'action="/device/{first}"' in page
    assert f'action="/device/{first}/spotify/disconnect"' in page
    assert f"/auth/spotify/login?device={first}" in page


def test_saving_one_screen_leaves_the_other_alone(app, client, two_screens):
    first, second = two_screens
    _set_weather(app, second, "Secondtown")

    response = client.post(f"/device/{first}", data={"weather_location": "", "clock_format": "12h", "postcode": ""})

    assert (response.status_code, response.headers["Location"]) == (302, f"/device/{first}")
    with app.app_context():
        assert db.session.get_one(DeviceConfig, first).clock_format == "12h"
        assert db.session.get_one(DeviceConfig, second).clock_format == "24h"
    assert _weather(app, second) == "Secondtown"


def test_each_screen_keeps_its_own_clock_format(app, client, two_screens, config_of):
    first, second = two_screens

    assert client.post(f"/device/{first}", data={"clock_format": "12h"}).status_code == 302

    with app.app_context():
        assert db.session.get_one(DeviceConfig, first).clock_format == "12h"
        assert db.session.get_one(DeviceConfig, second).clock_format == "24h"
    for device_id, expected in [(first, "12h"), (second, "24h")]:
        page = client.get(f"/device/{device_id}").get_data(as_text=True)
        assert f'name="clock_format" value="{expected}" checked' in page
        assert config_of(device_id)["clock_format"] == expected


def test_disconnecting_spotify_and_unlinking_act_on_the_chosen_screen(app, client, two_screens):
    first, second = two_screens
    with app.app_context():
        for device_id in (first, second):
            db.session.add(SpotifyToken(device_id=device_id, refresh_token="refresh"))
        db.session.get_one(Device, first).display_secret_hash = "a" * 64
        db.session.get_one(Device, second).display_secret_hash = "b" * 64
        db.session.commit()

    assert client.post(f"/device/{first}/spotify/disconnect").headers["Location"] == f"/device/{first}"
    assert client.post(f"/device/{first}/display/unlink").headers["Location"] == f"/device/{first}"

    with app.app_context():
        assert db.session.get(SpotifyToken, first) is None
        assert db.session.get(SpotifyToken, second) is not None
        assert db.session.get_one(Device, first).display_secret_hash is None
        assert db.session.get_one(Device, second).display_secret_hash == "b" * 64


def test_a_second_screen_is_named_on_its_own_page(app, client, two_screens):
    first, second = two_screens

    assert client.post(f"/device/{second}", data={"screen_name": "Bedroom"}).headers["Location"] == f"/device/{second}"

    with app.app_context():
        assert db.session.get_one(Device, second).device_name == "Bedroom"
        assert db.session.get_one(Device, first).device_name != "Bedroom"


# --- nobody else's screen -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "suffix"),
    [
        ("get", ""),
        ("post", ""),
        ("get", "/name"),
        ("post", "/name"),
        ("post", "/spotify/disconnect"),
        ("post", "/display/unlink"),
    ],
)
def test_someone_elses_screen_is_not_found(app, client, two_screens, other_screen, method, suffix):
    response = getattr(client, method)(f"/device/{other_screen}{suffix}", data={"slug": "mine", "clock_format": "12h"})

    assert response.status_code == 404
    with app.app_context():
        assert db.session.get_one(Device, other_screen).owner == "user-2"
        assert db.session.get_one(DeviceConfig, other_screen).clock_format == "24h"


def test_an_unknown_screen_is_not_found(client, two_screens):
    assert client.get("/device/does-not-exist").status_code == 404


def test_a_user_with_no_screens_is_sent_to_pair(client):
    assert client.get("/device").headers["Location"] == "/pair"
    assert client.get("/device/anything").status_code == 404


# --- the switcher -----------------------------------------------------------------------------------------


def test_the_switcher_lists_every_screen_and_marks_the_current_one(client, two_screens):
    first, second = two_screens

    page = client.get(f"/device/{first}").data.decode()

    assert f'<a href="/device/{first}" class="current" aria-current="page">' in page
    assert f'<a href="/device/{second}">' in page
    assert 'href="/pair" class="add">+ Add a screen' in page


def test_plain_device_marks_the_most_recent_screen_as_current(client, two_screens):
    _, second = two_screens
    assert f'<a href="/device/{second}" class="current"' in client.get("/device").data.decode()


def test_the_switcher_uses_the_screens_name(app, client, two_screens):
    first, _ = two_screens
    client.post(f"/device/{first}", data={"screen_name": "Kitchen"})

    assert ">Kitchen</a>" in client.get("/device").data.decode()


def test_no_switcher_for_a_user_without_screens(client):
    assert 'class="screens"' not in client.get("/pair").data.decode()


def test_another_users_screens_never_appear_in_the_switcher(client, two_screens, other_screen):
    assert other_screen not in client.get("/device").data.decode()


# --- linking Spotify to a chosen screen ---------------------------------------------------------------------


def _callback(client, state):
    payload = {"access_token": "access", "refresh_token": "refresh", "expires_in": 3600}
    with patch("broker.clients.spotify.requests.post") as post:
        post.return_value.status_code = 200
        post.return_value.json.return_value = payload
        post.return_value.raise_for_status = lambda: None
        return client.get(f"/auth/spotify/callback?code=abc&state={state}")


def test_spotify_links_to_the_screen_the_login_was_started_for(app, client, two_screens):
    first, second = two_screens
    state = client.get(f"/auth/spotify/login?device={first}").headers["Location"].split("state=")[1].split("&")[0]

    response = _callback(client, state)

    assert response.headers["Location"] == f"/device/{first}"
    with app.app_context():
        assert db.session.get(SpotifyToken, first) is not None
        assert db.session.get(SpotifyToken, second) is None


def test_spotify_login_without_a_device_uses_the_most_recent_screen(app, client, two_screens):
    _, second = two_screens
    state = client.get("/auth/spotify/login").headers["Location"].split("state=")[1].split("&")[0]

    assert _callback(client, state).headers["Location"] == f"/device/{second}"


def test_spotify_login_refuses_someone_elses_screen(app, client, two_screens, other_screen):
    response = client.get(f"/auth/spotify/login?device={other_screen}")

    assert response.headers["Location"] == "/pair"
    with app.app_context():
        assert db.session.get(SpotifyToken, other_screen) is None


def test_spotify_callback_refuses_a_screen_the_user_no_longer_owns(app, client, two_screens):
    first, _ = two_screens
    state = client.get(f"/auth/spotify/login?device={first}").headers["Location"].split("state=")[1].split("&")[0]
    with app.app_context():
        db.session.get_one(Device, first).owner = "user-2"
        db.session.commit()

    response = _callback(client, state)

    with app.app_context():
        assert db.session.get(SpotifyToken, first) is None
    assert response.status_code == 302
    assert response.headers["Location"] != f"/device/{first}"


def test_spotify_login_needs_a_signed_in_user(anonymous_client):
    assert anonymous_client.get("/auth/spotify/login").status_code == 401


def test_the_users_own_header_is_what_decides_ownership(app, client, two_screens):
    first, _ = two_screens
    assert USER == "user-1"
    with app.app_context():
        assert db.session.get_one(Device, first).owner == USER


def test_without_a_signed_in_user_there_are_no_screens(app):
    with app.test_request_context("/"):
        assert user_devices() == []


def test_a_screen_with_no_name_is_called_screen_and_its_id(client, two_screens):
    _, second = two_screens

    assert f">Screen {second[:6]}</a>" in client.get("/device").data.decode()
