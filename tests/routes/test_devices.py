from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from countdown_api.models import ConfigResponse
from sqlalchemy import func, select, update

from broker.db import db
from broker.models import Device, DeviceConfig, GitHubToken, GlowmarktCredentials, PairingCode


def test_register_creates_device(client):
    secret = "a-very-long-device-secret-value"
    response = client.post("/api/devices/register", json={"secret": secret, "width": 480, "height": 800})
    assert response.status_code == 201
    assert "device_id" in response.get_json()


def test_register_rejects_short_secret(client):
    response = client.post("/api/devices/register", json={"secret": "short"})
    assert response.status_code == 400


def test_register_rejects_missing_body(client):
    response = client.post("/api/devices/register")
    assert response.status_code == 400


def test_pairing_code_requires_auth(client, register_device):
    _device_id, _ = register_device()
    response = client.post("/api/devices/pairing-code")
    assert response.status_code == 401


def test_pairing_code_rejects_wrong_secret(client, register_device):
    _device_id, _ = register_device()
    response = client.post("/api/devices/pairing-code", headers={"Authorization": "Bearer wrong-secret"})
    assert response.status_code == 401


def test_pairing_code_uses_unambiguous_alphabet(client, register_device):
    _device_id, secret = register_device()
    response = client.post("/api/devices/pairing-code", headers={"Authorization": f"Bearer {secret}"})
    assert response.status_code == 200
    body = response.get_json()
    assert len(body["code"]) == 6
    assert body["expires_in_seconds"] == 600
    for ambiguous_char in "0O1IL":
        assert ambiguous_char not in body["code"]


def test_requesting_new_pairing_code_invalidates_old_one(client, register_device):
    _device_id, secret = register_device()
    headers = {"Authorization": f"Bearer {secret}"}

    first = client.post("/api/devices/pairing-code", headers=headers).get_json()["code"]
    client.post("/api/devices/pairing-code", headers=headers)

    response = client.post("/pair", data={"code": first})
    assert b"invalid or has expired" in response.data


def test_get_config_requires_auth(client, register_device):
    _device_id, _ = register_device()
    response = client.get("/api/config")
    assert response.status_code == 401


def test_get_config_returns_defaults_and_shared_keys(client, register_device):
    device_id, secret = register_device()
    response = client.get("/api/config", headers={"Authorization": f"Bearer {secret}"})
    assert response.status_code == 200
    body = response.get_json()
    assert body == {
        "device_id": device_id,
        "interval": 60,
        "clock_format": "24h",
        "language": "en",
        "timezone": "Europe/London",
        "style": "airy",
        "screen": {"orientation": "vertical", "width": 480, "height": 800},
        "weather": {"api_key": "test-weather-key", "location": "", "key": None},
        "notice_board": {"postcode": None, "country": None, "style": "airy"},
        "tfl": {"app_key": "test-tfl-key", "stop_ids": [], "style": "grouped"},
        "departures": {"stops": [], "style": "grouped"},
        "spotify": {"enabled": False, "style": "airy"},
        "glowmarkt": {"enabled": False},
        "developer": {"enabled": False},
        "apple_music": {"enabled": False},
        "pairing_code": body["pairing_code"],
    }


def test_get_config_sends_the_developer_switch_but_never_the_github_token(app, client, register_device):
    device_id, secret = register_device()
    _set_config(app, device_id, developer_enabled=True)
    with app.app_context():
        token = GitHubToken(device_id=device_id)
        token.token = "github_pat_secret"
        db.session.add(token)
        db.session.commit()

    response = client.get("/api/config", headers={"Authorization": f"Bearer {secret}"})

    assert response.get_json()["developer"] == {"enabled": True}
    assert b"github_pat_secret" not in response.data


def test_get_config_sends_the_key_the_weather_is_subscribed_under(app, client, register_device):
    """The renderer reads v1:weather:<key>: the picked place's, beside its name."""
    device_id, secret = register_device()
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

    body = client.get("/api/config", headers={"Authorization": f"Bearer {secret}"}).get_json()

    assert body["weather"] == {"api_key": "test-weather-key", "location": "London", "key": "london-gb-2643743"}


def test_get_config_sends_the_slug_as_the_key_of_a_location_with_no_place_picked(app, client, register_device):
    """A config saved before places were picked, and not backfilled: its typed text, as it is subscribed."""
    device_id, secret = register_device()
    _set_config(app, device_id, weather_location="St. Albans")

    body = client.get("/api/config", headers={"Authorization": f"Bearer {secret}"}).get_json()

    assert (body["weather"]["location"], body["weather"]["key"]) == ("St. Albans", "st-albans")


def _set_config(app, device_id, **values):
    with app.app_context():
        config = db.session.get(DeviceConfig, device_id)
        for key, value in values.items():
            setattr(config, key, value)
        db.session.commit()


def test_get_config_gives_each_stop_its_source(app, client, register_device):
    device_id, secret = register_device()
    _set_config(app, device_id, tfl_stop_ids=["490000254W", "NSR:StopPlace:58366", "NSR:Quay:11145"], language="nb")
    body = client.get("/api/config", headers={"Authorization": f"Bearer {secret}"}).get_json()

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
def test_get_config_time_zone_follows_postcode_then_first_stop(
    app, client, register_device, postcode, stop_ids, country, timezone
):
    device_id, secret = register_device()
    _set_config(app, device_id, postcode=postcode, tfl_stop_ids=stop_ids)
    body = client.get("/api/config", headers={"Authorization": f"Bearer {secret}"}).get_json()

    assert body["notice_board"] == {"postcode": postcode or None, "country": country, "style": "airy"}
    assert body["timezone"] == timezone


def _get_pairing_code(client, device_id, secret):
    response = client.get("/api/config", headers={"Authorization": f"Bearer {secret}"})
    return response.get_json()["pairing_code"]


def test_get_config_returns_active_pairing_code(client, register_device):
    device_id, secret = register_device()
    code = client.post("/api/devices/pairing-code", headers={"Authorization": f"Bearer {secret}"}).get_json()["code"]

    assert _get_pairing_code(client, device_id, secret) == code


def test_get_config_pairing_code_persists_until_paired(client, register_device):
    device_id, secret = register_device()
    code = client.post("/api/devices/pairing-code", headers={"Authorization": f"Bearer {secret}"}).get_json()["code"]

    assert _get_pairing_code(client, device_id, secret) == code
    assert _get_pairing_code(client, device_id, secret) == code


def test_get_config_pairing_code_cleared_once_paired(client, register_device):
    device_id, secret = register_device()
    code = client.post("/api/devices/pairing-code", headers={"Authorization": f"Bearer {secret}"}).get_json()["code"]

    assert client.post("/pair", data={"code": code}).status_code == 302

    assert _get_pairing_code(client, device_id, secret) is None


def test_get_config_replaces_an_expired_code_for_an_unpaired_device(app, client, register_device):
    device_id, secret = register_device()
    code = client.post("/api/devices/pairing-code", headers={"Authorization": f"Bearer {secret}"}).get_json()["code"]

    with app.app_context():
        _expire_codes(device_id)

    fresh = _get_pairing_code(client, device_id, secret)
    assert fresh is not None
    assert fresh != code


def _expire_codes(device_id):
    db.session.execute(
        update(PairingCode)
        .where(PairingCode.device_id == device_id)
        .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
    )
    db.session.commit()


def _codes_for(device_id):
    return db.session.scalars(select(PairingCode.code).where(PairingCode.device_id == device_id)).all()


def test_issuing_a_code_purges_other_devices_expired_codes(app, client, register_device):
    abandoned_id, abandoned_secret = register_device()
    waiting_id, waiting_secret = register_device("another-very-long-device-secret")
    new_id, new_secret = register_device("a-third-very-long-device-secret")
    _get_pairing_code(client, abandoned_id, abandoned_secret)
    waiting_code = _get_pairing_code(client, waiting_id, waiting_secret)
    with app.app_context():
        _expire_codes(abandoned_id)

    _get_pairing_code(client, new_id, new_secret)

    with app.app_context():
        assert _codes_for(abandoned_id) == []
        assert _codes_for(waiting_id) == [waiting_code]  # a live code is left alone


def test_purge_keeps_a_code_up_to_its_expiry(app, client, register_device):
    """The purge uses redeem_code's cutoff, so a code that can still be redeemed is never deleted."""
    device_id, secret = register_device()
    other_id, other_secret = register_device("another-very-long-device-secret")
    code = _get_pairing_code(client, device_id, secret)
    expiry = datetime.now(UTC) + timedelta(minutes=1)
    with app.app_context():
        db.session.execute(update(PairingCode).where(PairingCode.code == code).values(expires_at=expiry))
        db.session.commit()

    with patch("broker.services.pairing.datetime") as mocked:
        mocked.now.return_value = expiry
        _get_pairing_code(client, other_id, other_secret)

    with app.app_context():
        assert _codes_for(device_id) == [code]


def test_get_config_pairing_code_is_the_latest_one(client, register_device):
    device_id, secret = register_device()
    headers = {"Authorization": f"Bearer {secret}"}
    client.post("/api/devices/pairing-code", headers=headers)
    second = client.post("/api/devices/pairing-code", headers=headers).get_json()["code"]

    assert _get_pairing_code(client, device_id, secret) == second


def test_get_config_pairing_code_not_leaked_to_other_devices(client, register_device):
    device_id, secret = register_device()
    other_id, other_secret = register_device("another-very-long-device-secret")
    client.post("/api/devices/pairing-code", headers={"Authorization": f"Bearer {secret}"})

    assert _get_pairing_code(client, other_id, other_secret) != _get_pairing_code(client, device_id, secret)


def test_get_config_stores_device_name_from_header(app, client, register_device):
    device_id, secret = register_device()
    client.get(
        "/api/config",
        headers={"Authorization": f"Bearer {secret}", "X-Device-Name": "camilla"},
    )

    with app.app_context():
        device_name = db.session.get(Device, device_id).device_name
    assert device_name == "camilla"


def test_get_config_without_device_name_header_keeps_the_name_it_has(app, client, register_device):
    device_id, secret = register_device()
    client.get("/api/config", headers={"Authorization": f"Bearer {secret}"})

    with app.app_context():
        device = db.session.get(Device, device_id)
        assert device.device_name == device.assignment.slug  # given when its renderer claimed it


def test_get_config_device_name_self_heals_on_rename(app, client, register_device):
    """A renamed Pi's next poll updates the stored name -- no re-registration needed."""
    device_id, secret = register_device()
    headers = {"Authorization": f"Bearer {secret}"}
    client.get("/api/config", headers={**headers, "X-Device-Name": "old-name"})

    client.get("/api/config", headers={**headers, "X-Device-Name": "new-name"})

    with app.app_context():
        device_name = db.session.get(Device, device_id).device_name
    assert device_name == "new-name"


def test_get_config_blank_device_name_header_does_not_overwrite(app, client, register_device):
    device_id, secret = register_device()
    headers = {"Authorization": f"Bearer {secret}"}
    client.get("/api/config", headers={**headers, "X-Device-Name": "camilla"})

    client.get("/api/config", headers={**headers, "X-Device-Name": "  "})

    with app.app_context():
        device_name = db.session.get(Device, device_id).device_name
    assert device_name == "camilla"


def test_get_config_switches_glowmarkt_on_but_never_sends_its_credentials(paired_client):
    client, _device_id, secret = paired_client
    client.post("/device", data={"glowmarkt_username": "someone@example.com", "glowmarkt_password": "hunter2"})

    response = client.get("/api/config", headers={"Authorization": f"Bearer {secret}"})

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
def test_get_config_leaves_glowmarkt_off_without_an_account_the_provider_can_use(
    app, client, register_device, credentials
):
    device_id, secret = register_device()
    with app.app_context():
        creds = GlowmarktCredentials(device_id=device_id, **credentials)
        db.session.add(creds)
        db.session.commit()

    response = client.get("/api/config", headers={"Authorization": f"Bearer {secret}"})

    assert response.status_code == 200
    assert response.get_json()["glowmarkt"] == {"enabled": False}


def test_get_config_issues_a_code_to_an_unpaired_device_and_keeps_it_stable(client, register_device):
    device_id, secret = register_device()

    code = _get_pairing_code(client, device_id, secret)

    assert len(code) == 6
    assert not set(code) & set("0O1IL")
    assert _get_pairing_code(client, device_id, secret) == code


def test_a_paired_device_gets_no_code_and_none_is_issued_again(app, client, register_device):
    device_id, secret = register_device()
    code = _get_pairing_code(client, device_id, secret)
    assert client.post("/pair", data={"code": code}).status_code == 302

    assert _get_pairing_code(client, device_id, secret) is None
    assert _get_pairing_code(client, device_id, secret) is None
    with app.app_context():
        assert db.session.get(Device, device_id).paired_at
        assert db.session.scalar(select(func.count()).where(PairingCode.device_id == device_id)) == 0


def test_a_failed_pairing_attempt_does_not_mark_the_device_paired(app, client, register_device):
    device_id, secret = register_device()
    _get_pairing_code(client, device_id, secret)

    client.post("/pair", data={"code": "WRONG1"})

    with app.app_context():
        assert db.session.get(Device, device_id).paired_at is None


def test_a_forced_code_for_a_paired_device_is_shown_until_redeemed(client, register_device):
    device_id, secret = register_device()
    client.post("/pair", data={"code": _get_pairing_code(client, device_id, secret)})
    forced = client.post("/api/devices/pairing-code", headers={"Authorization": f"Bearer {secret}"}).get_json()["code"]

    assert _get_pairing_code(client, device_id, secret) == forced

    client.post("/pair", data={"code": forced})
    assert _get_pairing_code(client, device_id, secret) is None


def _config_body(client, device_id, secret):
    response = client.get("/api/config", headers={"Authorization": f"Bearer {secret}"})
    assert response.status_code == 200
    return response.get_json()


def test_get_config_is_valid_with_no_location_postcode_or_stops(app, client, register_device):
    device_id, secret = register_device()
    with app.app_context():
        config = db.session.get(DeviceConfig, device_id)
        config.weather_location, config.postcode, config.tfl_stop_ids = "", "", []
        db.session.commit()

    body = _config_body(client, device_id, secret)

    ConfigResponse.model_validate(body)
    assert "setup_missing" not in body
    assert body["weather"]["location"] == ""
    assert body["notice_board"] == {"postcode": None, "country": None, "style": "airy"}
    assert body["tfl"]["stop_ids"] == []


def test_get_config_returns_the_saved_postcode(app, client, register_device):
    device_id, secret = register_device()
    with app.app_context():
        db.session.get(DeviceConfig, device_id).postcode = "SW1A 1AA"
        db.session.commit()

    response = client.get("/api/config", headers={"Authorization": f"Bearer {secret}"})
    assert response.get_json()["notice_board"] == {"postcode": "SW1A 1AA", "country": "GB", "style": "airy"}


def test_get_config_returns_the_saved_orientation(app, client, register_device):
    device_id, secret = register_device()
    with app.app_context():
        db.session.get(DeviceConfig, device_id).orientation = "horizontal"
        db.session.commit()

    response = client.get("/api/config", headers={"Authorization": f"Bearer {secret}"})
    assert response.get_json()["screen"]["orientation"] == "horizontal"


def test_register_stores_the_size_it_is_given(app, client):
    response = client.post(
        "/api/devices/register", json={"secret": "display-secret-0123456789", "width": 1080, "height": 1920}
    )
    with app.app_context():
        config = db.session.get(DeviceConfig, response.get_json()["device_id"])
        assert (config.width, config.height) == (1080, 1920)


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
    response = client.post("/api/devices/register", json={"secret": "display-secret-0123456789", **extra})
    assert response.status_code == 400


@pytest.mark.parametrize(("style", "departures_style"), [("airy", "grouped"), ("hero", "hero"), ("card", "card")])
def test_get_config_returns_the_saved_style_for_every_panel(app, client, register_device, style, departures_style):
    """The one style, and each panel's own for renderers that predate it: airy is grouped to the departures."""
    device_id, secret = register_device()
    headers = {"Authorization": f"Bearer {secret}"}
    with app.app_context():
        db.session.get(DeviceConfig, device_id).style = style
        db.session.commit()

    config = client.get("/api/config", headers=headers).get_json()
    assert config["style"] == style
    assert (config["spotify"]["style"], config["notice_board"]["style"]) == (style, style)
    assert (config["tfl"]["style"], config["departures"]["style"]) == (departures_style, departures_style)


def test_get_config_returns_airy_for_a_new_device(client, register_device):
    _, secret = register_device()
    config = client.get("/api/config", headers={"Authorization": f"Bearer {secret}"}).get_json()
    assert config["style"] == "airy"


@pytest.mark.parametrize("clock_format", ["12h", "24h"])
def test_get_config_returns_the_clock_format_saved_on_the_website(paired_client, clock_format):
    client, _, secret = paired_client
    assert client.post("/device", data={"clock_format": clock_format}).status_code == 302

    response = client.get("/api/config", headers={"Authorization": f"Bearer {secret}"})

    assert response.status_code == 200
    body = response.get_json()
    assert body["clock_format"] == clock_format
    assert ConfigResponse.model_validate(body).clock_format == clock_format
