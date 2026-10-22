import pytest
from flask import g, jsonify, make_response

from broker import create_app
from broker.auth import require_device_auth, require_user, require_user_id


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
        response = make_response(view())
    assert response.status_code == 403
    assert response.get_json() == {"error": "forbidden"}


def test_require_device_auth_sets_device_id_and_role(app, register_device):
    device_id, secret = register_device()

    @require_device_auth()
    def view(device_id):
        return jsonify(device_id=g.device_id, role=g.role)

    with app.test_request_context(headers={"Authorization": f"Bearer {secret}"}):
        assert make_response(view()).get_json() == {"device_id": device_id, "role": "display"}


def test_session_cookie_domain_comes_from_the_environment(monkeypatch, app):
    monkeypatch.setenv("SESSION_COOKIE_DOMAIN", ".example.com")
    assert create_app().config["SESSION_COOKIE_DOMAIN"] == ".example.com"


def test_require_user_id_passes_the_signed_in_user_to_the_view(app):
    @require_user_id
    def view(user_id):
        return jsonify(user_id=user_id)

    with app.test_request_context(headers={app.config["USER_HEADER"]: " user-1 "}):
        assert make_response(view()).get_json() == {"user_id": "user-1"}


def test_require_user_id_keeps_the_urls_own_arguments(app):
    @require_user_id
    def view(name, user_id):
        return f"{user_id}/{name}"

    with app.test_request_context(headers={app.config["USER_HEADER"]: "user-1"}):
        assert view(name="project") == "user-1/project"


@pytest.mark.parametrize("headers", [{}, {"X-Authentik-Uid": ""}, {"X-Authentik-Uid": "  "}])
def test_require_user_id_turns_away_a_request_without_the_login_header(app, headers):
    @require_user_id
    def view(user_id):
        raise AssertionError

    with app.test_request_context(headers=headers):
        assert view() == ("Sign in required.", 401)


def test_require_user_lets_the_view_run_without_the_users_id(app):
    @require_user
    def view():
        return "ok"

    with app.test_request_context(headers={app.config["USER_HEADER"]: "user-1"}):
        assert view() == "ok"


@pytest.mark.parametrize("headers", [{}, {"X-Authentik-Uid": "  "}])
def test_require_user_turns_away_a_request_without_the_login_header(app, headers):
    @require_user
    def view():
        raise AssertionError

    with app.test_request_context(headers=headers):
        assert view() == ("Sign in required.", 401)
