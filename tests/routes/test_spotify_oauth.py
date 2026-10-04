from unittest.mock import Mock, patch

from broker.db import db
from broker.models import SpotifyToken


def _mock_response(json_data):
    response = Mock()
    response.status_code = 200
    response.json.return_value = json_data
    response.raise_for_status = Mock()
    return response


def test_login_requires_paired_session(client):
    response = client.get("/auth/spotify/login")
    assert response.status_code == 302
    assert response.headers["Location"] == "/pair"


def test_login_redirects_to_spotify_with_state(paired_client):
    client, device_id, _ = paired_client
    response = client.get("/auth/spotify/login")
    assert response.status_code == 302
    location = response.headers["Location"]
    assert location.startswith("https://accounts.spotify.com/authorize?")
    assert "client_id=test-client-id" in location
    assert "state=" in location


def test_callback_rejects_state_mismatch(paired_client):
    client, device_id, _ = paired_client
    response = client.get("/auth/spotify/callback?code=abc&state=wrong")
    assert response.status_code == 400


def test_callback_surfaces_spotify_error(paired_client):
    client, device_id, _ = paired_client
    response = client.get("/auth/spotify/callback?error=access_denied")
    assert response.status_code == 400


def test_callback_exchanges_code_and_stores_tokens(app, paired_client):
    client, device_id, _ = paired_client

    login_response = client.get("/auth/spotify/login")
    state = login_response.headers["Location"].split("state=")[1]

    token_payload = {
        "access_token": "access-123",
        "refresh_token": "refresh-123",
        "expires_in": 3600,
    }
    with patch("broker.clients.spotify.requests.post", return_value=_mock_response(token_payload)):
        response = client.get(f"/auth/spotify/callback?code=abc123&state={state}")

    assert response.status_code == 302
    assert response.headers["Location"] == "/device"

    with app.app_context():
        token = db.session.get(SpotifyToken, device_id)
        assert token.refresh_token == "refresh-123"
        assert token.access_token == "access-123"
