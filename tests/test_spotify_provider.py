"""The music provider (broker/spotify_provider.py): what it writes to the store, and how it copes with failures.

tests/fixtures/music_player.json and music_top.json are the stored contract: pi-dashboard's renderer tests read the
same two files (copied into its tests), so a change to either side that breaks the other shows up in both. So is
music_art.json, the covers stored for music_player.json (each music_art.jpg's bytes here); tests/test_album_art.py has
the rest of the covers' tests.
"""

import json
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import Mock, patch

import fakeredis
import pytest
import requests
import responses

from broker import spotify_provider
from broker.album_art import ART_TTL_S
from broker.clients import spotify as spotify_client
from broker.db import db
from broker.models import AppleMusicToken, DeviceConfig, SpotifyToken
from broker.spotify_provider import (
    PLAYER_TTL_S,
    TOP_INTERVAL_S,
    TOP_TTL_S,
    MusicProvider,
    next_tick,
    player_key,
    top_key,
)
from tests.conftest import apple_song

FIXTURES = Path(__file__).parent / "fixtures"
PLAYER = json.loads((FIXTURES / "music_player.json").read_text())
TOP = json.loads((FIXTURES / "music_top.json").read_text())
COVERS = json.loads((FIXTURES / "music_art.json").read_text())
ART = (FIXTURES / "music_art.jpg").read_bytes()
FETCHED_AT = datetime.fromisoformat(PLAYER["fetched_at"]).timestamp()
SPOTIFY_API = "https://api.spotify.com/v1"
APPLE_SONGS = [apple_song("1", name="Efecto"), apple_song("2", name="Tití Me Preguntó")]


def _response(body=None, status_code=200, headers=None):
    response = Mock()
    response.status_code = status_code
    response.headers = headers or {}
    response.content = b"" if body is None else json.dumps(body).encode()
    response.json.return_value = body
    if status_code >= 400:
        response.raise_for_status.side_effect = requests.HTTPError(f"{status_code}", response=response)
    return response


class FakeSpotify:
    """Spotify's Web API and token endpoint, as far as the provider calls them, answering from the fixture."""

    def __init__(self, queue=None, *, revoked=frozenset(), top_429=None):
        self.queue = PLAYER["spotify"] if queue is None else queue
        self.revoked = revoked
        # A 429 for the top lists, with these headers (e.g. {"Retry-After": "120"}), while set.
        self.top_429 = top_429
        self.gets: list[tuple[str, dict | None]] = []
        self.refreshes: list[str] = []

    def get(self, url, headers=None, params=None, timeout=None):  # noqa: ARG002
        self.gets.append((url, params))
        if url == f"{SPOTIFY_API}/me/player/queue":
            return _response(None, 204) if self.queue == "204" else _response(self.queue)
        if self.top_429 is not None:
            return _response({"error": {"status": 429}}, 429, self.top_429)
        type_ = url.removeprefix(f"{SPOTIFY_API}/me/top/")
        return _response(TOP["spotify"][params["time_range"]][type_])

    def post(self, url, data=None, auth=None, timeout=None):  # noqa: ARG002
        assert url == spotify_client.TOKEN_URL
        self.refreshes.append(data["refresh_token"])
        if data["refresh_token"] in self.revoked:
            return _response({"error": "invalid_grant"}, 400)
        return _response({"access_token": f"new-for-{data['refresh_token']}", "expires_in": 3600})


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


@pytest.fixture(autouse=True)
def image_host():
    """Mock the covers' hosts: music_art.jpg for the fixture's covers; any other request fails.

    Their calls go through the provider's own session, which the patched requests.get doesn't reach.
    """
    with responses.RequestsMock(assert_all_requests_are_fired=False) as host:
        for cover in COVERS.values():
            host.add(responses.GET, cover["url"], body=ART, content_type="image/jpeg")
        yield host


@pytest.fixture
def provider(app, store):
    return MusicProvider(app, store, clock=lambda: FETCHED_AT)


@pytest.fixture
def device(app, register_device):
    """Make a device with the given services switched on (and linked); return its id."""

    def _make(*, spotify=True, apple_music=False, linked=True, refresh_token="refresh", expires_in=3600):  # noqa: S107
        device_id, _ = register_device(f"a-very-long-device-secret-{refresh_token}")
        with app.app_context():
            config = db.session.get(DeviceConfig, device_id)
            config.spotify_enabled = spotify
            config.apple_music_enabled = apple_music
            if linked:
                db.session.add(
                    SpotifyToken(
                        device_id=device_id,
                        refresh_token=refresh_token,
                        access_token="access",
                        expires_at=time.time() + expires_in,
                    )
                )
                apple = AppleMusicToken(device_id=device_id, seen_ids=[])
                apple.token = "user-token"
                db.session.add(apple)
            db.session.commit()
        return device_id

    return _make


def _run(provider, fake_spotify=None, apple_songs=APPLE_SONGS):
    fake_spotify = fake_spotify or FakeSpotify()
    with (
        patch("broker.clients.spotify.requests.get", side_effect=fake_spotify.get),
        patch("broker.clients.spotify.requests.post", side_effect=fake_spotify.post),
        patch("broker.clients.apple_music.recently_played", return_value=apple_songs),
    ):
        return provider.run_cycle()


def _stored(store, key):
    value = store.get(key)
    return None if value is None else json.loads(value)


def test_writes_the_player_and_the_top_lists_in_the_stored_shape(app, provider, store, device, musickit):
    """Both services' parts are what their proxy routes answer, wrapped with when they were fetched."""
    device_id = device(apple_music=True)

    assert _run(provider) == 2

    assert _stored(store, player_key(device_id)) == PLAYER
    assert _stored(store, top_key(device_id)) == TOP
    assert 0 < store.ttl(player_key(device_id)) <= PLAYER_TTL_S
    assert PLAYER_TTL_S < store.ttl(top_key(device_id)) <= TOP_TTL_S


def test_the_covers_are_stored_under_the_keys_the_renderer_reads(provider, binary, device, musickit, image_host):
    """Each service's playing track's (Apple Music's last played), as the image's bytes, by its URL."""
    device(apple_music=True)

    _run(provider)

    for service in ("spotify", "apple_music"):
        assert binary.get(COVERS[service]["key"]) == ART
        assert PLAYER_TTL_S < binary.ttl(COVERS[service]["key"]) <= ART_TTL_S
    assert sorted(call.request.url for call in image_host.calls) == sorted(c["url"] for c in COVERS.values())


def test_the_covers_are_stored_before_the_player_key_that_shows_them(provider, store, device):
    """So a renderer never reads a track whose cover isn't there yet."""
    device_id = device()
    keep = provider.art.keep
    player_there = []

    def spy(urls):
        player_there.append(store.exists(player_key(device_id)))
        keep(urls)

    provider.art.keep = spy
    _run(provider)

    assert player_there == [0]
    assert store.exists(COVERS["spotify"]["key"], player_key(device_id)) == 2


def test_screens_playing_the_same_album_share_one_cover(provider, binary, device, image_host):
    device(refresh_token="one")
    device(refresh_token="two")

    assert _run(provider) == 4

    assert len(image_host.calls) == 1
    assert binary.keys("v1:art:*") == [COVERS["spotify"]["key"].encode()]


def test_a_cover_still_playing_is_renewed_each_minute_not_fetched_again(app, binary, device, image_host, store):
    device()
    now = [FETCHED_AT]
    provider = MusicProvider(app, store, clock=lambda: now[0])
    _run(provider)
    binary.expire(COVERS["spotify"]["key"], 30)

    now[0] += 60
    _run(provider)

    assert len(image_host.calls) == 1
    assert binary.ttl(COVERS["spotify"]["key"]) > ART_TTL_S - 5


def test_a_cover_that_cant_be_fetched_doesnt_cost_the_player(provider, store, binary, device, image_host):
    """The renderer draws the page without it; it's tried again next cycle."""
    device_id = device()
    image_host.replace(responses.GET, COVERS["spotify"]["url"], status=500)

    assert _run(provider) == 2

    assert _stored(store, player_key(device_id))["spotify"] == PLAYER["spotify"]
    assert binary.get(COVERS["spotify"]["key"]) is None
    image_host.replace(responses.GET, COVERS["spotify"]["url"], body=ART, content_type="image/jpeg")
    _run(provider)
    assert binary.get(COVERS["spotify"]["key"]) == ART


def test_a_failure_storing_the_covers_doesnt_cost_the_player(provider, store, device):
    device_id = device()

    with (
        patch.object(provider.art, "keep", side_effect=ValueError("boom")),
        patch.object(spotify_provider, "logger") as logger,
    ):
        assert _run(provider) == 2

    assert _stored(store, player_key(device_id))["spotify"] == PLAYER["spotify"]
    assert "covers" in logger.exception.call_args.args[0]


def test_nothing_playing_has_no_cover(provider, store, device, image_host):
    device()

    _run(provider, FakeSpotify(queue="204"))

    assert len(image_host.calls) == 0
    assert store.keys("v1:art:*") == []


def test_the_stored_parts_are_what_the_proxy_routes_answer(app, client, provider, store, device, musickit):
    device_id = device(apple_music=True)
    _run(provider)
    fake = FakeSpotify()
    headers = {"Authorization": "Bearer a-very-long-device-secret-refresh"}
    with (
        patch("broker.clients.spotify.requests.get", side_effect=fake.get),
        patch("broker.clients.apple_music.recently_played", return_value=APPLE_SONGS),
    ):
        spotify_queue = client.get("/api/spotify/queue", headers=headers).get_json()
        spotify_top = client.get("/api/spotify/top/tracks?time_range=short_term&limit=5", headers=headers).get_json()
        apple_queue = client.get("/api/apple-music/queue", headers=headers).get_json()
        apple_top = client.get("/api/apple-music/top/artists?time_range=long_term&limit=5", headers=headers).get_json()

    assert _stored(store, player_key(device_id))["spotify"] == spotify_queue
    assert _stored(store, player_key(device_id))["apple_music"] == apple_queue
    assert _stored(store, top_key(device_id))["spotify"]["short_term"]["tracks"] == spotify_top
    assert _stored(store, top_key(device_id))["apple_music"]["long_term"]["artists"] == apple_top
    # Asked for as the renderer asks the proxy.
    assert (f"{SPOTIFY_API}/me/top/tracks", {"time_range": "short_term", "limit": "5"}) in fake.gets


def test_nothing_playing_is_stored_as_such(provider, store, device):
    """Spotify's 204 (nothing playing) is a queue with nothing playing, not a missing key."""
    device_id = device()

    _run(provider, FakeSpotify(queue="204"))

    assert _stored(store, player_key(device_id))["spotify"] == {"currently_playing": None, "queue": []}


def test_only_the_services_switched_on_are_fetched(provider, store, device):
    device_id = device()

    _run(provider)

    stored = _stored(store, player_key(device_id))
    assert stored["spotify"] == PLAYER["spotify"]
    assert stored["apple_music"] is None


def test_devices_with_music_off_or_spotify_unlinked_are_skipped(provider, store, device):
    off = device(spotify=False, refresh_token="off")
    unlinked = device(linked=False, refresh_token="unlinked")
    fake = FakeSpotify()

    assert _run(provider, fake) == 0

    assert store.keys() == []
    assert fake.gets == []
    assert {off, unlinked}.isdisjoint(store.keys())


def test_apple_music_switched_on_but_not_connected_is_nothing_playing(provider, store, device):
    """As the proxy answers it."""
    device_id = device(spotify=False, apple_music=True, linked=False)

    _run(provider)

    assert _stored(store, player_key(device_id))["apple_music"] == {"currently_playing": None, "queue": []}
    assert _stored(store, top_key(device_id))["apple_music"]["short_term"] == {
        "tracks": {"items": []},
        "artists": {"items": []},
    }


def test_one_devices_revoked_token_doesnt_stop_the_others(provider, store, device):
    revoked = device(refresh_token="revoked", expires_in=-100)
    fine = device(refresh_token="fine")
    fake = FakeSpotify(revoked={"revoked"})

    assert _run(provider, fake) == 2

    assert store.get(player_key(revoked)) is None
    assert store.get(top_key(revoked)) is None
    assert _stored(store, player_key(fine)) == {**PLAYER, "apple_music": None}
    # Only the player's refresh is tried: the top lists would only fail the same way.
    assert fake.refreshes == ["revoked"]


def test_a_failing_service_leaves_the_other_ones_part(provider, store, device, musickit):
    device_id = device(refresh_token="revoked", expires_in=-100, apple_music=True)

    _run(provider, FakeSpotify(revoked={"revoked"}))

    stored = _stored(store, player_key(device_id))
    assert stored["spotify"] is None
    assert stored["apple_music"] == PLAYER["apple_music"]


def test_a_partial_top_fetch_doesnt_replace_a_full_one(app, store, device, musickit):
    device_id = device(refresh_token="soon-revoked", apple_music=True)
    now = [FETCHED_AT]
    provider = MusicProvider(app, store, clock=lambda: now[0])
    _run(provider)
    assert _stored(store, top_key(device_id)) == TOP
    with app.app_context():
        db.session.get(SpotifyToken, device_id).expires_at = 0
        db.session.commit()

    now[0] += TOP_INTERVAL_S
    _run(provider, FakeSpotify(revoked={"soon-revoked"}))

    assert _stored(store, top_key(device_id)) == TOP  # kept, not replaced by Apple Music's alone
    assert _stored(store, player_key(device_id))["spotify"] is None


def test_refreshed_tokens_are_stored_as_the_proxy_stores_them(app, provider, device):
    device_id = device(refresh_token="old-refresh", expires_in=-100)
    fake = FakeSpotify()

    _run(provider, fake)

    assert fake.refreshes == ["old-refresh"]  # once: the top lists' calls use the refreshed token
    with app.app_context():
        token = db.session.get(SpotifyToken, device_id)
        assert token.access_token == "new-for-old-refresh"
        assert token.refresh_token == "old-refresh"  # Spotify sent no new one, so the old one is kept
        assert token.expires_at > time.time() + 3000


def test_the_top_lists_are_fetched_every_half_hour(app, store, device):
    device_id = device()
    now = [FETCHED_AT]
    provider = MusicProvider(app, store, clock=lambda: now[0])
    fake = FakeSpotify()

    def top_calls():
        return sum(1 for url, _ in fake.gets if "/me/top/" in url)

    _run(provider, fake)
    assert top_calls() == 4  # two time ranges, tracks and artists
    now[0] += 60
    _run(provider, fake)
    assert top_calls() == 4
    assert sum(1 for url, _ in fake.gets if url.endswith("/queue")) == 2
    now[0] += TOP_INTERVAL_S
    _run(provider, fake)
    assert top_calls() == 8
    assert store.get(top_key(device_id)) is not None


def test_switching_a_service_on_fetches_its_top_lists_at_once(app, provider, store, device, musickit):
    device_id = device()
    _run(provider)
    with app.app_context():
        db.session.get(DeviceConfig, device_id).apple_music_enabled = True
        db.session.commit()

    _run(provider)

    assert _stored(store, top_key(device_id))["apple_music"] == TOP["apple_music"]


def test_a_failed_top_fetch_is_tried_again_next_cycle(app, store, device):
    """Any failure but a 429 (which waits for Retry-After, see below) is tried again on the next cycle."""
    device_id = device()
    provider = MusicProvider(app, store, clock=lambda: FETCHED_AT)
    failing = FakeSpotify()
    original = failing.get

    def get(url, **kwargs):
        if "/me/top/" in url:
            return _response({"error": "server error"}, 500)
        return original(url, **kwargs)

    failing.get = get
    _run(provider, failing)
    assert store.get(top_key(device_id)) is None

    _run(provider)

    assert _stored(store, top_key(device_id))["spotify"] == TOP["spotify"]


def test_redis_down_is_logged_and_tried_next_tick(app, store, device, caplog):
    device_id = device()
    server = fakeredis.FakeServer()
    server.connected = False  # every command raises ConnectionError
    provider = MusicProvider(app, fakeredis.FakeRedis(server=server, decode_responses=True), clock=lambda: FETCHED_AT)

    # Patched rather than caplog: other tests (alembic's logging set-up) can leave loggers disabled.
    with (
        patch("broker.clients.spotify.requests.get", side_effect=FakeSpotify().get),
        patch.object(spotify_provider, "logger") as logger,
    ):
        provider._try_cycle()  # the loop's own error handling: it doesn't raise

    assert "couldn't reach Redis" in logger.warning.call_args.args[0]
    server.connected = True
    with patch("broker.clients.spotify.requests.get", side_effect=FakeSpotify().get):
        provider._try_cycle()
    # The top lists weren't counted as fetched while they couldn't be written.
    assert provider.store.get(top_key(device_id)) is not None


def test_a_nudge_on_sub_changed_ends_the_wait_early(app, store):
    provider = MusicProvider(app, store, clock=lambda: FETCHED_AT)
    assert not provider._listen(0.01)  # subscribes; nothing published yet

    store.publish(spotify_provider.SUB_CHANGED, "")

    assert provider._listen(0.5)


def test_stop_ends_the_wait(app, store):
    """Within a second, though the next tick is most of a minute away."""
    provider = MusicProvider(app, store, clock=lambda: FETCHED_AT + 1)
    threading.Timer(0.1, provider.stop).start()

    started = time.monotonic()
    provider._wait_for_next_cycle()

    assert time.monotonic() - started < 2


def test_ticks_are_epoch_aligned():
    assert next_tick(120.0, 60) == 180.0
    assert next_tick(179.9, 60) == 180.0
    assert datetime.fromtimestamp(next_tick(FETCHED_AT + 1, 60), UTC).second == 0


@pytest.mark.parametrize(("headers", "wait"), [({"Retry-After": "120"}, 120), ({}, spotify_provider.DEFAULT_BACKOFF_S)])
def test_a_429_stops_all_spotify_calls_until_retry_after(app, store, device, headers, wait):
    """Spotify's limit is the app's: once it says 429, no device's Spotify is asked again until it said to."""
    device()
    now = [FETCHED_AT]
    provider = MusicProvider(app, store, clock=lambda: now[0])
    throttled = FakeSpotify(top_429=headers)

    # Patched rather than caplog: other tests (alembic's logging set-up) can leave loggers disabled.
    with patch.object(spotify_provider, "logger") as logger:
        _run(provider, throttled)
    logger.warning.assert_called_once_with(
        "Music: %s is rate-limiting the app (429); calling it again in %ds", "spotify", wait
    )
    logger.exception.assert_not_called()  # a warning, not a stack trace each time
    assert sum("/me/top/" in url for url, _ in throttled.gets) == 1  # it stopped at the first 429

    now[0] += wait - 1
    waiting = FakeSpotify()
    _run(provider, waiting)
    assert waiting.gets == []  # neither the player nor the top lists

    now[0] += 1
    recovered = FakeSpotify()
    _run(provider, recovered)
    assert any("/me/top/" in url for url, _ in recovered.gets)


def test_other_errors_are_not_treated_as_rate_limits():
    retry_after = spotify_provider.retry_after
    assert retry_after(requests.HTTPError("500", response=_response(None, 500))) is None
    assert retry_after(ValueError("boom")) is None
    unreadable = requests.HTTPError("429", response=_response(None, 429, {"Retry-After": "soon"}))
    assert retry_after(unreadable) == spotify_provider.DEFAULT_BACKOFF_S
