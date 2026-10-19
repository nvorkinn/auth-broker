from flask import g, jsonify

from broker import create_app
from broker.auth import require_device_auth


def test_require_device_auth_rejects_missing_header(client, register_device):
    _device_id, _ = register_device()
    response = client.post("/api/frame")
    assert response.status_code == 401


def test_require_device_auth_rejects_non_bearer_header(client, register_device):
    _device_id, secret = register_device()
    response = client.post("/api/frame", headers={"Authorization": secret})
    assert response.status_code == 401


def test_require_device_auth_accepts_correct_secret(client, register_device):
    _device_id, secret = register_device()
    response = client.post("/api/frame", headers={"Authorization": f"Bearer {secret}"})
    # Let in: there's just no frame yet.
    assert response.status_code == 404
    assert response.get_json() == {"error": "no frame yet"}


def test_require_paired_user_redirects_when_unpaired(client):
    response = client.get("/device")
    assert response.status_code == 302
    assert response.headers["Location"] == "/pair"


def test_require_paired_user_allows_the_paired_user(paired_client):
    client, _, _ = paired_client
    response = client.get("/device")
    assert response.status_code == 200


def test_require_device_auth_rejects_an_empty_bearer_secret(client, register_device):
    _device_id, _ = register_device()
    response = client.post("/api/frame", headers={"Authorization": "Bearer "})
    assert response.status_code == 401


def test_require_device_auth_forbids_a_valid_secret_for_a_role_not_allowed(app, register_device):
    _device_id, secret = register_device()

    @require_device_auth(roles=frozenset())
    def view(device_id):
        return jsonify(device_id=device_id)

    with app.test_request_context(headers={"Authorization": f"Bearer {secret}"}):
        response, status = view()
    assert status == 403
    assert response.get_json() == {"error": "forbidden"}


def test_require_device_auth_sets_device_id_and_role(app, register_device):
    device_id, secret = register_device()

    @require_device_auth()
    def view(device_id):
        return jsonify(device_id=g.device_id, role=g.role)

    with app.test_request_context(headers={"Authorization": f"Bearer {secret}"}):
        assert view().get_json() == {"device_id": device_id, "role": "display"}


def test_session_cookie_domain_comes_from_the_environment(monkeypatch, app):
    monkeypatch.setenv("SESSION_COOKIE_DOMAIN", ".example.com")
    assert create_app().config["SESSION_COOKIE_DOMAIN"] == ".example.com"
