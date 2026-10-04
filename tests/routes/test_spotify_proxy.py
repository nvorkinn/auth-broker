from unittest.mock import patch

import pytest

from broker.db import db
from broker.models import DeviceConfig


def test_now_playing_returns_null_when_spotify_disabled(client, register_device):
    device_id, secret = register_device()
    response = client.get(f"/api/devices/{device_id}/now-playing", headers={"Authorization": f"Bearer {secret}"})
    assert response.status_code == 200
    assert response.get_json() is None


def test_now_playing_returns_null_when_enabled_but_not_linked(paired_client):
    client, device_id, secret = paired_client
    client.post("/device", data={"interval": "15", "spotify_enabled": "on"})

    response = client.get(f"/api/devices/{device_id}/now-playing", headers={"Authorization": f"Bearer {secret}"})
    assert response.status_code == 200
    assert response.get_json() is None


def test_now_playing_proxies_spotify_module(paired_client):
    client, device_id, secret = paired_client
    client.post("/device", data={"interval": "15", "spotify_enabled": "on"})

    track = {
        "song": "A Song",
        "artist": "An Artist",
        "album": "An Album",
        "album_image": "http://x",
        "is_playing": True,
    }
    with patch("broker.clients.spotify.get_current_track", return_value=track) as mocked:
        response = client.get(f"/api/devices/{device_id}/now-playing", headers={"Authorization": f"Bearer {secret}"})

    mocked.assert_called_once_with(device_id)
    assert response.get_json() == track


def test_now_playing_unknown_device_rejected(client, register_device):
    _, secret = register_device()
    response = client.get("/api/devices/does-not-exist/now-playing", headers={"Authorization": f"Bearer {secret}"})
    assert response.status_code == 401


def test_queue_requires_auth(client, register_device):
    device_id, _ = register_device()
    assert client.get(f"/api/devices/{device_id}/queue").status_code == 401


def test_queue_returns_empty_list_when_spotify_disabled(client, register_device):
    device_id, secret = register_device()
    response = client.get(f"/api/devices/{device_id}/queue", headers={"Authorization": f"Bearer {secret}"})
    assert response.status_code == 200
    assert response.get_json() == []


def test_queue_returns_empty_list_when_enabled_but_not_linked(paired_client):
    client, device_id, secret = paired_client
    client.post("/device", data={"interval": "15", "spotify_enabled": "on"})

    response = client.get(f"/api/devices/{device_id}/queue", headers={"Authorization": f"Bearer {secret}"})
    assert response.status_code == 200
    assert response.get_json() == []


def test_queue_proxies_spotify_module(paired_client):
    client, device_id, secret = paired_client
    client.post("/device", data={"interval": "15", "spotify_enabled": "on"})

    queue = [{"song": "A Song", "artist": "An Artist", "album": "An Album", "album_image": "http://x"}]
    with patch("broker.clients.spotify.get_queue", return_value=queue) as mocked:
        response = client.get(f"/api/devices/{device_id}/queue", headers={"Authorization": f"Bearer {secret}"})

    mocked.assert_called_once_with(device_id)
    assert response.get_json() == queue


def test_spotify_view_is_not_called_when_spotify_disabled(client, register_device):
    device_id, secret = register_device()

    with (
        patch("broker.clients.spotify.get_current_track") as now_playing,
        patch("broker.clients.spotify.get_queue") as queue,
    ):
        client.get(f"/api/devices/{device_id}/now-playing", headers={"Authorization": f"Bearer {secret}"})
        client.get(f"/api/devices/{device_id}/queue", headers={"Authorization": f"Bearer {secret}"})

    now_playing.assert_not_called()
    queue.assert_not_called()


def test_spotify_disabled_check_runs_after_auth(client, register_device):
    """A caller without the device secret must get a 401, not the disabled default,
    otherwise anyone could probe which devices have Spotify switched on."""
    device_id, _ = register_device()

    for path in ("now-playing", "queue", "top/tracks"):
        assert client.get(f"/api/devices/{device_id}/{path}").status_code == 401
        wrong = client.get(f"/api/devices/{device_id}/{path}", headers={"Authorization": "Bearer wrong-secret"})
        assert wrong.status_code == 401


def test_spotify_endpoints_return_404_when_device_has_no_config_row(app, client, register_device):
    device_id, secret = register_device()
    with app.app_context():
        db.session.delete(db.session.get(DeviceConfig, device_id))
        db.session.commit()

    for path in ("now-playing", "queue", "top/tracks"):
        response = client.get(f"/api/devices/{device_id}/{path}", headers={"Authorization": f"Bearer {secret}"})
        assert response.status_code == 404


def test_top_returns_null_when_spotify_disabled(client, register_device):
    device_id, secret = register_device()
    response = client.get(f"/api/devices/{device_id}/top/tracks", headers={"Authorization": f"Bearer {secret}"})
    assert response.status_code == 200
    assert response.get_json() is None


def test_top_proxies_spotify_module_with_query_params(paired_client):
    client, device_id, secret = paired_client
    client.post("/device", data={"interval": "15", "spotify_enabled": "on"})

    top_items = {"items": [{"name": "A Song"}]}
    with patch("broker.clients.spotify.get_users_top_items", return_value=top_items) as mocked:
        response = client.get(
            f"/api/devices/{device_id}/top/tracks?limit=5&time_range=short_term",
            headers={"Authorization": f"Bearer {secret}"},
        )

    mocked.assert_called_once_with(device_id, "tracks", {"limit": "5", "time_range": "short_term"})
    assert response.get_json() == top_items


def test_top_passes_empty_params_when_no_query_string(paired_client):
    client, device_id, secret = paired_client
    client.post("/device", data={"interval": "15", "spotify_enabled": "on"})

    with patch("broker.clients.spotify.get_users_top_items", return_value=None) as mocked:
        response = client.get(f"/api/devices/{device_id}/top/artists", headers={"Authorization": f"Bearer {secret}"})

    mocked.assert_called_once_with(device_id, "artists", {})
    assert response.status_code == 200
    assert response.get_json() is None


def test_top_only_proxies_artists_or_tracks(paired_client):
    client, device_id, secret = paired_client
    client.post("/device", data={"interval": "15", "spotify_enabled": "on"})

    with patch("broker.clients.spotify.get_users_top_items") as mocked:
        response = client.get(f"/api/devices/{device_id}/top/albums", headers={"Authorization": f"Bearer {secret}"})

    assert response.status_code == 404
    mocked.assert_not_called()


# The same routes are also served under /api/spotify while the Pis move over to it.
NEW_PATHS = [
    ("now-playing", "get_current_track", ()),
    ("queue", "get_queue", ()),
    ("top/tracks", "get_users_top_items", ("tracks", {})),
]


@pytest.mark.parametrize(("path", "function", "extra_args"), NEW_PATHS)
def test_new_api_spotify_paths_proxy_spotify_module(paired_client, path, function, extra_args):
    client, device_id, secret = paired_client
    client.post("/device", data={"interval": "15", "spotify_enabled": "on"})

    with patch(f"broker.clients.spotify.{function}", return_value={"from": function}) as mocked:
        response = client.get(f"/api/spotify/{device_id}/{path}", headers={"Authorization": f"Bearer {secret}"})

    mocked.assert_called_once_with(device_id, *extra_args)
    assert response.status_code == 200
    assert response.get_json() == {"from": function}


@pytest.mark.parametrize(("path", "default"), [("now-playing", None), ("queue", []), ("top/tracks", None)])
def test_new_api_spotify_paths_return_default_when_spotify_disabled(client, register_device, path, default):
    device_id, secret = register_device()
    response = client.get(f"/api/spotify/{device_id}/{path}", headers={"Authorization": f"Bearer {secret}"})
    assert response.status_code == 200
    assert response.get_json() == default


@pytest.mark.parametrize("path", ["now-playing", "queue", "top/tracks"])
def test_new_api_spotify_paths_require_device_auth(client, register_device, path):
    device_id, _ = register_device()
    assert client.get(f"/api/spotify/{device_id}/{path}").status_code == 401
    wrong = client.get(f"/api/spotify/{device_id}/{path}", headers={"Authorization": "Bearer wrong-secret"})
    assert wrong.status_code == 401


def test_new_api_spotify_top_only_proxies_artists_or_tracks(paired_client):
    client, device_id, secret = paired_client
    client.post("/device", data={"interval": "15", "spotify_enabled": "on"})

    with patch("broker.clients.spotify.get_users_top_items") as mocked:
        response = client.get(f"/api/spotify/{device_id}/top/albums", headers={"Authorization": f"Bearer {secret}"})

    assert response.status_code == 404
    mocked.assert_not_called()
