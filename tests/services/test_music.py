"""The music area's Apple Music data (broker/services/music.py), which the music provider writes to the store.

How the plays are told apart is in tests/clients/test_apple_music.py; that the provider logs them is in
tests/providers/test_music_provider.py.
"""

from datetime import UTC, datetime
from unittest.mock import patch

import pytest
import requests

from broker.clients import apple_music
from broker.db import db
from broker.models import AppleMusicPlay, AppleMusicToken
from broker.services import music
from tests.conftest import apple_song as song


def _connect(app, device_id):
    with app.app_context():
        stored = AppleMusicToken(device_id=device_id, seen_ids=[])
        stored.token = "user-token"
        db.session.add(stored)
        db.session.commit()


def test_nothing_playing_is_spotifys_empty_queue():
    assert music.nothing_playing() == {"currently_playing": None, "queue": []}


def _assert_nothing_playing_and_apple_not_asked(app, device_id):
    with app.app_context(), patch("broker.clients.apple_music.recently_played") as played:
        assert music.apple_music_queue(device_id) == music.nothing_playing()
        assert db.session.query(AppleMusicPlay).count() == 0
    played.assert_not_called()


def test_the_queue_is_nothing_playing_without_a_musickit_key(app, register_device):
    device_id, _ = register_device()
    _connect(app, device_id)

    _assert_nothing_playing_and_apple_not_asked(app, device_id)


def test_the_queue_is_nothing_playing_when_not_connected(app, register_device, musickit):
    device_id, _ = register_device()

    _assert_nothing_playing_and_apple_not_asked(app, device_id)


def test_the_queue_is_the_last_played_and_the_ones_before(app, register_device, musickit):
    device_id, _ = register_device()
    _connect(app, device_id)
    songs = [song(str(i), name=f"Song {i}") for i in range(8)]

    with app.app_context(), patch("broker.clients.apple_music.recently_played", return_value=songs):
        queue = music.apple_music_queue(device_id)

    assert queue["currently_playing"]["name"] == "Song 0"
    assert [t["name"] for t in queue["queue"]] == [f"Song {i}" for i in range(1, 1 + music.EARLIER)]


def test_the_queue_raises_when_apple_cant_be_reached(app, register_device, musickit):
    """So the provider writes nothing for it, and the renderer keeps what it last showed."""
    device_id, _ = register_device()
    _connect(app, device_id)

    with (
        app.app_context(),
        patch("broker.clients.apple_music.recently_played", side_effect=requests.ConnectionError),
        pytest.raises(requests.RequestException),
    ):
        music.apple_music_queue(device_id)


def test_the_top_lists_count_the_logged_plays(app, register_device):
    device_id, _ = register_device()
    _connect(app, device_id)
    with app.app_context():
        for song_id in ("1", "1", "2"):
            db.session.add(
                AppleMusicPlay(
                    device_id=device_id,
                    played_at=datetime.now(UTC),
                    song_id=song_id,
                    artist="Flume",
                    track=apple_music.as_track(song(song_id, name=f"Song {song_id}")),
                )
            )
        db.session.commit()

        tracks = music.apple_music_top(device_id, "tracks", "short_term", 5)
        artists = music.apple_music_top(device_id, "artists", "long_term", 5)

    assert [t["name"] for t in tracks["items"]] == ["Song 1", "Song 2"]
    assert artists == {"items": [{"name": "Flume"}]}


def test_the_top_lists_are_empty_when_not_connected(app, register_device):
    device_id, _ = register_device()

    with app.app_context():
        assert music.apple_music_top(device_id, "tracks", "short_term", 5) == {"items": []}
