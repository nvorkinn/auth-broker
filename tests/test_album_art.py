"""The covers the music provider stores (broker/album_art.py): which size, under which key, and when it's fetched.

The contract's music_art.json is the keys stored for music_player.json, each holding music_art.jpg's bytes in these
tests: pi-dashboard's renderer tests read copies of the same files, and find the covers by the same keys.
"""

import re
from unittest.mock import patch

import fakeredis
import pytest
import redis
import requests
import responses
from countdown_api import fixtures

from broker import album_art
from broker.album_art import ART_TTL_S, MAX_ART_BYTES, AlbumArt, art_key, art_url, art_urls

PLAYER = fixtures.load("music_player.json")
COVERS = fixtures.load("music_art.json")
ART = fixtures.read_bytes("music_art.jpg")
SPOTIFY_URL = COVERS["spotify"]["url"]
APPLE_URL = COVERS["apple_music"]["url"]


def track(*sizes, url="https://i.scdn.co/image/{}"):
    return {"name": "A Song", "album": {"name": "An Album", "images": [image(size, url) for size in sizes]}}


def image(size, url="https://i.scdn.co/image/{}"):
    return {"width": size, "height": size, "url": url.format(size)}


@pytest.fixture
def server():
    return fakeredis.FakeServer()


@pytest.fixture
def store(server):
    return fakeredis.FakeRedis(server=server, decode_responses=True)


@pytest.fixture
def binary(server):
    """Return the same store as the renderer reads the covers: values as bytes."""
    return fakeredis.FakeRedis(server=server)


@pytest.fixture
def host():
    """Mock the image hosts: music_art.jpg for the fixture's covers; any other request fails."""
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mock:
        for url in (SPOTIFY_URL, APPLE_URL):
            mock.add(responses.GET, url, body=ART, content_type="image/jpeg")
        yield mock


@pytest.fixture
def art(store):
    return AlbumArt(store)


@pytest.fixture
def logger():
    # Patched rather than caplog: other tests (alembic's logging set-up) can leave loggers disabled.
    with patch.object(album_art, "logger") as mock:
        yield mock


# --- The contract -------------------------------------------------------------------


@pytest.mark.parametrize("service", ["spotify", "apple_music"])
def test_the_stored_cover_is_the_one_the_renderer_reads(service):
    """The size pi-dashboard's store.music.art_image picks, under the key its store.keys.album_art makes."""
    url = art_url(PLAYER[service]["currently_playing"])

    assert url == COVERS[service]["url"]
    assert art_key(url) == COVERS[service]["key"]


def test_the_key_is_the_sha1_of_the_url_exactly_as_given():
    assert art_key("https://i.scdn.co/image/ab01300") == "v1:art:f0c75b31e316838c7115ebf3205ef753eb4a0353"
    assert art_key("https://example.com") != art_key("https://example.com/")


def test_a_cycle_stores_each_playing_tracks_cover_as_raw_bytes(art, binary, host):
    art.keep(art_urls(PLAYER))

    for service in ("spotify", "apple_music"):
        key = COVERS[service]["key"]
        assert binary.get(key) == ART  # the file itself: not base64, not JSON
        assert 0 < binary.ttl(key) <= ART_TTL_S
    assert sorted(binary.keys("v1:art:*")) == sorted(cover["key"].encode() for cover in COVERS.values())
    assert art.fetched == 2


# --- Which size ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("sizes", "stored"),
    [
        ((640, 300, 64), 300),  # Spotify's: the next size up from 120, not the closer but smaller 64
        ((64, 640, 300), 300),
        ((300,), 300),  # Apple Music's one
        ((120, 300), 120),
        ((64, 32), 64),  # none tall enough: the tallest
    ],
)
def test_the_smallest_image_at_least_120_px_tall_is_stored(sizes, stored):
    assert art_url(track(*sizes)) == f"https://i.scdn.co/image/{stored}"


@pytest.mark.parametrize(
    "playing",
    [
        None,
        PLAYER["spotify"]["queue"][1],  # a local file: no images
        {"name": "An Episode", "type": "episode", "images": [image(640)]},  # a podcast: no album
        {"album": {"images": [{"url": "https://i.scdn.co/image/x"}]}},  # no size
        {"album": None},
        "not a track",
    ],
)
def test_a_track_without_a_cover_has_none_to_store(playing):
    assert art_url(playing) is None


def test_the_covers_shown_are_each_services_playing_tracks_once():
    both_the_same = {"spotify": PLAYER["apple_music"], "apple_music": PLAYER["apple_music"]}

    assert art_urls(PLAYER) == [SPOTIFY_URL, APPLE_URL]
    assert art_urls(both_the_same) == [APPLE_URL]
    assert art_urls({"spotify": {"currently_playing": None, "queue": [track(300)]}}) == []  # not what's next


# --- Fetched once, then kept --------------------------------------------------------


def test_screens_showing_the_same_album_share_one_fetch(art, binary, host):
    art.keep([SPOTIFY_URL])
    art.keep([SPOTIFY_URL])  # another device, the same cycle

    assert len(host.calls) == 1
    assert binary.keys("v1:art:*") == [COVERS["spotify"]["key"].encode()]


def test_a_cover_still_shown_is_renewed_not_fetched_again(art, binary, host):
    art.keep([SPOTIFY_URL])
    binary.expire(COVERS["spotify"]["key"], 10)  # most of its TTL gone

    art.new_cycle()
    art.keep([SPOTIFY_URL])

    assert len(host.calls) == 1
    assert binary.ttl(COVERS["spotify"]["key"]) > ART_TTL_S - 5
    assert art.fetched == 0


def test_a_cover_that_lapsed_or_was_evicted_is_fetched_again(art, binary, host):
    art.keep([SPOTIFY_URL])
    binary.delete(COVERS["spotify"]["key"])

    art.new_cycle()
    art.keep([SPOTIFY_URL])

    assert len(host.calls) == 2
    assert binary.get(COVERS["spotify"]["key"]) == ART


# --- Failures: the cover goes without, nothing else does ----------------------------


@pytest.mark.parametrize(
    ("answer", "says"),
    [
        ({"status": 404}, "couldn't fetch"),
        ({"body": b"<html>", "content_type": "text/html"}, "isn't an image"),
        ({"body": b"x" * (MAX_ART_BYTES + 1), "content_type": "image/jpeg"}, "is over"),
    ],
)
def test_a_cover_that_cant_be_stored_is_left_out_and_tried_again_next_cycle(art, binary, host, logger, answer, says):
    host.replace(responses.GET, SPOTIFY_URL, **answer)

    art.keep([SPOTIFY_URL, APPLE_URL])

    assert binary.get(COVERS["spotify"]["key"]) is None
    assert binary.get(COVERS["apple_music"]["key"]) == ART  # the other one's unaffected
    assert says in logger.warning.call_args.args[0]
    host.replace(responses.GET, SPOTIFY_URL, body=ART, content_type="image/jpeg")
    art.new_cycle()
    art.keep([SPOTIFY_URL])
    assert binary.get(COVERS["spotify"]["key"]) == ART


def test_a_host_that_doesnt_answer_is_tried_once_a_cycle(art, binary, host, logger):
    """So a host that's down costs one timeout a cycle, not one per cover."""
    other = "https://i.scdn.co/image/other300"
    host.replace(responses.GET, SPOTIFY_URL, body=requests.ConnectTimeout("timed out"))
    host.add(responses.GET, other, body=ART, content_type="image/jpeg")

    art.keep([SPOTIFY_URL, other, APPLE_URL])

    assert [call.request.url for call in host.calls] == [SPOTIFY_URL, APPLE_URL]  # not i.scdn.co again
    assert binary.get(art_key(other)) is None
    assert binary.get(COVERS["apple_music"]["key"]) == ART  # another host
    message, unreachable, _ = logger.warning.call_args.args
    assert "couldn't reach" in message
    assert unreachable == "i.scdn.co"

    art.new_cycle()
    art.keep([other])
    assert binary.get(art_key(other)) == ART


@pytest.mark.parametrize(
    "url",
    [
        "http://i.scdn.co/image/ab01300",
        "file:///etc/hosts",
        "https:///no-host",
        "nonsense",
        "https://[i.scdn.co/image/ab01300",  # urlsplit can't parse it (an unclosed IPv6 bracket)
    ],
)
def test_only_an_https_url_is_fetched(art, binary, host, logger, url):
    art.keep([url])

    assert len(host.calls) == 0
    assert binary.keys("v1:art:*") == []
    logger.warning.assert_called_once()


def test_a_slow_host_is_given_up_on(art, host):
    """The timeout is short: a cycle has every device's player to fetch in its minute."""
    with patch.object(art.session, "get", side_effect=requests.ReadTimeout("slow")) as get:
        art.keep([SPOTIFY_URL])

    assert get.call_args.kwargs["timeout"] == album_art.TIMEOUT_S <= 10


def test_redis_down_raises(host):
    server = fakeredis.FakeServer()
    server.connected = False

    with pytest.raises(redis.exceptions.ConnectionError):
        AlbumArt(fakeredis.FakeRedis(server=server, decode_responses=True)).keep([SPOTIFY_URL])


def test_the_provider_writes_bytes_through_its_decoding_client(store, binary):
    """decode_responses only decodes replies: bytes written through the provider's client are stored as they are."""
    store.set("v1:art:x", ART)

    assert binary.get("v1:art:x") == ART
    assert re.fullmatch(r"v1:art:[0-9a-f]{40}", art_key(SPOTIFY_URL))
