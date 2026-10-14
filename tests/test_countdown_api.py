"""The broker's side of the countdown API.

The countdown-api package (https://github.com/nvorkinn/countdown-api) is generated from the contract: the
Pydantic models, and a Flask view for each route that this broker subclasses (broker/routes/devices.py).
create_app registers them with `register_all`, which refuses to start the app if a route or verb of the contract
has no implementation, so the contract gaining an operation fails the build here. These check the rest."""

import pytest
from countdown_api.blueprints import register_all
from countdown_api.models import ConfigResponse
from flask import Flask

from broker.db import db
from broker.models import Device, DeviceConfig


def test_a_live_response_has_exactly_the_contracts_fields(client, register_device):
    _, secret = register_device()

    body = client.get("/api/config", headers={"Authorization": f"Bearer {secret}"}).get_json()

    assert set(body) == set(ConfigResponse.model_fields)
    ConfigResponse.model_validate(body)


def test_the_device_name_header_is_read_by_the_generated_view(client, register_device):
    """X-Device-Name is declared by the contract, so the generated view reads it and hands it to the broker."""
    device_id, secret = register_device()

    client.get("/api/config", headers={"Authorization": f"Bearer {secret}", "X-Device-Name": "camilla"})

    with client.application.app_context():
        assert db.session.get(Device, device_id).device_name == "camilla"


def test_a_device_without_a_config_is_not_found(client, register_device):
    """The contract declares a 404 for it, with the same error body as the other errors."""
    device_id, secret = register_device()
    with client.application.app_context():
        db.session.delete(db.session.get(DeviceConfig, device_id))
        db.session.commit()

    response = client.get("/api/config", headers={"Authorization": f"Bearer {secret}"})

    assert response.status_code == 404
    assert response.get_json() == {"error": "not found"}


def test_the_app_refuses_to_start_without_an_implementation_of_every_route():
    with pytest.raises(ValueError):
        register_all(Flask(__name__))
