from unittest.mock import patch

import pytest
import requests

from broker.clients import apple_music
from broker.db import db
from broker.models import AppleMusicPlay, AppleMusicToken, DeviceConfig
from tests.conftest import apple_song as song


def _get(client, secret, path):
    return client.get(f"/api/apple-music{path}", headers={"Authorization": f"Bearer {secret}"})


def _switch_on(app, device_id, *, source="apple_music", linked=True):
    with app.app_context():
        config = db.session.get(DeviceConfig, device_id)
        config.spotify_enabled = True
        config.music_source = source
        if linked:
            stored = AppleMusicToken(device_id=device_id, seen_ids=[])
            stored.token = "user-token"
            db.session.add(stored)
        db.session.commit()


def test_requires_auth(client, register_device):
    register_device()

    assert client.get("/api/apple-music/queue").status_code == 401


@pytest.mark.parametrize("source", ["spotify", "apple_music"])
def test_queue_is_empty_unless_apple_music_is_chosen_and_set_up(app, client, register_device, source):
    device_id, secret = register_device()
    _switch_on(app, device_id, source=source)

    with patch("broker.clients.apple_music.recently_played") as played:
        body = _get(client, secret, "/queue").get_json()

    assert body == {"currently_playing": None, "queue": []}
    played.assert_not_called()


def test_queue_is_the_last_played_and_the_ones_before(app, client, register_device, musickit):
    device_id, secret = register_device()
    _switch_on(app, device_id)
    songs = [song(str(i), name=f"Song {i}") for i in range(8)]

    with patch("broker.clients.apple_music.recently_played", return_value=songs):
        body = _get(client, secret, "/queue").get_json()

    assert body["currently_playing"]["name"] == "Song 0"
    assert [t["name"] for t in body["queue"]] == [f"Song {i}" for i in range(1, 6)]


def test_queue_502s_when_apple_cant_be_reached(app, client, register_device, musickit):
    device_id, secret = register_device()
    _switch_on(app, device_id)

    with patch("broker.clients.apple_music.recently_played", side_effect=requests.ConnectionError):
        assert _get(client, secret, "/queue").status_code == 502


def test_top_counts_the_logged_plays(app, client, register_device):
    device_id, secret = register_device()
    _switch_on(app, device_id)
    with app.app_context():
        for song_id in ("1", "1", "2"):
            db.session.add(
                AppleMusicPlay(
                    device_id=device_id,
                    played_at=apple_music.datetime.now(apple_music.UTC),
                    song_id=song_id,
                    artist="Flume",
                    track=apple_music.as_track(song(song_id, name=f"Song {song_id}")),
                )
            )
        db.session.commit()

    tracks = _get(client, secret, "/top/tracks?time_range=short_term&limit=5").get_json()
    artists = _get(client, secret, "/top/artists?time_range=long_term&limit=5").get_json()

    assert [t["name"] for t in tracks["items"]] == ["Song 1", "Song 2"]
    assert artists == {"items": [{"name": "Flume"}]}


def test_top_is_empty_when_switched_to_spotify(app, client, register_device):
    device_id, secret = register_device()
    _switch_on(app, device_id, source="spotify")

    assert _get(client, secret, "/top/tracks").get_json() == {"items": []}


def test_developer_token_404s_when_not_set_up(paired_client):
    client, _device_id, _ = paired_client

    assert client.get("/device/apple-music/developer-token").status_code == 404


def test_developer_token_for_musickit_js(paired_client, musickit):
    client, _device_id, _ = paired_client

    body = client.get("/device/apple-music/developer-token").get_json()

    assert body["app_name"] == "Countdown"
    assert body["developer_token"].count(".") == 2


def test_connect_stores_the_token_encrypted_and_disconnect_forgets_it_and_the_plays(app, paired_client, musickit):
    client, device_id, _ = paired_client

    assert client.post("/device/apple-music/connect", data={"music_user_token": " mut "}).status_code == 302
    with app.app_context():
        stored = db.session.get(AppleMusicToken, device_id)
        assert stored.token_encrypted != "mut"
        assert stored.token == "mut"
    assert b"Connected" in client.get("/device").data

    client.post("/device/apple-music/disconnect")
    with app.app_context():
        assert db.session.get(AppleMusicToken, device_id) is None


def test_connect_needs_a_token(paired_client):
    client, _device_id, _ = paired_client

    assert client.post("/device/apple-music/connect", data={}).status_code == 400


def test_another_users_device_404s(client, register_device):
    device_id, _ = register_device()

    assert client.post(f"/device/{device_id}/apple-music/connect", data={"music_user_token": "x"}).status_code == 404


def test_the_config_page_saves_the_music_source_and_sends_it(app, paired_client, musickit):
    client, device_id, secret = paired_client

    page = client.get("/device").data
    assert b'value="apple_music"' in page
    client.post("/device", data={"interval": "20", "spotify_enabled": "on", "music_source": "apple_music"})

    with app.app_context():
        assert db.session.get(DeviceConfig, device_id).music_source == "apple_music"
    config = client.get("/api/config", headers={"Authorization": f"Bearer {secret}"}).get_json()
    assert config["spotify"]["source"] == "apple_music"


def test_the_config_page_hides_apple_music_when_not_set_up(paired_client):
    client, _device_id, _ = paired_client

    assert b'value="apple_music"' not in client.get("/device").data


def test_the_config_page_refuses_an_unknown_music_source(paired_client):
    client, _device_id, _ = paired_client

    response = client.post("/device", data={"interval": "20", "music_source": "tidal"})

    assert response.status_code == 400
    assert b"The music service must be one of" in response.data
