import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from countdown_api.models import ConfigResponse
from sqlalchemy import func, select, update

from broker.db import db
from broker.models import Device, DeviceConfig, GitHubToken, GlowmarktCredentials, PairingCode
from broker.services import pairing, renderer_config


def _config(app, device_id):
    """Return the device's config as the render pool reads it, wrapped like a test client's response."""
    with app.app_context():
        body = renderer_config.build(db.session.get_one(DeviceConfig, device_id), pairing_code=None).model_dump(
            mode="json"
        )
    return SimpleNamespace(status_code=200, data=json.dumps(body).encode(), get_json=lambda: body)


def _issue(app, device_id):
    """Issue the device a pairing code, as `flask devices code` does; return it."""
    with app.app_context():
        return pairing.issue_code(device_id)


def test_the_per_screen_routes_are_gone(client, register_device):
    device_id, secret = register_device()
    headers = {"Authorization": f"Bearer {secret}"}

    assert client.get("/api/config", headers=headers).status_code == 404
    assert client.post("/api/devices/pairing-code", headers=headers).status_code == 404
    assert client.put("/api/frame", data=b"0" * 48_000, headers=headers).status_code == 405
    assert client.post("/api/renderers/claim", json={"secret": "x" * 20, "claim_token": "t"}).status_code == 404
    assert client.get("/api/orchestrator/assignments", headers=headers).status_code == 404
    assert client.post(f"/api/displays/{device_id}/renderer", json={"slug": "lisa"}).status_code == 404


def test_register_creates_device(client):
    secret = "a-very-long-device-secret-value"
    response = client.post(
        "/api/devices/register", json={"secret": secret, "width": 480, "height": 800, "format": "mono-1bpp-packed"}
    )
    assert response.status_code == 201
    assert "device_id" in response.get_json()


def test_register_rejects_short_secret(client):
    response = client.post("/api/devices/register", json={"secret": "short"})
    assert response.status_code == 400


def test_register_rejects_missing_body(client):
    response = client.post("/api/devices/register")
    assert response.status_code == 400


def test_pairing_code_uses_unambiguous_alphabet(app, register_device):
    device_id, _secret = register_device()
    code = _issue(app, device_id)
    assert len(code) == 6
    assert timedelta(minutes=10) == pairing.CODE_TTL
    for ambiguous_char in "0O1IL":
        assert ambiguous_char not in code


def test_requesting_new_pairing_code_invalidates_old_one(app, client, register_device):
    device_id, _secret = register_device()

    first = _issue(app, device_id)
    _issue(app, device_id)

    response = client.post("/pair", data={"code": first})
    assert b"invalid or has expired" in response.data


def test_the_config_returns_defaults(app, register_device):
    device_id, _secret = register_device()
    response = _config(app, device_id)
    assert response.status_code == 200
    body = response.get_json()
    assert body == {
        "device_id": device_id,
        "interval": 60,
        "clock_format": "24h",
        "language": "en",
        "timezone": "Europe/London",
        "style": "airy",
        "screen": {
            "orientation": "vertical",
            "width": 480,
            "height": 800,
            "format": "mono-1bpp-packed",
            "layout": None,
        },
        "weather": {"location": "", "key": None, "design": "columns"},
        "notice_board": {"postcode": None, "country": None, "style": "airy"},
        "tfl": {"stop_ids": [], "style": "grouped"},
        "departures": {"stops": [], "style": "grouped"},
        "spotify": {"enabled": False, "style": "airy"},
        "glowmarkt": {"enabled": False},
        "developer": {"enabled": False},
        "apple_music": {"enabled": False},
        "pairing_code": None,
    }


def test_the_config_never_sends_the_tfl_key(app, client, register_device):
    """The broker keeps TfL's key for its stop lookups; the providers have their own, and the pool reads the store."""
    device_id, _secret = register_device()
    _set_config(app, device_id, tfl_stop_ids=["940GZZLUEUS"])

    response = _config(app, device_id)

    assert response.get_json()["tfl"] == {"stop_ids": ["940GZZLUEUS"], "style": "grouped"}
    assert app.config["TFL_APP_KEY"] == "test-tfl-key"
    assert b"test-tfl-key" not in response.data


def test_the_config_sends_no_weather_api_key(app, register_device):
    """Nothing uses one: the weather provider calls Open-Meteo, which needs none, and the pool reads the store."""
    device_id, _secret = register_device()

    response = _config(app, device_id)

    assert "api_key" not in response.get_json()["weather"]


def test_the_config_sends_the_developer_switch_but_never_the_github_token(app, client, register_device):
    device_id, _secret = register_device()
    _set_config(app, device_id, developer_enabled=True)
    with app.app_context():
        token = GitHubToken(device_id=device_id)
        token.token = "github_pat_secret"
        db.session.add(token)
        db.session.commit()

    response = _config(app, device_id)

    assert response.get_json()["developer"] == {"enabled": True}
    assert b"github_pat_secret" not in response.data


def test_the_config_sends_the_key_the_weather_is_subscribed_under(app, client, register_device):
    """The renderer reads v1:weather:<key>: the picked place's, beside its name."""
    device_id, _secret = register_device()
    _set_config(
        app,
        device_id,
        weather_location="London",
        weather_place_id=2643743,
        weather_place_name="London",
        weather_place_label="London, England, United Kingdom",
        weather_country_code="GB",
        weather_lat=51.50853,
        weather_lon=-0.12574,
    )

    body = _config(app, device_id).get_json()

    assert body["weather"] == {"location": "London", "key": "london-gb-2643743", "design": "columns"}


def test_the_config_sends_the_slug_as_the_key_of_a_location_with_no_place_picked(app, client, register_device):
    """A config saved before places were picked, and not backfilled: its typed text, as it is subscribed."""
    device_id, _secret = register_device()
    _set_config(app, device_id, weather_location="St. Albans")

    body = _config(app, device_id).get_json()

    assert (body["weather"]["location"], body["weather"]["key"]) == ("St. Albans", "st-albans")


def _set_config(app, device_id, **values):
    with app.app_context():
        config = db.session.get_one(DeviceConfig, device_id)
        for key, value in values.items():
            setattr(config, key, value)
        db.session.commit()


def test_the_config_gives_each_stop_its_source(app, client, register_device):
    device_id, _secret = register_device()
    _set_config(app, device_id, tfl_stop_ids=["490000254W", "NSR:StopPlace:58366", "NSR:Quay:11145"], language="nb")
    body = _config(app, device_id).get_json()

    assert body["departures"]["stops"] == [
        {"source": "tfl", "id": "490000254W"},
        {"source": "entur", "id": "NSR:StopPlace:58366"},
        {"source": "entur", "id": "NSR:Quay:11145"},
    ]
    # A renderer that only knows `tfl` gets only the stops it can show.
    assert body["tfl"]["stop_ids"] == ["490000254W"]
    assert body["language"] == "nb"
    # No postcode: the first stop says where the screen is.
    assert body["timezone"] == "Europe/London"


@pytest.mark.parametrize(
    ("postcode", "stop_ids", "country", "timezone"),
    [
        ("0150", [], "NO", "Europe/Oslo"),
        ("0150", ["490000254W"], "NO", "Europe/Oslo"),
        ("SW1A 1AA", ["NSR:StopPlace:58366"], "GB", "Europe/London"),
        ("", ["NSR:StopPlace:58366", "490000254W"], None, "Europe/Oslo"),
    ],
)
def test_the_config_time_zone_follows_postcode_then_first_stop(
    app, client, register_device, postcode, stop_ids, country, timezone
):
    device_id, _secret = register_device()
    _set_config(app, device_id, postcode=postcode, tfl_stop_ids=stop_ids)
    body = _config(app, device_id).get_json()

    assert body["notice_board"] == {"postcode": postcode or None, "country": country, "style": "airy"}
    assert body["timezone"] == timezone


def _expire_codes(device_id):
    db.session.execute(
        update(PairingCode)
        .where(PairingCode.device_id == device_id)
        .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
    )
    db.session.commit()


def _codes_for(device_id):
    return db.session.scalars(select(PairingCode.code).where(PairingCode.device_id == device_id)).all()


def test_issuing_a_code_purges_other_devices_expired_codes(app, register_device):
    abandoned_id, _ = register_device()
    waiting_id, _ = register_device("another-very-long-device-secret")
    new_id, _ = register_device("a-third-very-long-device-secret")
    _issue(app, abandoned_id)
    waiting_code = _issue(app, waiting_id)
    with app.app_context():
        _expire_codes(abandoned_id)

    _issue(app, new_id)

    with app.app_context():
        assert _codes_for(abandoned_id) == []
        assert _codes_for(waiting_id) == [waiting_code]  # a live code is left alone


def test_purge_keeps_a_code_up_to_its_expiry(app, register_device):
    """The purge uses redeem_code's cutoff, so a code that can still be redeemed is never deleted."""
    device_id, _ = register_device()
    other_id, _ = register_device("another-very-long-device-secret")
    code = _issue(app, device_id)
    expiry = datetime.now(UTC) + timedelta(minutes=1)
    with app.app_context():
        db.session.execute(update(PairingCode).where(PairingCode.code == code).values(expires_at=expiry))
        db.session.commit()

    with patch("broker.services.pairing.datetime") as mocked:
        mocked.now.return_value = expiry
        _issue(app, other_id)

    with app.app_context():
        assert _codes_for(device_id) == [code]


def test_the_config_switches_glowmarkt_on_but_never_sends_its_credentials(app, paired_client):
    client, device_id, _secret = paired_client
    client.post("/device", data={"glowmarkt_username": "someone@example.com", "glowmarkt_password": "hunter2"})

    response = _config(app, device_id)

    assert response.get_json()["glowmarkt"] == {"enabled": True}
    assert b"someone@example.com" not in response.data
    assert b"hunter2" not in response.data


@pytest.mark.parametrize(
    "credentials",
    [
        {"username": "someone@example.com"},  # no password
        {"username": "  ", "password": "hunter2"},
        {"username": "someone@example.com", "password": "   "},
        {"username": "someone@example.com", "password_encrypted": "not-a-fernet-token"},  # won't decrypt
    ],
)
def test_the_config_leaves_glowmarkt_off_without_an_account_the_provider_can_use(
    app, client, register_device, credentials
):
    device_id, _secret = register_device()
    with app.app_context():
        creds = GlowmarktCredentials(device_id=device_id, **credentials)
        db.session.add(creds)
        db.session.commit()

    response = _config(app, device_id)

    assert response.status_code == 200
    assert response.get_json()["glowmarkt"] == {"enabled": False}


def test_pairing_spends_the_code(app, client, register_device):
    device_id, _secret = register_device()
    code = _issue(app, device_id)
    assert client.post("/pair", data={"code": code}).status_code == 302

    with app.app_context():
        assert pairing.live_code(device_id) is None
        assert db.session.get_one(Device, device_id).paired_at
        assert db.session.scalar(select(func.count()).where(PairingCode.device_id == device_id)) == 0


def test_a_failed_pairing_attempt_does_not_mark_the_device_paired(app, client, register_device):
    device_id, _secret = register_device()
    _issue(app, device_id)

    client.post("/pair", data={"code": "WRONG1"})

    with app.app_context():
        assert db.session.get_one(Device, device_id).paired_at is None


def test_the_config_is_valid_with_no_location_postcode_or_stops(app, client, register_device):
    device_id, _secret = register_device()
    with app.app_context():
        config = db.session.get_one(DeviceConfig, device_id)
        config.weather_location, config.postcode, config.tfl_stop_ids = "", "", []
        db.session.commit()

    body = _config(app, device_id).get_json()

    ConfigResponse.model_validate(body)
    assert "setup_missing" not in body
    assert body["weather"]["location"] == ""
    assert body["notice_board"] == {"postcode": None, "country": None, "style": "airy"}
    assert body["tfl"]["stop_ids"] == []


def test_the_config_returns_the_saved_postcode(app, client, register_device):
    device_id, _secret = register_device()
    with app.app_context():
        db.session.get_one(DeviceConfig, device_id).postcode = "SW1A 1AA"
        db.session.commit()

    response = _config(app, device_id)
    assert response.get_json()["notice_board"] == {"postcode": "SW1A 1AA", "country": "GB", "style": "airy"}


@pytest.mark.parametrize("design", ["columns", "timeline", "overview", "poster"])
def test_the_config_sends_the_weather_design(app, register_device, design):
    """As stored, whatever the screen's size: only an HD screen's renderer draws it, and the others ignore it."""
    device_id, _secret = register_device()
    _set_config(app, device_id, weather_design=design)

    assert _config(app, device_id).get_json()["weather"]["design"] == design


def test_the_config_sends_no_layout_by_default(app, register_device):
    """Null, the screen's default, for a screen whose owner never picked one."""
    device_id, _secret = register_device()

    assert _config(app, device_id).get_json()["screen"]["layout"] is None


@pytest.mark.parametrize(
    "layout",
    ["classic", "masthead", "column", "lockscreen", "timeline", "board", "pages", "sidebar", "focus", "newspaper"],
)
def test_the_config_sends_the_layout(app, register_device, layout):
    """As stored, whatever the screen's size: a renderer draws a layout its size doesn't have as its default."""
    device_id, _secret = register_device()
    _set_config(app, device_id, layout=layout)

    assert _config(app, device_id).get_json()["screen"]["layout"] == layout


def test_the_config_returns_the_saved_orientation(app, client, register_device):
    device_id, _secret = register_device()
    with app.app_context():
        db.session.get_one(DeviceConfig, device_id).orientation = "horizontal"
        db.session.commit()

    response = _config(app, device_id)
    assert response.get_json()["screen"]["orientation"] == "horizontal"


def test_register_stores_the_size_and_format_it_is_given(app, client):
    response = client.post(
        "/api/devices/register",
        json={"secret": "display-secret-0123456789", "width": 1080, "height": 1920, "format": "mono-1bpp-packed"},
    )
    with app.app_context():
        config = db.session.get_one(DeviceConfig, response.get_json()["device_id"])
        assert (config.width, config.height, config.format) == (1080, 1920, "mono-1bpp-packed")


@pytest.mark.parametrize(
    "extra",
    [
        {},
        {"width": 100},
        {"height": 100},
        {"width": 100, "height": 0},
        {"width": "100", "height": 100},
        {"width": True, "height": 100},
        {"width": 1.5, "height": 100},
    ],
)
def test_register_rejects_a_missing_or_bad_screen_size(client, extra):
    response = client.post(
        "/api/devices/register", json={"secret": "display-secret-0123456789", "format": "mono-1bpp-packed", **extra}
    )
    assert response.status_code == 400


@pytest.mark.parametrize("frame_format", ["mono-1bpp-packed", "png"])
def test_the_config_sends_the_frame_format_the_screen_registered_with(app, client, frame_format):
    response = client.post(
        "/api/devices/register",
        json={"secret": "display-secret-0123456789", "width": 800, "height": 480, "format": frame_format},
    )
    assert response.status_code == 201
    screen = _config(app, response.get_json()["device_id"]).get_json()["screen"]
    assert screen == {"orientation": "vertical", "width": 800, "height": 480, "format": frame_format, "layout": None}


@pytest.mark.parametrize(
    "extra", [{}, {"format": None}, {"format": ""}, {"format": "PNG"}, {"format": "1bpp"}, {"format": ["png"]}]
)
def test_register_rejects_a_missing_or_unknown_frame_format(app, client, extra):
    secret = "display-secret-0123456789"
    response = client.post("/api/devices/register", json={"secret": secret, "width": 480, "height": 800, **extra})
    assert response.status_code == 400
    assert response.get_json() == {"error": "format must be one of mono-1bpp-packed, png"}
    with app.app_context():
        assert db.session.scalar(select(func.count()).select_from(Device)) == 0


@pytest.mark.parametrize(("style", "departures_style"), [("airy", "grouped"), ("hero", "hero"), ("card", "card")])
def test_the_config_returns_the_saved_style_for_every_panel(app, client, register_device, style, departures_style):
    """The one style, and each panel's own for renderers that predate it: airy is grouped to the departures."""
    device_id, _secret = register_device()
    with app.app_context():
        db.session.get_one(DeviceConfig, device_id).style = style
        db.session.commit()

    config = _config(app, device_id).get_json()
    assert config["style"] == style
    assert (config["spotify"]["style"], config["notice_board"]["style"]) == (style, style)
    assert (config["tfl"]["style"], config["departures"]["style"]) == (departures_style, departures_style)


def test_the_config_returns_airy_for_a_new_device(app, register_device):
    device_id, _ = register_device()
    config = _config(app, device_id).get_json()
    assert config["style"] == "airy"


@pytest.mark.parametrize("clock_format", ["12h", "24h"])
def test_the_config_returns_the_clock_format_saved_on_the_website(app, paired_client, clock_format):
    client, device_id, _ = paired_client
    assert client.post("/device", data={"clock_format": clock_format}).status_code == 302

    response = _config(app, device_id)

    assert response.status_code == 200
    body = response.get_json()
    assert body["clock_format"] == clock_format
    assert ConfigResponse.model_validate(body).clock_format == clock_format
