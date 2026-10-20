"""The music provider (broker/spotify_provider.py): what it writes to the store, and how it copes with failures.

The store contract's music_player.json and music_top.json (countdown_api.fixtures) are the stored values: pi-dashboard's
renderer tests read the same two files, so a change to either side that breaks the other shows up in both. So is
music_art.json, the covers stored for music_player.json (each music_art.jpg's bytes here); tests/test_album_art.py has
the rest of the covers' tests.
"""

import json
import threading
import time
from datetime import UTC, datetime
from unittest.mock import Mock, patch

import fakeredis
import pytest
import requests
import responses
from countdown_api import fixtures
from sqlalchemy import select

from broker import spotify_provider
from broker.album_art import ART_TTL_S
from broker.clients import spotify as spotify_client
from broker.db import db
from broker.models import AppleMusicPlay, AppleMusicToken, DeviceConfig, SpotifyToken
from broker.services import render_jobs
from broker.services.render_jobs import JOBS, SCREENS, queued_key
from broker.spotify_provider import (
    BURST_INTERVAL_S,
    INTERVAL_S,
    PACE_S,
    PACE_TTL_S,
    PLAYER_TTL_S,
    TOP_INTERVAL_S,
    TOP_TTL_S,
    MusicProvider,
    next_tick,
    pace_key,
    player_key,
    top_key,
)
from tests.conftest import apple_song

PLAYER = fixtures.load("music_player.json")
TOP = fixtures.load("music_top.json")
COVERS = fixtures.load("music_art.json")
ART = fixtures.read_bytes("music_art.jpg")
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


def playback_answer(*, progress_ms=None, is_playing=None, item=None):
    """Return Spotify's /me/player/currently-playing answer for the fixture's player: its track and its position."""
    position = PLAYER["spotify_playback"]
    return {
        "item": PLAYER["spotify"]["currently_playing"] if item is None else item,
        "progress_ms": position["progress_ms"] if progress_ms is None else progress_ms,
        "is_playing": position["is_playing"] if is_playing is None else is_playing,
        "currently_playing_type": "track",
    }


class FakeSpotify:
    """Spotify's Web API and token endpoint, as far as the provider calls them, answering from the fixture."""

    def __init__(self, queue=None, *, playback=None, revoked=frozenset(), top_429=None):
        self.queue = PLAYER["spotify"] if queue is None else queue
        # What /me/player/currently-playing answers: Spotify's own shape, the fixture's track and position by default.
        # "204" for nothing playing, "fail" for a server error.
        self.playback = playback_answer() if playback is None else playback
        self.revoked = revoked
        # A 429 for the top lists, with these headers (e.g. {"Retry-After": "120"}), while set.
        self.top_429 = top_429
        self.gets: list[tuple[str, dict | None]] = []
        self.refreshes: list[str] = []

    def get(self, url, headers=None, params=None, timeout=None):  # noqa: ARG002
        self.gets.append((url, params))
        if url == f"{SPOTIFY_API}/me/player/queue":
            return _response(None, 204) if self.queue == "204" else _response(self.queue)
        if url == f"{SPOTIFY_API}/me/player/currently-playing":
            if self.playback == "fail":
                return _response({"error": {"status": 500}}, 500)
            return _response(None, 204) if self.playback == "204" else _response(self.playback)
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


class Clock:
    """The provider's clock, which only moves when a test says."""

    def __init__(self, now=FETCHED_AT):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def provider(app, store, clock):
    return MusicProvider(app, store, clock=clock)


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
    """Each service's queue and top lists in Spotify's shapes, wrapped with when they were fetched.

    Spotify's are its own answers, asked for as the renderer shows them (each of TIME_RANGES, five of each); Apple
    Music's are made to look like them (broker/services/music.py).
    """
    device_id = device(apple_music=True)
    fake = FakeSpotify()

    assert _run(provider, fake) == 2

    assert _stored(store, player_key(device_id)) == PLAYER
    assert _stored(store, top_key(device_id)) == TOP
    assert 0 < store.ttl(player_key(device_id)) <= PLAYER_TTL_S
    assert PLAYER_TTL_S < store.ttl(top_key(device_id)) <= TOP_TTL_S
    assert (f"{SPOTIFY_API}/me/top/tracks", {"time_range": "short_term", "limit": "5"}) in fake.gets


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


def test_a_cover_that_cant_be_fetched_doesnt_cost_the_player(provider, clock, store, binary, device, image_host):
    """The renderer draws the page without it; it's tried again next cycle."""
    device_id = device()
    image_host.replace(responses.GET, COVERS["spotify"]["url"], status=500)

    assert _run(provider) == 2

    assert _stored(store, player_key(device_id))["spotify"] == PLAYER["spotify"]
    assert binary.get(COVERS["spotify"]["key"]) is None
    image_host.replace(responses.GET, COVERS["spotify"]["url"], body=ART, content_type="image/jpeg")
    clock.advance(INTERVAL_S)
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
    """Its part is there, empty: null would mean its fetch failed."""
    device_id = device(spotify=False, apple_music=True, linked=False)

    _run(provider)

    assert _stored(store, player_key(device_id))["apple_music"] == {"currently_playing": None, "queue": []}
    assert _stored(store, top_key(device_id))["apple_music"]["short_term"] == {
        "tracks": {"items": []},
        "artists": {"items": []},
    }


def _plays(app, device_id):
    with app.app_context():
        plays = db.session.scalars(
            select(AppleMusicPlay).where(AppleMusicPlay.device_id == device_id).order_by(AppleMusicPlay.id)
        )
        return [play.song_id for play in plays]


def test_apple_musics_new_plays_are_logged_once_each(app, store, device, musickit):
    """The top lists are counted from these plays (apple_music_plays), which only the provider logs.

    Every minute it reads the recently played list, which has no times: only the songs ahead of where the last read
    picks up are new (apple_music.new_plays), so the same list read again logs nothing, and the same song played
    again straight after is a second play.
    """
    device_id = device(spotify=False, apple_music=True)
    now = [FETCHED_AT]
    provider = MusicProvider(app, store, clock=lambda: now[0])
    one, two, three = (apple_song(song_id, name=f"Song {song_id}") for song_id in ("1", "2", "3"))

    _run(provider, apple_songs=[one, two])
    assert _plays(app, device_id) == ["2", "1"]  # oldest first

    now[0] += 60
    _run(provider, apple_songs=[one, two])
    assert _plays(app, device_id) == ["2", "1"]

    now[0] += 60
    _run(provider, apple_songs=[three, three, one])
    assert _plays(app, device_id) == ["2", "1", "3", "3"]
    with app.app_context():
        assert db.session.get(AppleMusicToken, device_id).seen_ids == ["3", "3", "1"]

    now[0] += TOP_INTERVAL_S
    _run(provider, apple_songs=[three, three, one])
    top = _stored(store, top_key(device_id))["apple_music"]["short_term"]["tracks"]["items"]
    assert top[0]["name"] == "Song 3"
    assert sorted(track["name"] for track in top) == ["Song 1", "Song 2", "Song 3"]
    assert _plays(app, device_id) == ["2", "1", "3", "3"]


def test_apple_music_switched_off_is_not_read_or_logged(app, provider, device, musickit):
    """Even with a token: plays are only logged while the screen shows Apple Music."""
    device_id = device(apple_music=False)

    with (
        patch("broker.clients.spotify.requests.get", side_effect=FakeSpotify().get),
        patch("broker.clients.apple_music.recently_played") as played,
    ):
        provider.run_cycle()

    played.assert_not_called()
    assert _plays(app, device_id) == []


def test_one_devices_revoked_token_doesnt_stop_the_others(provider, store, device):
    revoked = device(refresh_token="revoked", expires_in=-100)
    fine = device(refresh_token="fine")
    fake = FakeSpotify(revoked={"revoked"})

    assert _run(provider, fake) == 2

    assert store.get(player_key(revoked)) is None
    assert store.get(top_key(revoked)) is None
    assert _stored(store, player_key(fine)) == {**PLAYER, "apple_music": None}
    # Only the player's refresh is tried (the position's, then the queue's): the top lists would only fail the same way.
    assert fake.refreshes == ["revoked", "revoked"]


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


def test_refreshed_tokens_are_stored_for_the_next_calls(app, provider, device):
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


def test_a_failed_top_fetch_is_tried_again_next_cycle(app, store, clock, device):
    """Any failure but a 429 (which waits for Retry-After, see below) is tried again on the next cycle."""
    device_id = device()
    provider = MusicProvider(app, store, clock=clock)
    failing = FakeSpotify()
    original = failing.get

    def get(url, **kwargs):
        if "/me/top/" in url:
            return _response({"error": "server error"}, 500)
        return original(url, **kwargs)

    failing.get = get
    _run(provider, failing)
    assert store.get(top_key(device_id)) is None

    clock.advance(INTERVAL_S)
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


# --- while Spotify plays: the position, every BURST_INTERVAL_S ---------------------------------------------------


def _pool(store, *device_ids):
    """Put screens in the render pool, as services/render_screens.py does."""
    store.hset(SCREENS, mapping={device_id: f"screen-{device_id}" for device_id in device_ids})


def _jobs(store):
    """Return the render pool's queue, and take its jobs as the pool's worker would (pop, then claim: the flag goes)."""
    jobs = store.lrange(JOBS, 0, -1)
    store.delete(JOBS, *(queued_key(device_id) for device_id in jobs))
    return jobs


def _urls(fake):
    return [url.removeprefix(SPOTIFY_API) for url, _ in fake.gets]


def test_the_position_is_stored_with_the_time_it_was_fetched(provider, store, device):
    device_id = device()

    _run(provider)

    assert _stored(store, player_key(device_id))["spotify_playback"] == PLAYER["spotify_playback"]


def test_a_playing_track_asks_for_a_faster_refresh_and_queues_a_render(provider, store, device):
    device_id = device()
    _pool(store, device_id)

    _run(provider)

    assert store.get(pace_key(device_id)) == str(PACE_S)
    assert 0 < store.ttl(pace_key(device_id)) <= PACE_TTL_S
    assert _jobs(store) == [device_id]
    assert provider.interval_s() == BURST_INTERVAL_S


def test_between_minutes_only_the_position_is_fetched_and_written(provider, clock, store, device):
    device_id = device()
    _pool(store, device_id)
    _run(provider)
    _jobs(store)
    clock.advance(BURST_INTERVAL_S)
    fake = FakeSpotify(playback=playback_answer(progress_ms=81234))

    assert _run(provider, fake) == 1

    assert _urls(fake) == ["/me/player/currently-playing"]
    stored = _stored(store, player_key(device_id))
    # The queue is the last full cycle's, with its time; the position is new.
    assert stored["fetched_at"] == PLAYER["fetched_at"]
    assert stored["spotify"] == PLAYER["spotify"]
    assert stored["spotify_playback"] == {
        "fetched_at": datetime.fromtimestamp(clock(), UTC).isoformat(),
        "is_playing": True,
        "progress_ms": 81234,
        "track_id": "tab01",
    }
    assert store.get(pace_key(device_id)) == str(PACE_S)
    assert _jobs(store) == [device_id]


def test_the_minute_fetches_everything_again(provider, clock, store, device):
    device_id = device()
    _run(provider)
    clock.advance(BURST_INTERVAL_S)
    _run(provider)
    clock.advance(BURST_INTERVAL_S)
    _run(provider)
    clock.advance(BURST_INTERVAL_S)
    fake = FakeSpotify()

    _run(provider, fake)

    assert _urls(fake)[:2] == ["/me/player/currently-playing", "/me/player/queue"]
    assert _stored(store, player_key(device_id))["fetched_at"] == datetime.fromtimestamp(clock(), UTC).isoformat()


def test_a_new_track_fetches_the_queue_at_once(provider, clock, store, device):
    device_id = device()
    _run(provider)
    clock.advance(BURST_INTERVAL_S)
    other = {**PLAYER["spotify"]["currently_playing"], "id": "tab02", "name": "Please Please Please"}
    fake = FakeSpotify(
        queue={"currently_playing": other, "queue": []}, playback=playback_answer(item=other, progress_ms=900)
    )

    _run(provider, fake)

    assert "/me/player/queue" in _urls(fake)
    stored = _stored(store, player_key(device_id))
    assert stored["spotify"]["currently_playing"]["id"] == "tab02"
    assert stored["spotify_playback"]["track_id"] == "tab02"


def test_paused_ends_the_burst_with_one_last_render(provider, clock, store, device):
    device_id = device()
    _pool(store, device_id)
    _run(provider)
    _jobs(store)
    clock.advance(BURST_INTERVAL_S)

    _run(provider, FakeSpotify(playback=playback_answer(is_playing=False)))

    stored = _stored(store, player_key(device_id))
    assert stored["spotify_playback"]["is_playing"] is False
    assert store.get(pace_key(device_id)) is None
    assert _jobs(store) == [device_id]
    assert provider.interval_s() == INTERVAL_S
    # And no more renders from this provider while it stays paused.
    clock.advance(INTERVAL_S)
    _run(provider, FakeSpotify(playback=playback_answer(is_playing=False)))
    assert _jobs(store) == []


def test_nothing_playing_has_no_position_and_no_pace(provider, store, device):
    device_id = device()
    _pool(store, device_id)

    _run(provider, FakeSpotify(queue="204", playback="204"))

    assert _stored(store, player_key(device_id))["spotify_playback"] is None
    assert store.get(pace_key(device_id)) is None
    assert _jobs(store) == []
    assert provider.interval_s() == INTERVAL_S


def test_stopping_between_minutes_fetches_the_queue_and_queues_a_last_render(provider, clock, store, device):
    device_id = device()
    _pool(store, device_id)
    _run(provider)
    _jobs(store)
    clock.advance(BURST_INTERVAL_S)

    _run(provider, FakeSpotify(queue="204", playback="204"))

    stored = _stored(store, player_key(device_id))
    assert stored["spotify"] == {"currently_playing": None, "queue": []}
    assert stored["spotify_playback"] is None
    assert _jobs(store) == [device_id]
    assert provider.interval_s() == INTERVAL_S


def test_a_position_that_cant_be_fetched_leaves_the_last_one_and_the_pace(provider, clock, store, device):
    device_id = device()
    _run(provider)
    before = store.get(player_key(device_id))
    clock.advance(BURST_INTERVAL_S)

    assert _run(provider, FakeSpotify(playback="fail")) == 0

    assert store.get(player_key(device_id)) == before
    assert store.get(pace_key(device_id)) == str(PACE_S)
    assert provider.interval_s() == BURST_INTERVAL_S


def test_an_episode_has_no_position_to_show(provider, store, device):
    device_id = device()

    _run(provider, FakeSpotify(playback={"item": None, "progress_ms": 5000, "is_playing": True}))

    assert _stored(store, player_key(device_id))["spotify_playback"] is None
    assert store.get(pace_key(device_id)) is None


def test_a_device_no_longer_wanted_stops_the_burst(app, provider, clock, store, device):
    device_id = device()
    _run(provider)
    assert provider.interval_s() == BURST_INTERVAL_S
    with app.app_context():
        db.session.get(DeviceConfig, device_id).spotify_enabled = False
        db.session.commit()
    clock.advance(BURST_INTERVAL_S)

    _run(provider)

    assert provider.interval_s() == INTERVAL_S


def test_apple_music_alone_never_bursts_and_is_fetched_once_a_minute(provider, clock, store, device, musickit):
    device_id = device(spotify=False, apple_music=True)
    _run(provider)
    clock.advance(BURST_INTERVAL_S)
    fake = FakeSpotify()

    assert _run(provider, fake) == 0

    assert fake.gets == []
    assert provider.interval_s() == INTERVAL_S
    assert _stored(store, player_key(device_id))["spotify_playback"] is None
    assert store.get(pace_key(device_id)) is None


def test_a_render_job_that_cant_be_queued_doesnt_cost_the_position(provider, store, device):
    device_id = device()
    _pool(store, device_id)
    with patch.object(render_jobs, "enqueue", side_effect=lambda *_: 0):
        _run(provider)

    assert _stored(store, player_key(device_id))["spotify_playback"] is not None
