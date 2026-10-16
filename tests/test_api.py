"""Test the routes of the countdown API contract (vendor/countdown-api).

The broker serves them through the registry in broker/api.py rather than as plain blueprints.
"""

import pytest
from countdown_api.blueprints import Api, RegistrationError
from countdown_api.models import ConfigResponse
from flask import Flask

from broker import create_app
from broker.api import BrokerApi, api
from broker.db import db
from broker.models import Device, DeviceConfig


def test_the_broker_answers_every_operation_of_the_contract(app):
    api.verify()

    endpoints = {rule.endpoint for rule in app.url_map.iter_rules()}
    assert {f"api.{operation.id}" for operation in api.OPERATIONS} <= endpoints


def test_the_app_wont_start_if_an_operation_has_no_answer(app, monkeypatch):
    """Check what a contract change the broker hasn't caught up with does.

    The submodule gains an operation, nothing here registers it, and the app stops, so the build fails
    rather than the first request.
    """
    monkeypatch.setattr("broker.api.api", BrokerApi())

    with pytest.raises(RegistrationError) as caught:
        create_app()

    assert "no handler is registered for get_config (GET /api/config)" in caught.value.problems
    assert any("'deviceSecret'" in problem for problem in caught.value.problems)


def test_the_contracts_own_registry_refuses_an_empty_server():
    with pytest.raises(RegistrationError):
        Api().register(Flask(__name__))


def test_config_is_what_the_contract_says(client, register_device):
    _, secret = register_device()

    response = client.get("/api/config", headers={"Authorization": f"Bearer {secret}"})

    assert response.status_code == 200
    assert set(response.get_json()) == set(ConfigResponse.model_fields)
    assert ConfigResponse.model_validate(response.get_json()).interval == 15


@pytest.mark.parametrize("authorization", [None, "", "Bearer ", "Bearer nope", "bearer nope", "nope"])
def test_config_without_a_known_secret_is_a_401(client, register_device, authorization):
    register_device()
    headers = {} if authorization is None else {"Authorization": authorization}

    response = client.get("/api/config", headers=headers)

    assert response.status_code == 401
    assert response.get_json() == {"error": "unauthorized"}


def test_the_prefix_must_be_spelled_exactly_even_with_the_right_secret(client, register_device):
    _, secret = register_device()

    assert client.get("/api/config", headers={"Authorization": f"bearer {secret}"}).status_code == 401
    assert client.get("/api/config", headers={"Authorization": f"Bearer {secret}"}).status_code == 200


def test_the_displays_secret_is_forbidden_by_the_contracts_scopes(client, split_device):
    _, _, display_secret = split_device

    response = client.get("/api/config", headers={"Authorization": f"Bearer {display_secret}"})

    assert response.status_code == 403
    assert response.get_json() == {"error": "forbidden"}


def test_a_missing_credential_is_challenged(client):
    response = client.get("/api/config")

    assert response.headers["WWW-Authenticate"] == "Bearer"


def test_a_device_without_config_is_a_404_that_still_records_its_name(app, client, register_device):
    device_id, secret = register_device()
    with app.app_context():
        db.session.delete(db.session.get(DeviceConfig, device_id))
        db.session.commit()

    response = client.get("/api/config", headers={"Authorization": f"Bearer {secret}", "X-Device-Name": "kitchen"})

    assert response.status_code == 404
    assert response.get_json() == {"error": "not found"}
    with app.app_context():
        assert db.session.get(Device, device_id).device_name == "kitchen"
