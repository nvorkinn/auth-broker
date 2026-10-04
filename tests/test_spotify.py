import time
from unittest.mock import Mock, patch

from broker import spotify as spotify_module
from broker.db import db
from broker.models import SpotifyToken


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
        token = db.session.get(SpotifyToken, device_id)
        assert token.refresh_token == "refresh-123"
        assert token.access_token == "access-123"


def test_get_current_track_returns_none_when_not_linked(app, paired_client):
    _, device_id, _ = paired_client
    with app.app_context():
        assert spotify_module.get_current_track(device_id) is None


def test_get_current_track_refreshes_expired_token(app, paired_client):
    _, device_id, _ = paired_client

    with app.app_context():
        db.session.add(
            SpotifyToken(
                device_id=device_id,
                refresh_token="old-refresh",
                access_token="old-access",
                expires_at=str(time.time() - 100),
            )
        )
        db.session.commit()

        refresh_payload = {"access_token": "new-access", "expires_in": 3600}
        now_playing_payload = {
            "is_playing": True,
            "item": {
                "name": "Song",
                "artists": [{"name": "Artist"}],
                "album": {
                    "name": "Album",
                    "images": [{"height": 300, "url": "https://big"}, {"height": 64, "url": "https://small"}],
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

        assert track == now_playing_payload

        row = db.session.get(SpotifyToken, device_id)
        assert row.access_token == "new-access"
        # Spotify didn't rotate the refresh token in this response - the old one must survive.
        assert row.refresh_token == "old-refresh"


def test_get_current_track_returns_none_on_204(app, paired_client):
    _, device_id, _ = paired_client

    with app.app_context():
        db.session.add(
            SpotifyToken(
                device_id=device_id, refresh_token="refresh", access_token="access", expires_at=str(time.time() + 3600)
            )
        )
        db.session.commit()

        with patch("broker.spotify.requests.get", return_value=_mock_response(status_code=204, content=b"")):
            assert spotify_module.get_current_track(device_id) is None


def test_get_current_track_passes_through_when_nothing_playing(app, paired_client):
    _, device_id, _ = paired_client
    payload = {"is_playing": False, "item": None}

    with app.app_context():
        db.session.add(
            SpotifyToken(
                device_id=device_id, refresh_token="refresh", access_token="access", expires_at=str(time.time() + 3600)
            )
        )
        db.session.commit()

        with patch("broker.spotify.requests.get", return_value=_mock_response(payload)):
            assert spotify_module.get_current_track(device_id) == payload


def _link_spotify(device_id):
    db.session.add(
        SpotifyToken(
            device_id=device_id, refresh_token="refresh", access_token="access", expires_at=str(time.time() + 3600)
        )
    )
    db.session.commit()


def test_get_queue_returns_empty_when_not_linked(app, paired_client):
    _, device_id, _ = paired_client
    with app.app_context():
        assert spotify_module.get_queue(device_id) == []


def test_get_queue_passes_through_spotify_response(app, paired_client):
    _, device_id, _ = paired_client
    payload = {
        "currently_playing": {"name": "Now"},
        "queue": [
            {
                "type": "track",
                "name": "Song",
                "artists": [{"name": "A"}, {"name": "B"}],
                "album": {
                    "name": "Album",
                    "images": [{"height": 300, "url": "https://big"}, {"height": 64, "url": "https://small"}],
                },
            },
            {
                "type": "episode",
                "name": "Episode",
                "show": {"name": "Show", "publisher": "Pub", "images": [{"height": 64, "url": "https://show"}]},
                "images": [],
            },
        ],
    }

    with app.app_context():
        _link_spotify(device_id)
        with patch("broker.spotify.requests.get", return_value=_mock_response(payload)) as mocked:
            queue = spotify_module.get_queue(device_id)

    assert mocked.call_args.args[0] == "https://api.spotify.com/v1/me/player/queue"
    assert mocked.call_args.kwargs["headers"] == {"Authorization": "Bearer access"}
    assert queue == payload


def test_get_queue_returns_empty_on_204(app, paired_client):
    _, device_id, _ = paired_client

    with app.app_context():
        _link_spotify(device_id)
        with patch("broker.spotify.requests.get", return_value=_mock_response(status_code=204, content=b"")):
            assert spotify_module.get_queue(device_id) == []


def test_get_queue_refreshes_expired_token(app, paired_client):
    _, device_id, _ = paired_client

    with app.app_context():
        db.session.add(
            SpotifyToken(
                device_id=device_id,
                refresh_token="old-refresh",
                access_token="old-access",
                expires_at=str(time.time() - 100),
            )
        )
        db.session.commit()

        with (
            patch(
                "broker.spotify.requests.post",
                return_value=_mock_response({"access_token": "new-access", "expires_in": 3600}),
            ),
            patch("broker.spotify.requests.get", return_value=_mock_response({"queue": []})) as mocked,
        ):
            assert spotify_module.get_queue(device_id) == {"queue": []}

        assert mocked.call_args.kwargs["headers"] == {"Authorization": "Bearer new-access"}


def test_get_users_top_items_returns_none_when_not_linked(app, paired_client):
    _, device_id, _ = paired_client
    with app.app_context():
        assert spotify_module.get_users_top_items(device_id, "tracks", {}) is None


def test_get_users_top_items_passes_type_and_params_and_returns_raw_response(app, paired_client):
    _, device_id, _ = paired_client
    payload = {"items": [{"name": "Song", "popularity": 50}], "total": 1}

    with app.app_context():
        _link_spotify(device_id)
        with patch("broker.spotify.requests.get", return_value=_mock_response(payload)) as mocked:
            result = spotify_module.get_users_top_items(device_id, "tracks", {"limit": "5"})

    assert mocked.call_args.args[0] == "https://api.spotify.com/v1/me/top/tracks"
    assert mocked.call_args.kwargs["headers"] == {"Authorization": "Bearer access"}
    assert mocked.call_args.kwargs["params"] == {"limit": "5"}
    assert result == payload


def test_get_users_top_items_returns_empty_on_204(app, paired_client):
    _, device_id, _ = paired_client

    with app.app_context():
        _link_spotify(device_id)
        with patch("broker.spotify.requests.get", return_value=_mock_response(status_code=204, content=b"")):
            assert spotify_module.get_users_top_items(device_id, "artists", {}) == []
