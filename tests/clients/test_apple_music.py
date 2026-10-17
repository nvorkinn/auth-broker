import base64
import json
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock, patch

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

from broker.clients import apple_music
from broker.db import db
from broker.models import AppleMusicPlay, AppleMusicToken
from tests.conftest import apple_song as song


def _unb64(part):
    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


def test_not_configured_without_a_key(app):
    with app.app_context():
        assert not apple_music.configured()


def test_developer_token_is_an_es256_jwt_signed_by_the_key(app, musickit):
    with app.app_context():
        assert apple_music.configured()
        token = apple_music.developer_token()

    header, claims, signature = token.split(".")
    assert json.loads(_unb64(header)) == {"alg": "ES256", "kid": "KEY456"}
    body = json.loads(_unb64(claims))
    assert body["iss"] == "TEAM123"
    assert body["exp"] - body["iat"] == 12 * 3600
    raw = _unb64(signature)
    der = encode_dss_signature(int.from_bytes(raw[:32]), int.from_bytes(raw[32:]))
    musickit.verify(der, f"{header}.{claims}".encode(), ec.ECDSA(hashes.SHA256()))


def test_developer_token_is_reused(app, musickit):
    with app.app_context():
        assert apple_music.developer_token() == apple_music.developer_token()


def test_as_track_is_in_spotifys_shape():
    assert apple_music.as_track(song("1")) == {
        "name": "Efecto",
        "duration_ms": 213061,
        "artists": [{"name": "Bad Bunny"}],
        "album": {
            "name": "Un Verano Sin Ti",
            "images": [{"width": 300, "height": 300, "url": "https://is5-ssl.mzstatic.com/x/300x300bb.jpg"}],
        },
    }


def test_as_track_without_artwork_has_no_images():
    resource = song("1")
    del resource["attributes"]["artwork"]

    assert apple_music.as_track(resource)["album"]["images"] == []


@pytest.mark.parametrize(
    ("seen", "recent", "new"),
    [
        ([], ["a", "b"], ["a", "b"]),
        (["a", "b", "c"], ["a", "b", "c"], []),
        (["a", "b", "c"], ["d", "a", "b"], ["d"]),
        (["a", "b", "c"], ["e", "d", "a"], ["e", "d"]),
        # The same song again straight after: it's ahead of where the last list picks up.
        (["a", "b", "c"], ["a", "a", "b"], ["a"]),
        # More plays than a page: none of the last list is left.
        (["a", "b"], ["x", "y"], ["x", "y"]),
    ],
)
def test_new_plays(seen, recent, new):
    assert apple_music.new_plays(seen, recent) == new


def _response(status, body=None):
    return Mock(status_code=status, json=Mock(return_value=body), raise_for_status=Mock())


def test_recently_played_reads_two_pages_with_both_tokens(app, musickit):
    pages = [
        _response(200, {"data": [song("1")], "next": "/v1/me/recent/played/tracks?offset=10"}),
        _response(200, {"data": [song("2"), {"id": "ra.1", "type": "stations"}]}),
    ]
    with app.app_context(), patch("broker.clients.apple_music.requests.get", side_effect=pages) as get:
        songs = apple_music.recently_played("user-token")

    assert [s["id"] for s in songs] == ["1", "2"]
    assert get.call_args_list[1].args[0] == f"{apple_music.API_URL}/me/recent/played/tracks?offset=10"
    assert get.call_args.kwargs["headers"]["Music-User-Token"] == "user-token"
    assert get.call_args.kwargs["headers"]["Authorization"].startswith("Bearer ")


def _link(app, device_id, seen=()):
    with app.app_context():
        stored = AppleMusicToken(device_id=device_id, seen_ids=list(seen))
        stored.token = "user-token"
        db.session.add(stored)
        db.session.commit()


def test_recent_tracks_logs_only_the_new_plays(app, musickit, register_device):
    device_id, _ = register_device()
    _link(app, device_id, seen=["1", "2"])

    with app.app_context():
        with patch("broker.clients.apple_music.recently_played", return_value=[song("3"), song("1"), song("2")]):
            tracks = apple_music.recent_tracks(device_id)
        plays = db.session.query(AppleMusicPlay).all()
        assert db.session.get(AppleMusicToken, device_id).seen_ids == ["3", "1", "2"]

    assert len(tracks) == 3
    assert [play.song_id for play in plays] == ["3"]


def test_a_refused_token_is_cleared_but_the_log_kept(app, musickit, register_device):
    device_id, _ = register_device()
    _link(app, device_id, seen=["1"])

    with app.app_context():
        with patch("broker.clients.apple_music.recently_played", side_effect=apple_music.AppleMusicRefusedError):
            assert apple_music.recent_tracks(device_id) is None
        stored = db.session.get(AppleMusicToken, device_id)
        assert stored.token is None
        assert stored.seen_ids == ["1"]
        assert not apple_music.linked(device_id)


def test_recently_played_raises_refused_on_401(app, musickit):
    with (
        app.app_context(),
        patch("broker.clients.apple_music.requests.get", return_value=_response(401)),
        pytest.raises(apple_music.AppleMusicRefusedError),
    ):
        apple_music.recently_played("expired")


def _play(device_id, song_id, artist, days_ago):
    return AppleMusicPlay(
        device_id=device_id,
        played_at=datetime.now(UTC) - timedelta(days=days_ago),
        song_id=song_id,
        artist=artist,
        track=apple_music.as_track(song(song_id, name=f"Song {song_id}", artist=artist)),
    )


def test_top_counts_the_logged_plays_over_the_time_range(app, register_device):
    device_id, _ = register_device()
    _link(app, device_id)
    with app.app_context():
        db.session.add_all(
            [
                _play(device_id, "1", "Flume", 1),
                _play(device_id, "2", "Beach Bunny", 2),
                _play(device_id, "2", "Beach Bunny", 3),
                _play(device_id, "3", "Flume", 4),
                # Older than four weeks: only the longer ranges count it.
                _play(device_id, "1", "Flume", 60),
                _play(device_id, "1", "Flume", 61),
            ]
        )
        db.session.commit()

        tracks = apple_music.top(device_id, "tracks", "short_term", 5)
        artists = apple_music.top(device_id, "artists", "short_term", 5)
        long_tracks = apple_music.top(device_id, "tracks", "long_term", 1)

    assert [t["name"] for t in tracks["items"]] == ["Song 2", "Song 1", "Song 3"]
    assert artists == {"items": [{"name": "Flume"}, {"name": "Beach Bunny"}]}
    assert [t["name"] for t in long_tracks["items"]] == ["Song 1"]


def test_top_is_none_when_not_linked(app, register_device):
    device_id, _ = register_device()

    with app.app_context():
        assert apple_music.top(device_id, "tracks", "short_term", 5) is None
