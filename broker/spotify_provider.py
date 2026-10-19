"""The music provider: what each screen's Spotify and Apple Music are playing, written to the shared Redis store.

    python -m broker.spotify_provider

It runs from the broker's image with the broker's settings and database, and calls Spotify and Apple Music with the
broker's own clients (broker/clients/spotify.py, broker/services/music.py), so the tokens never leave the broker, and
a refreshed Spotify token is stored back in the database. The renderers get the music area only from the store. Reading
Apple Music also logs its new plays, which its top lists are counted from (apple_music.record_plays); nothing else logs
them. The keys and what they hold are in the countdown-api store contract.

The playing track's cover is stored too, once per image however many screens show it (broker/album_art.py), so the
renderers never call Spotify's or Apple's image hosts either.

The loop is pi-dashboard's provider runner (countdown_providers/runner.py) in miniature: a cycle at once on start,
then on every epoch-aligned tick of INTERVAL_S, or early on a `v1:sub:changed` message (a config was saved). SIGINT
and SIGTERM stop it between devices. A device whose fetch fails is logged and skipped (its keys expire if it keeps
failing); an unreachable Redis is logged and tried again on the next tick.
"""

import json
import logging
import os
import signal
import sys
import threading
import time
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from http import HTTPStatus
from typing import TYPE_CHECKING

import redis
import requests
from countdown_api import keys as store_keys
from redis.exceptions import RedisError
from sqlalchemy import or_, select

from broker.album_art import AlbumArt, art_urls
from broker.clients import spotify
from broker.db import db
from broker.models import DeviceConfig, SpotifyToken
from broker.services import music

if TYPE_CHECKING:
    from collections.abc import Callable

    from flask import Flask

# By name, not __name__, which is "__main__" when run with -m.
logger = logging.getLogger("broker.spotify_provider")

# What's playing: fetched every minute. That's as often as a renderer's music panel moves on today (and it shows the
# player only every other page), so the provider makes no more Spotify calls than the renderers it stands in for.
INTERVAL_S = 60
# Three missed cycles, then the player is gone (and the renderer's panel says it's not available). The store contract's.
PLAYER_TTL_S = store_keys.SPOTIFY_TTL_S
# The top lists change slowly: fetched every half hour, and kept for four missed fetches (the store contract's TTL).
TOP_INTERVAL_S = 30 * 60
TOP_TTL_S = store_keys.SPOTIFY_TOP_TTL_S
# What the renderer's top pages show: Spotify's time ranges, and five of each.
TIME_RANGES = ("long_term", "short_term")
TOP_LIMIT = 5
TOP_TYPES = ("tracks", "artists")

SPOTIFY = "spotify"
APPLE_MUSIC = "apple_music"

# A 429 (Too Many Requests) is the service's limit for the whole app, not one device's: none of its calls are made
# again until it says (Retry-After, in seconds), or until this long when it doesn't say.
DEFAULT_BACKOFF_S = 60

SUB_CHANGED = store_keys.SUB_CHANGED
# How long each wait for a `v1:sub:changed` message lasts, so a stop is noticed within it.
LISTEN_S = 1.0
# How long to wait before subscribing again after losing Redis.
RESUBSCRIBE_S = 5.0


def player_key(device_id: str) -> str:
    """Return the key of what a device's music services are playing (the store contract's)."""
    return store_keys.spotify(device_id)


def top_key(device_id: str) -> str:
    """Return the key of a device's top tracks and artists (the store contract's)."""
    return store_keys.spotify_top(device_id)


def next_tick(now: float, interval_s: float) -> float:
    """Return the first multiple of `interval_s` after `now` (epoch seconds): when the next cycle is due."""
    return (now // interval_s + 1) * interval_s


@dataclass(frozen=True)
class Wanted:
    """A device whose music area is on, and which of its services can be fetched."""

    device_id: str
    services: tuple[str, ...]


def wanted_devices() -> list[Wanted]:
    """Return the devices to fetch: Spotify switched on and linked, or Apple Music switched on.

    Spotify switched on but not linked has nothing to fetch. Apple Music switched on but not connected is still
    fetched, as "nothing playing".
    """
    rows = db.session.execute(
        select(
            DeviceConfig.device_id,
            DeviceConfig.spotify_enabled,
            DeviceConfig.apple_music_enabled,
            SpotifyToken.device_id.is_not(None),
        )
        .outerjoin(SpotifyToken, SpotifyToken.device_id == DeviceConfig.device_id)
        .where(or_(DeviceConfig.spotify_enabled, DeviceConfig.apple_music_enabled))
        .order_by(DeviceConfig.device_id)
    ).all()
    wanted = []
    for device_id, spotify_enabled, apple_music_enabled, linked in rows:
        services = ((SPOTIFY,) if spotify_enabled and linked else ()) + ((APPLE_MUSIC,) if apple_music_enabled else ())
        if services:
            wanted.append(Wanted(device_id, services))
    return wanted


def spotify_queue(device_id: str) -> dict[str, object]:
    """Return the device's Spotify queue, as Spotify's /me/player/queue answers it; nothing playing (204) as such."""
    # get_queue hands over [] for a 204, which the screen can't read as a queue; the store says what it means.
    return spotify.get_queue(device_id) or music.nothing_playing()


def spotify_top(device_id: str) -> dict[str, dict[str, object]]:
    """Return the device's Spotify top tracks and artists over each of TIME_RANGES, as Spotify's /me/top answers them.

    Empty lists for a 204, rather than its [].
    """
    return {
        time_range: {
            type_: spotify.get_users_top_items(device_id, type_, {"time_range": time_range, "limit": str(TOP_LIMIT)})
            or {"items": []}
            for type_ in TOP_TYPES
        }
        for time_range in TIME_RANGES
    }


def apple_music_top(device_id: str) -> dict[str, dict[str, object]]:
    """Return the device's Apple Music top tracks and artists over each of TIME_RANGES, in Spotify's /me/top shape."""
    return {
        time_range: {type_: music.apple_music_top(device_id, type_, time_range, TOP_LIMIT) for type_ in TOP_TYPES}
        for time_range in TIME_RANGES
    }


PLAYER_FETCHERS: dict[str, Callable[[str], dict]] = {SPOTIFY: spotify_queue, APPLE_MUSIC: music.apple_music_queue}
TOP_FETCHERS: dict[str, Callable[[str], dict]] = {SPOTIFY: spotify_top, APPLE_MUSIC: apple_music_top}


def stored(fetched_at: datetime, parts: dict[str, dict]) -> str:
    """Return the stored JSON: when it was fetched, and each service's part (null if not fetched)."""
    return json.dumps({"fetched_at": fetched_at.isoformat(), **{s: parts.get(s) for s in (SPOTIFY, APPLE_MUSIC)}})


@dataclass
class TopFetched:
    """When a device's top lists were last fetched in full, and for which services."""

    at: float
    services: tuple[str, ...] = field(default=())


def retry_after(error: Exception) -> int | None:
    """Return how many seconds to wait if `error` is a 429 (its Retry-After, else DEFAULT_BACKOFF_S); None otherwise."""
    response = getattr(error, "response", None) if isinstance(error, requests.HTTPError) else None
    if response is None or response.status_code != HTTPStatus.TOO_MANY_REQUESTS:
        return None
    header = (response.headers or {}).get("Retry-After", "")
    return max(int(header), 1) if str(header).isdigit() else DEFAULT_BACKOFF_S


class MusicProvider:
    """Runs the cycles against `store` (made with decode_responses=True), in `app`'s context, until stop()."""

    def __init__(
        self,
        app: Flask,
        store: redis.Redis,
        clock: Callable[[], float] = time.time,
        images: requests.Session | None = None,
    ) -> None:
        """`clock` gives the time in epoch seconds (a parameter for the tests); `images` fetches the covers."""
        self.app = app
        self.store = store
        self.clock = clock
        self.art = AlbumArt(store, images)
        self._stopping = threading.Event()
        self._top_fetched: dict[str, TopFetched] = {}
        # Service -> when it may be called again, after it answered 429.
        self._backoff_until: dict[str, float] = {}
        self._pubsub: redis.client.PubSub | None = None
        # Set while Redis is unreachable: when to try subscribing again.
        self._resubscribe_at: float | None = None

    def stop(self) -> None:
        """Stop after the device being fetched (if any). Safe to call from a signal handler."""
        self._stopping.set()

    def run(self) -> None:
        """Run cycles until stop() is called."""
        logger.info("Starting the music provider: every %ss, top lists every %ss", INTERVAL_S, TOP_INTERVAL_S)
        try:
            while not self._stopping.is_set():
                self._try_cycle()
                self._wait_for_next_cycle()
        finally:
            self._close_pubsub()
        logger.info("Stopped the music provider")

    def run_cycle(self) -> int:
        """Fetch every wanted device and write what came back; return how many of their keys were written.

        Not counting the covers (logged apart). Raises RedisError if Redis can't be written, ending the cycle; a
        failing device only loses its own keys.
        """
        self.art.new_cycle()
        with self.app.app_context():
            try:
                wanted = wanted_devices()
            finally:
                db.session.remove()
            # Forget the devices no longer wanted, so one switched back on gets its top lists at once.
            ids = {w.device_id for w in wanted}
            self._top_fetched = {d: last for d, last in self._top_fetched.items() if d in ids}
            written = 0
            for want in wanted:
                if self._stopping.is_set():
                    break
                written += self._fetch_device(want)
        logger.debug("Music: wrote %d keys for %d devices, and %d new covers", written, len(wanted), self.art.fetched)
        return written

    def _top_due(self, want: Wanted, now: float) -> bool:
        last = self._top_fetched.get(want.device_id)
        return last is None or last.services != want.services or now - last.at >= TOP_INTERVAL_S

    def _fetch_device(self, want: Wanted) -> int:
        """Fetch one device's services and write its keys; return how many were written."""
        now = self.clock()
        fetched_at = datetime.fromtimestamp(now, UTC)
        top_due = self._top_due(want, now)
        player: dict[str, dict] = {}
        top: dict[str, dict] = {}
        try:
            for service in want.services:
                player_part = self._fetch(PLAYER_FETCHERS[service], want.device_id, service, "player", now)
                if player_part is None:
                    # The same failure would only repeat (a revoked token is refreshed once per call).
                    continue
                player[service] = player_part
                if top_due and (top_part := self._fetch(TOP_FETCHERS[service], want.device_id, service, "top", now)):
                    top[service] = top_part
        finally:
            # Short transactions: the web app writes to the same SQLite file.
            db.session.remove()
        top_complete = set(top) == set(want.services)
        if not player and not top:
            return 0
        if player:
            # Before the player key, so a renderer never reads a track whose cover isn't stored yet.
            self._keep_art(want.device_id, player)
        with self.store.pipeline(transaction=False) as pipe:
            if player:
                pipe.set(player_key(want.device_id), stored(fetched_at, player), ex=PLAYER_TTL_S)
            if top:
                # Short of a service, only if there's nothing there: an older full copy is better than a gap, and
                # it's tried again next cycle.
                pipe.set(top_key(want.device_id), stored(fetched_at, top), ex=TOP_TTL_S, nx=not top_complete)
            pipe.execute()
        if top_due and top_complete:
            self._top_fetched[want.device_id] = TopFetched(now, want.services)
        return bool(player) + bool(top)

    def _keep_art(self, device_id: str, player: dict[str, dict]) -> None:
        """Store (or keep) the covers the device's player shows; a cover that fails only goes without.

        Raises RedisError if Redis can't be written, as the player key couldn't be either.
        """
        try:
            self.art.keep(art_urls(player))
        except RedisError:
            raise
        except Exception:
            # Never worth the player key: the renderer draws the page without a cover.
            logger.exception("Music: couldn't store device %s's covers", device_id)

    def _fetch(
        self, fetcher: Callable[[str], dict], device_id: str, service: str, part: str, now: float
    ) -> dict | None:
        """Return what `fetcher` gets for the device, or None (logged) if it raises or `service` is backing off."""
        if now < self._backoff_until.get(service, 0):
            logger.debug("Music: skipping device %s's %s %s, rate-limited", device_id, service, part)
            return None
        try:
            return fetcher(device_id)
        except Exception as e:
            db.session.rollback()
            if (wait := retry_after(e)) is not None:
                self._backoff_until[service] = now + wait
                logger.warning("Music: %s is rate-limiting the app (429); calling it again in %ds", service, wait)
            else:
                logger.exception("Music: couldn't fetch device %s's %s %s", device_id, service, part)
            return None

    def _try_cycle(self) -> None:
        """Run a cycle, logging rather than raising if it fails (the next tick tries again)."""
        try:
            self.run_cycle()
        except RedisError as e:
            logger.warning("Music: couldn't reach Redis (%s); trying again next tick", e)
        except Exception:
            # The database, say: a provider that stops would have to be restarted by hand.
            logger.exception("Music: the cycle failed; trying again next tick")

    def _wait_for_next_cycle(self) -> None:
        """Wait until the next tick, or until a `v1:sub:changed` message or stop() if sooner."""
        due = next_tick(self.clock(), INTERVAL_S)
        while not self._stopping.is_set():
            remaining = due - self.clock()
            if remaining <= 0 or self._listen(min(remaining, LISTEN_S)):
                return

    def _listen(self, timeout: float) -> bool:
        """Wait up to `timeout` for a `v1:sub:changed` message; return whether one came.

        A message that came during a cycle is already waiting, so the next cycle starts at once. Losing Redis only
        means waiting for the tick: every cycle reads the devices afresh anyway.
        """
        if self._pubsub is None and self._resubscribe_at is not None and self.clock() < self._resubscribe_at:
            self._stopping.wait(timeout)
            return False
        try:
            if self._pubsub is None:
                pubsub = self.store.pubsub(ignore_subscribe_messages=True)
                pubsub.subscribe(SUB_CHANGED)
                self._pubsub = pubsub
                if self._resubscribe_at is not None:
                    logger.info("Music: listening on %s again", SUB_CHANGED)
                    self._resubscribe_at = None
            message = self._pubsub.get_message(timeout=timeout)
        except RedisError as e:
            if self._resubscribe_at is None:
                logger.warning("Music: lost %s (%s); subscribing again every %ss", SUB_CHANGED, e, RESUBSCRIBE_S)
            self._resubscribe_at = self.clock() + RESUBSCRIBE_S
            self._close_pubsub()
            self._stopping.wait(timeout)
            return False
        if message is None or message["type"] != "message":
            return False
        logger.debug("Music: subscriptions changed")
        return True

    def _close_pubsub(self) -> None:
        if self._pubsub is not None:
            with suppress(RedisError):
                self._pubsub.close()
            self._pubsub = None


def connect() -> redis.Redis:
    """Return a client for the store, from REDIS_URL and REDIS_PASSWORD, as the broker's own."""
    return redis.Redis.from_url(
        os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0"),
        password=os.environ.get("REDIS_PASSWORD") or None,
        decode_responses=True,
        socket_timeout=5,
        socket_connect_timeout=5,
    )


def _configure_logging() -> None:
    """JSON lines on stdout, as gunicorn's are (gunicorn.conf.py), so Fluent Bit ships them the same way."""
    from broker.log_format import JsonFormatter  # noqa: PLC0415 -- it imports gunicorn, which only this needs here

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO").upper(), handlers=[handler], force=True)


def main() -> None:
    """Run the provider until SIGINT or SIGTERM."""
    _configure_logging()
    from broker import create_app  # noqa: PLC0415 -- after logging is set up, so the app's start is logged too

    provider = MusicProvider(create_app(), connect())
    # As a container's PID 1 nothing handles SIGTERM unless we do, and docker stop would have to kill us.
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda *_: provider.stop())
    provider.run()


if __name__ == "__main__":
    main()
