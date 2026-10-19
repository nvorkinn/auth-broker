import hashlib

import pytest
from sqlalchemy import select

from broker.auth import hash_secret
from broker.db import db
from broker.models import Device
from broker.services import pairing

SECRET = "a-very-long-device-secret-value"
SCREEN_SECRET = SECRET  # the screen's own, as register_device registers it


def token_for_secret(secret=SCREEN_SECRET):
    """Compute what the device does: SHA-256 of "pairing:" and the hex SHA-256 of its secret."""
    return hashlib.sha256(f"pairing:{hashlib.sha256(secret.encode()).hexdigest()}".encode()).hexdigest()


def _pair(client, token=None):
    return client.post("/pair/token", json={"token": token or token_for_secret()})


def _owner(app, device_id):
    with app.app_context():
        return db.session.get(Device, device_id).owner


def test_the_token_is_derived_from_the_secret_the_way_a_device_would():
    assert pairing.token_for(hash_secret(SECRET)) == token_for_secret(SECRET)


def test_a_device_registering_stores_its_token(app, register_device):
    device_id, _ = register_device(SECRET)
    with app.app_context():
        assert db.session.get(Device, device_id).pairing_token == token_for_secret()


def test_the_token_pairs_the_user_with_a_registered_device(app, client, register_device):
    device_id, _ = register_device(SECRET)

    response = _pair(client)

    assert response.status_code == 200
    assert response.get_json() == {"status": "paired"}
    assert _owner(app, device_id) == "user-1"
    assert client.get("/device").status_code == 200


def test_the_device_never_shows_a_code_once_paired_by_token(app, client, register_device):
    device_id, _ = register_device(SECRET)
    with app.app_context():
        pairing.issue_code(device_id)
    _pair(client)
    with app.app_context():
        assert pairing.live_code(device_id) is None


def test_the_browser_waits_for_a_device_that_has_not_registered_yet(client, register_device):
    assert _pair(client).status_code == 202
    assert _pair(client).get_json() == {"status": "waiting"}
    assert client.get("/device").status_code == 302  # nothing to show yet

    register_device(SECRET)

    assert _pair(client).status_code == 200
    assert client.get("/device").status_code == 200


def test_pairing_again_as_the_same_user_is_fine(client, register_device):
    register_device(SECRET)
    assert _pair(client).status_code == 200
    assert _pair(client).status_code == 200


def test_a_device_with_an_owner_is_not_taken_by_another_user(app, client, register_device):
    device_id, _ = register_device(SECRET)
    _pair(client)
    other = app.test_client()
    other.environ_base["HTTP_X_AUTHENTIK_UID"] = "someone-else"

    response = _pair(other)

    assert response.status_code == 409
    assert _owner(app, device_id) == "user-1"


def test_a_different_secret_does_not_pair_with_the_device(app, client, register_device):
    device_id, _ = register_device(SECRET)

    assert _pair(client, token_for_secret("some-other-device-secret-0123")).status_code == 202
    assert _owner(app, device_id) is None


@pytest.mark.parametrize("token", ["", "short", "z" * 64, "A" * 64, None, 5])
def test_a_malformed_token_is_refused(client, token):
    assert client.post("/pair/token", json={"token": token}).status_code == 400


def test_pairing_by_token_needs_the_login_proxy_header(anonymous_client, register_device):
    register_device(SECRET)
    assert _pair(anonymous_client).status_code == 401


def test_the_pairing_page_carries_the_token_script_and_a_code_fallback(client):
    page = client.get("/pair").get_data(as_text=True)
    assert "location.hash" in page
    assert 'name="code"' in page


def test_the_pairing_page_passes_on_a_query_token(client):
    token = token_for_secret()
    assert token in client.get(f"/pair?t={token}").get_data(as_text=True)


def test_devices_registered_without_a_token_column_value_cannot_be_paired_by_token(app, client, register_device):
    device_id, _ = register_device(SECRET)
    with app.app_context():
        db.session.get(Device, device_id).pairing_token = None
        db.session.commit()
        assert db.session.scalar(select(Device).where(Device.pairing_token == token_for_secret())) is None
    assert _pair(client).status_code == 202
