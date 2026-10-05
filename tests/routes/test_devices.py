from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from sqlalchemy import func, select, update

from broker.db import db
from broker.models import Device, DeviceConfig, PairingCode


def test_register_creates_device(client):
    response = client.post("/api/devices/register", json={"device_secret": "a-very-long-device-secret-value"})
    assert response.status_code == 201
    assert "device_id" in response.get_json()


def test_register_rejects_short_secret(client):
    response = client.post("/api/devices/register", json={"device_secret": "short"})
    assert response.status_code == 400


def test_register_rejects_missing_body(client):
    response = client.post("/api/devices/register")
    assert response.status_code == 400


def test_pairing_code_requires_auth(client, register_device):
    device_id, _ = register_device()
    response = client.post(f"/api/devices/{device_id}/pairing-code")
    assert response.status_code == 401


def test_pairing_code_rejects_wrong_secret(client, register_device):
    device_id, _ = register_device()
    response = client.post(f"/api/devices/{device_id}/pairing-code", headers={"Authorization": "Bearer wrong-secret"})
    assert response.status_code == 401


def test_pairing_code_rejects_unknown_device(client, register_device):
    _, secret = register_device()
    response = client.post("/api/devices/does-not-exist/pairing-code", headers={"Authorization": f"Bearer {secret}"})
    assert response.status_code == 401


def test_pairing_code_uses_unambiguous_alphabet(client, register_device):
    device_id, secret = register_device()
    response = client.post(f"/api/devices/{device_id}/pairing-code", headers={"Authorization": f"Bearer {secret}"})
    assert response.status_code == 200
    body = response.get_json()
    assert len(body["code"]) == 6
    assert body["expires_in_seconds"] == 600
    for ambiguous_char in "0O1IL":
        assert ambiguous_char not in body["code"]


def test_requesting_new_pairing_code_invalidates_old_one(client, register_device):
    device_id, secret = register_device()
    headers = {"Authorization": f"Bearer {secret}"}

    first = client.post(f"/api/devices/{device_id}/pairing-code", headers=headers).get_json()["code"]
    client.post(f"/api/devices/{device_id}/pairing-code", headers=headers)

    response = client.post("/pair", data={"code": first})
    assert b"invalid or has expired" in response.data


def test_get_config_requires_auth(client, register_device):
    device_id, _ = register_device()
    response = client.get(f"/api/devices/{device_id}/config")
    assert response.status_code == 401


def test_get_config_returns_defaults_and_shared_keys(client, register_device):
    device_id, secret = register_device()
    response = client.get(f"/api/devices/{device_id}/config", headers={"Authorization": f"Bearer {secret}"})
    assert response.status_code == 200
    body = response.get_json()
    assert body == {
        "interval": 15,
        "weather": {"api_key": "test-weather-key", "location": ""},
        "notice_board": {"postcode": None},
        "tfl": {"app_key": "test-tfl-key", "stop_ids": []},
        "spotify": {"enabled": False},
        "glowmarkt": {"username": None, "password": None},
        "pairing_code": body["pairing_code"],
        "setup_missing": ["a weather location", "a postcode", "a bus or tube stop"],
    }


def _get_pairing_code(client, device_id, secret):
    response = client.get(f"/api/devices/{device_id}/config", headers={"Authorization": f"Bearer {secret}"})
    return response.get_json()["pairing_code"]


def test_get_config_returns_active_pairing_code(client, register_device):
    device_id, secret = register_device()
    code = client.post(
        f"/api/devices/{device_id}/pairing-code", headers={"Authorization": f"Bearer {secret}"}
    ).get_json()["code"]

    assert _get_pairing_code(client, device_id, secret) == code


def test_get_config_pairing_code_persists_until_paired(client, register_device):
    device_id, secret = register_device()
    code = client.post(
        f"/api/devices/{device_id}/pairing-code", headers={"Authorization": f"Bearer {secret}"}
    ).get_json()["code"]

    assert _get_pairing_code(client, device_id, secret) == code
    assert _get_pairing_code(client, device_id, secret) == code


def test_get_config_pairing_code_cleared_once_paired(client, register_device):
    device_id, secret = register_device()
    code = client.post(
        f"/api/devices/{device_id}/pairing-code", headers={"Authorization": f"Bearer {secret}"}
    ).get_json()["code"]

    assert client.post("/pair", data={"code": code}).status_code == 302

    assert _get_pairing_code(client, device_id, secret) is None


def test_get_config_replaces_an_expired_code_for_an_unpaired_device(app, client, register_device):
    device_id, secret = register_device()
    code = client.post(
        f"/api/devices/{device_id}/pairing-code", headers={"Authorization": f"Bearer {secret}"}
    ).get_json()["code"]

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
    client.post(f"/api/devices/{device_id}/pairing-code", headers=headers)
    second = client.post(f"/api/devices/{device_id}/pairing-code", headers=headers).get_json()["code"]

    assert _get_pairing_code(client, device_id, secret) == second


def test_get_config_pairing_code_not_leaked_to_other_devices(client, register_device):
    device_id, secret = register_device()
    other_id, other_secret = register_device("another-very-long-device-secret")
    client.post(f"/api/devices/{device_id}/pairing-code", headers={"Authorization": f"Bearer {secret}"})

    assert _get_pairing_code(client, other_id, other_secret) != _get_pairing_code(client, device_id, secret)


def test_get_config_stores_device_name_from_header(app, client, register_device):
    device_id, secret = register_device()
    client.get(
        f"/api/devices/{device_id}/config",
        headers={"Authorization": f"Bearer {secret}", "X-Device-Name": "camilla"},
    )

    with app.app_context():
        device_name = db.session.get(Device, device_id).device_name
    assert device_name == "camilla"


def test_get_config_without_device_name_header_leaves_it_unset(app, client, register_device):
    device_id, secret = register_device()
    client.get(f"/api/devices/{device_id}/config", headers={"Authorization": f"Bearer {secret}"})

    with app.app_context():
        device_name = db.session.get(Device, device_id).device_name
    assert device_name is None


def test_get_config_device_name_self_heals_on_rename(app, client, register_device):
    """A renamed Pi's next poll updates the stored name -- no re-registration needed."""
    device_id, secret = register_device()
    headers = {"Authorization": f"Bearer {secret}"}
    client.get(f"/api/devices/{device_id}/config", headers={**headers, "X-Device-Name": "old-name"})

    client.get(f"/api/devices/{device_id}/config", headers={**headers, "X-Device-Name": "new-name"})

    with app.app_context():
        device_name = db.session.get(Device, device_id).device_name
    assert device_name == "new-name"


def test_get_config_blank_device_name_header_does_not_overwrite(app, client, register_device):
    device_id, secret = register_device()
    headers = {"Authorization": f"Bearer {secret}"}
    client.get(f"/api/devices/{device_id}/config", headers={**headers, "X-Device-Name": "camilla"})

    client.get(f"/api/devices/{device_id}/config", headers={**headers, "X-Device-Name": "  "})

    with app.app_context():
        device_name = db.session.get(Device, device_id).device_name
    assert device_name == "camilla"


def test_get_config_returns_decrypted_glowmarkt_credentials(paired_client):
    client, device_id, secret = paired_client
    client.post("/device", data={"glowmarkt_username": "someone@example.com", "glowmarkt_password": "hunter2"})

    response = client.get(f"/api/devices/{device_id}/config", headers={"Authorization": f"Bearer {secret}"})
    assert response.get_json()["glowmarkt"] == {"username": "someone@example.com", "password": "hunter2"}


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
    forced = client.post(
        f"/api/devices/{device_id}/pairing-code", headers={"Authorization": f"Bearer {secret}"}
    ).get_json()["code"]

    assert _get_pairing_code(client, device_id, secret) == forced

    client.post("/pair", data={"code": forced})
    assert _get_pairing_code(client, device_id, secret) is None


def _setup_missing(client, device_id, secret):
    response = client.get(f"/api/devices/{device_id}/config", headers={"Authorization": f"Bearer {secret}"})
    return response.get_json()["setup_missing"]


def test_setup_missing_lists_what_is_still_needed(app, client, register_device):
    device_id, secret = register_device()
    assert _setup_missing(client, device_id, secret) == ["a weather location", "a postcode", "a bus or tube stop"]

    def configure(**columns):
        with app.app_context():
            config = db.session.get(DeviceConfig, device_id)
            for column, value in columns.items():
                setattr(config, column, value)
            db.session.commit()

    configure(weather_location="London")
    assert _setup_missing(client, device_id, secret) == ["a postcode", "a bus or tube stop"]

    configure(postcode="SW1A 1AA")
    assert _setup_missing(client, device_id, secret) == ["a bus or tube stop"]

    configure(weather_location="", tfl_stop_ids='["940GZZLUKNG"]')
    assert _setup_missing(client, device_id, secret) == ["a weather location"]

    configure(weather_location="London", postcode="")
    assert _setup_missing(client, device_id, secret) == ["a postcode"]

    configure(postcode="SW1A 1AA")
    assert _setup_missing(client, device_id, secret) == []


def test_get_config_returns_the_saved_postcode(app, client, register_device):
    device_id, secret = register_device()
    with app.app_context():
        db.session.get(DeviceConfig, device_id).postcode = "SW1A 1AA"
        db.session.commit()

    response = client.get(f"/api/devices/{device_id}/config", headers={"Authorization": f"Bearer {secret}"})
    assert response.get_json()["notice_board"] == {"postcode": "SW1A 1AA"}


def test_a_blank_weather_location_still_counts_as_missing(app, client, register_device):
    device_id, secret = register_device()
    with app.app_context():
        db.session.get(DeviceConfig, device_id).weather_location = "   "
        db.session.commit()

    assert "a weather location" in _setup_missing(client, device_id, secret)


# --- roles: register and attach -------------------------------------------------------------------


def _hashes(app, device_id):
    with app.app_context():
        device = db.session.get(Device, device_id)
        return device.renderer_secret_hash, device.display_secret_hash


@pytest.mark.parametrize("role", ["renderer", "display"])
def test_register_stores_the_hash_in_the_roles_column(app, client, role):
    response = client.post("/api/devices/register", json={"role": role, "secret": "a-very-long-device-secret-value"})

    assert response.status_code == 201
    renderer_hash, display_hash = _hashes(app, response.get_json()["device_id"])
    assert (renderer_hash is not None, display_hash is not None) == (role == "renderer", role == "display")
    assert "a-very-long-device-secret-value" not in (renderer_hash or "") + (display_hash or "")


def test_register_without_a_role_registers_a_renderer(app, client):
    response = client.post("/api/devices/register", json={"secret": "a-very-long-device-secret-value"})
    renderer_hash, display_hash = _hashes(app, response.get_json()["device_id"])
    assert renderer_hash is not None and display_hash is None


def test_register_rejects_an_unknown_role(client):
    response = client.post("/api/devices/register", json={"role": "admin", "secret": "a-very-long-device-secret-value"})
    assert response.status_code == 400


def test_attach_display_to_a_renderer_device(app, client, register_device):
    device_id, _ = register_device()

    attached_id, secret = register_device("display-secret-0123456789", role="display", device_id=device_id)

    assert attached_id == device_id
    assert all(h is not None for h in _hashes(app, device_id))
    assert (
        client.get(f"/api/devices/{device_id}/status", headers={"Authorization": f"Bearer {secret}"}).status_code == 200
    )


def test_attach_renderer_to_a_display_device(client, register_device):
    device_id, _ = register_device("display-secret-0123456789", role="display")

    _, secret = register_device("renderer-secret-0123456789", role="renderer", device_id=device_id)

    assert (
        client.get(f"/api/devices/{device_id}/config", headers={"Authorization": f"Bearer {secret}"}).status_code == 200
    )


def test_attach_to_a_taken_slot_is_a_conflict_and_keeps_the_old_secret(app, client, split_device):
    device_id, _, display_secret = split_device
    before = _hashes(app, device_id)

    response = client.post(
        "/api/devices/register",
        json={"role": "display", "secret": "an-attackers-long-secret-value", "device_id": device_id},
    )

    assert response.status_code == 409
    assert _hashes(app, device_id) == before
    assert (
        client.get(
            f"/api/devices/{device_id}/status", headers={"Authorization": f"Bearer {display_secret}"}
        ).status_code
        == 200
    )


def test_attach_to_an_unknown_device_is_a_conflict(client):
    response = client.post(
        "/api/devices/register",
        json={"role": "display", "secret": "a-very-long-device-secret-value", "device_id": "does-not-exist"},
    )
    assert response.status_code == 409


# --- roles: status and config ---------------------------------------------------------------------


def _status(client, device_id, secret):
    return client.get(f"/api/devices/{device_id}/status", headers={"Authorization": f"Bearer {secret}"})


def test_status_shows_a_pairing_code_to_an_unpaired_display(client, register_device):
    device_id, secret = register_device(role="display")

    body = _status(client, device_id, secret).get_json()

    assert body["paired"] is False
    assert len(body["pairing_code"]) == 6


def test_status_of_a_paired_display_has_no_code(client, register_device):
    device_id, secret = register_device(role="display")
    code = _status(client, device_id, secret).get_json()["pairing_code"]
    client.post("/pair", data={"code": code})

    assert _status(client, device_id, secret).get_json() == {"paired": True}


def test_status_is_forbidden_to_the_renderer(client, split_device):
    device_id, renderer_secret, _ = split_device
    assert _status(client, device_id, renderer_secret).status_code == 403


def test_status_requires_auth(client, split_device):
    device_id, _, _ = split_device
    assert client.get(f"/api/devices/{device_id}/status").status_code == 401
    assert _status(client, device_id, "wrong-secret").status_code == 401


def test_config_is_forbidden_to_the_display(client, split_device):
    device_id, _, display_secret = split_device
    response = client.get(f"/api/devices/{device_id}/config", headers={"Authorization": f"Bearer {display_secret}"})
    assert response.status_code == 403


@pytest.mark.parametrize("role", ["renderer", "display"])
def test_either_role_can_force_a_pairing_code(client, split_device, role):
    device_id, renderer_secret, display_secret = split_device
    secret = renderer_secret if role == "renderer" else display_secret
    response = client.post(f"/api/devices/{device_id}/pairing-code", headers={"Authorization": f"Bearer {secret}"})
    assert response.status_code == 200
