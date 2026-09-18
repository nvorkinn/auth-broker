import time
from unittest.mock import Mock, patch

from broker import spotify as spotify_module
from broker.db import get_db


def _mock_response(json_data=None, status_code=200, content=b"{}"):
    response = Mock()
    response.status_code = status_code
    response.content = content if json_data is None else b"non-empty"
    response.json.return_value = json_data or {}
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
    with patch("broker.spotify.requests.post", return_value=_mock_response(token_payload)):
        response = client.get(f"/auth/spotify/callback?code=abc123&state={state}")

    assert response.status_code == 302
    assert response.headers["Location"] == "/device"

    with app.app_context():
        row = get_db().execute("SELECT * FROM spotify_tokens WHERE device_id = ?", (device_id,)).fetchone()
        assert row["refresh_token"] == "refresh-123"
        assert row["access_token"] == "access-123"


def test_get_current_track_returns_none_when_not_linked(app, paired_client):
    _, device_id, _ = paired_client
    with app.app_context():
        assert spotify_module.get_current_track(device_id) is None


def test_get_current_track_refreshes_expired_token(app, paired_client):
    _, device_id, _ = paired_client

    with app.app_context():
        db = get_db()
        db.execute(
            "INSERT INTO spotify_tokens (device_id, refresh_token, access_token, expires_at) VALUES (?, ?, ?, ?)",
            (device_id, "old-refresh", "old-access", time.time() - 100),
        )
        db.commit()

        refresh_payload = {"access_token": "new-access", "expires_in": 3600}
        now_playing_payload = {
            "is_playing": True,
            "item": {
                "name": "Song",
                "artists": [{"name": "Artist"}],
                "album": {
                    "name": "Album",
                    "images": [{"height": 300, "url": "http://big"}, {"height": 64, "url": "http://small"}],
                },
            },
        }

        def fake_post(url, **kwargs):
            assert url == spotify_module.TOKEN_URL
            assert kwargs["data"]["grant_type"] == "refresh_token"
            assert kwargs["data"]["refresh_token"] == "old-refresh"
            return _mock_response(refresh_payload)

        with (
            patch("broker.spotify.requests.post", side_effect=fake_post),
            patch("broker.spotify.requests.get", return_value=_mock_response(now_playing_payload)),
        ):
            track = spotify_module.get_current_track(device_id)

        assert track == {
            "song": "Song",
            "artist": "Artist",
            "album": "Album",
            "album_image": "http://small",
            "is_playing": True,
        }

        row = db.execute("SELECT * FROM spotify_tokens WHERE device_id = ?", (device_id,)).fetchone()
        assert row["access_token"] == "new-access"
        # Spotify didn't rotate the refresh token in this response - the old one must survive.
        assert row["refresh_token"] == "old-refresh"


def test_get_current_track_returns_none_on_204(app, paired_client):
    _, device_id, _ = paired_client

    with app.app_context():
        db = get_db()
        db.execute(
            "INSERT INTO spotify_tokens (device_id, refresh_token, access_token, expires_at) VALUES (?, ?, ?, ?)",
            (device_id, "refresh", "access", time.time() + 3600),
        )
        db.commit()

        with patch("broker.spotify.requests.get", return_value=_mock_response(status_code=204, content=b"")):
            assert spotify_module.get_current_track(device_id) is None


def test_get_current_track_returns_none_when_nothing_playing(app, paired_client):
    _, device_id, _ = paired_client

    with app.app_context():
        db = get_db()
        db.execute(
            "INSERT INTO spotify_tokens (device_id, refresh_token, access_token, expires_at) VALUES (?, ?, ?, ?)",
            (device_id, "refresh", "access", time.time() + 3600),
        )
        db.commit()

        with patch("broker.spotify.requests.get", return_value=_mock_response({"is_playing": False, "item": None})):
            assert spotify_module.get_current_track(device_id) is None
