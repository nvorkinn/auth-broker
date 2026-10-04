from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest

from broker.db import db
from broker.models import Device, DeviceConfig

TOKEN = "test-status-token"


@pytest.fixture
def status_on(app):
    app.config["STATUS_TOKEN"] = TOKEN


def _status(client, token=TOKEN):
    return client.get("/api/status/devices", headers={"Authorization": f"Bearer {token}"})


def _poll(client, device_id, secret, path="/api/devices/{}/config"):
    return client.get(path.format(device_id), headers={"Authorization": f"Bearer {secret}"})


def _last_seen(app, device_id):
    with app.app_context():
        return db.session.get(Device, device_id).last_seen_at


def test_status_is_off_without_a_token(client):
    assert _status(client).status_code == 404
    assert _status(client, token="").status_code == 404


@pytest.mark.usefixtures("status_on")
def test_status_requires_the_token(client):
    assert client.get("/api/status/devices").status_code == 401
    assert _status(client, token="wrong-token").status_code == 401
    assert client.get("/api/status/devices", headers={"Authorization": TOKEN}).status_code == 401


@pytest.mark.usefixtures("status_on")
def test_status_is_read_only(client):
    for method in ("post", "put", "patch", "delete"):
        response = getattr(client, method)("/api/status/devices", headers={"Authorization": f"Bearer {TOKEN}"})
        assert response.status_code == 405


@pytest.mark.usefixtures("status_on")
def test_status_lists_every_device(app, client, register_device):
    paired_id, paired_secret = register_device()
    client.post("/pair", data={"code": _poll(client, paired_id, paired_secret).get_json()["pairing_code"]})
    new_id, _ = register_device("another-very-long-device-secret")
    with app.app_context():
        config = db.session.get(DeviceConfig, paired_id)
        config.weather_location, config.postcode = "London", "SW1A 1AA"
        db.session.get(Device, paired_id).device_name = "camilla"
        db.session.commit()
        paired = db.session.get(Device, paired_id)
        created_at, paired_at, last_seen_at = paired.created_at, paired.paired_at, paired.last_seen_at

    response = _status(client)

    assert response.status_code == 200
    assert response.get_json() == {
        paired_id: {
            "device_name": "camilla",
            "created_at": created_at.isoformat(),
            "paired_at": paired_at.isoformat(),
            "last_seen_at": last_seen_at.isoformat(),
            "setup_missing": ["a bus or tube stop"],
        },
        new_id: {
            "device_name": None,
            "created_at": response.get_json()[new_id]["created_at"],
            "paired_at": None,
            "last_seen_at": None,
            "setup_missing": ["a weather location", "a postcode", "a bus or tube stop"],
        },
    }
    assert datetime.fromisoformat(response.get_json()[new_id]["created_at"]).tzinfo == UTC


@pytest.mark.usefixtures("status_on")
def test_status_reports_null_setup_missing_for_a_device_without_config(app, client, register_device):
    device_id, _ = register_device()
    with app.app_context():
        db.session.delete(db.session.get(DeviceConfig, device_id))
        db.session.commit()

    assert _status(client).get_json()[device_id]["setup_missing"] is None


def test_last_seen_at_is_unset_until_the_device_authenticates(app, client, register_device):
    device_id, _ = register_device()
    assert _last_seen(app, device_id) is None

    client.get(f"/api/devices/{device_id}/config", headers={"Authorization": "Bearer wrong-secret"})

    assert _last_seen(app, device_id) is None


@pytest.mark.parametrize(
    "path",
    [
        "/api/devices/{}/config",
        "/api/devices/{}/now-playing",
        "/api/spotify/{}/queue",
        "/api/frames/{}/frame",
    ],
)
def test_any_authenticated_device_request_sets_last_seen_at(app, client, register_device, path):
    device_id, secret = register_device()
    before = datetime.now(UTC)

    _poll(client, device_id, secret, path)

    assert before <= _last_seen(app, device_id) <= datetime.now(UTC)


def test_last_seen_at_is_updated_at_most_once_a_minute(app, client, register_device):
    device_id, secret = register_device()
    _poll(client, device_id, secret)
    first = _last_seen(app, device_id)

    with patch("broker.auth.datetime") as mocked:
        mocked.now.return_value = first + timedelta(seconds=59)
        _poll(client, device_id, secret)
        assert _last_seen(app, device_id) == first

        mocked.now.return_value = first + timedelta(minutes=1)
        _poll(client, device_id, secret)
        assert _last_seen(app, device_id) == first + timedelta(minutes=1)
