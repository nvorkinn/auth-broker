"""The music provider: what each screen's Spotify and Apple Music are playing, written to the shared Redis store.

    python -m broker.spotify_provider

It runs in the broker's full app (create_app), as it writes to the database: refreshed Spotify tokens, and Apple
Music's new plays, which the top lists are counted from (apple_music.record_plays) and nothing else logs. The playing
tracks' covers are stored too (broker/album_art.py). The keys are the countdown-api store contract's.

It runs on provider_loop's loop, every INTERVAL_S. A device whose fetch fails is logged and skipped; its keys expire
if it keeps failing.

While a device's Spotify is playing, its screen shows a progress bar, which needs the position every few seconds. So
the loop then runs every BURST_INTERVAL_S, and a device that's playing is, in the cycles between its minutes:

- asked only for /me/player/currently-playing (its `progress_ms`), which is cheap; the queue, Apple Music and the
  top lists are the minute's, kept from its last full cycle;
- written to the player key with that position (`spotify_playback`), with `v1:pace:spotify:<device id>` set to ask
  for the screen to refresh as fast (services/pace.py), and a render of the screen queued (services/render_jobs.py),
  plus one more on the cycle that finds it paused or stopped.

A new track in between makes that cycle a full one, so the queue never shows the old track. Otherwise the pace key is
deleted and the minute's tick draws the screen.
"""

import json
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from http import HTTPStatus
from typing import TYPE_CHECKING

import requests
from countdown_api import keys as store_keys
from redis.exceptions import RedisError
from sqlalchemy import or_, select

from broker import create_app, provider_loop
from broker.album_art import AlbumArt, art_urls
from broker.clients import spotify
from broker.db import db
from broker.models import DeviceConfig, SpotifyToken
from broker.provider_loop import ProviderLoop
from broker.services import music, render_jobs

if TYPE_CHECKING:
    from collections.abc import Callable

    import redis
    from flask import Flask

# What's playing: fetched every minute, as often as the screens are rendered. The queue, Apple Music and the top lists
# are fetched this often at most, however often the loop runs.
INTERVAL_S = 60
# While a device is playing, the loop runs this often, for the position of its player (see the module docstring). The
# fastest pace the broker allows (services/pace.py's HINT_FLOOR_S).
BURST_INTERVAL_S = 20
# The pace asked of the screen while something plays: as often as the loop runs.
PACE_S = BURST_INTERVAL_S
# The pace key lapses unless the next cycle renews it: three burst cycles, so one missed fetch doesn't slow the screen.
PACE_TTL_S = store_keys.PACE_TTL_S
# A full cycle is due when this much of INTERVAL_S has passed since the last: a little less, so the minute's cycle isn't
# put off for another 20 s because it ran a moment before the last one's tick.
FULL_AFTER_S = INTERVAL_S - 5
# Three missed cycles, then the player is gone and the panel says it's not available. The store contract's.
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


def player_key(device_id: str) -> str:
    """Return the key of what a device's music services are playing (the store contract's)."""
    return store_keys.spotify(device_id)


def pace_key(device_id: str) -> str:
    """Return the key of the pace the music provider asks for while Spotify plays (the store contract's)."""
    return store_keys.pace(store_keys.SPOTIFY, device_id)


def top_key(device_id: str) -> str:
    """Return the key of a device's top tracks and artists (the store contract's)."""
    return store_keys.spotify_top(device_id)


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
    # get_queue's [] for a 204 isn't a queue the screen can read.
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


def spotify_playback(device_id: str) -> dict[str, object]:
    """Return the device's Spotify player as /me/player/currently-playing answers; {} for nothing playing (204)."""
    return spotify.get_currently_playing(device_id)


def playback_part(answer: dict, fetched_at: datetime) -> dict | None:
    """Return the stored `spotify_playback` for Spotify's currently-playing `answer`; None unless it shows a track.

    An episode (the answer has no `item` without asking for them) or an ad has no position worth a bar.
    """
    item = answer.get("item")
    progress = answer.get("progress_ms")
    if not isinstance(item, dict) or not isinstance(progress, int):
        return None
    return {
        "fetched_at": fetched_at.isoformat(),
        "is_playing": bool(answer.get("is_playing")),
        "progress_ms": max(progress, 0),
        "track_id": item.get("id"),
    }


def stored(fetched_at: datetime, parts: dict[str, dict]) -> str:
    """Return the stored JSON: when it was fetched, and each service's part (null if not fetched)."""
    return json.dumps({"fetched_at": fetched_at.isoformat(), **{s: parts.get(s) for s in (SPOTIFY, APPLE_MUSIC)}})


def stored_player(fetched_at: datetime, parts: dict[str, dict], playback: dict | None) -> str:
    """Return the player key's JSON: `stored`, and Spotify's position (null when it shows no track)."""
    return json.dumps({**json.loads(stored(fetched_at, parts)), "spotify_playback": playback})


def track_id(answer: dict) -> str | None:
    """Return the id of the track in Spotify's currently-playing `answer`, if it has one."""
    item = answer.get("item")
    return item.get("id") if isinstance(item, dict) else None


def current_track(parts: dict[str, dict]) -> str | None:
    """Return the id of the track Spotify's queue in `parts` says is playing, if any."""
    playing = (parts.get(SPOTIFY) or {}).get("currently_playing")
    return playing.get("id") if isinstance(playing, dict) else None


@dataclass
class FullCycle:
    """A device's last full cycle: when, for which services, and what it fetched (kept for the cycles in between)."""

    at: float
    services: tuple[str, ...]
    parts: dict[str, dict]
    fetched_at: datetime


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


class MusicProvider(ProviderLoop):
    """Fetches every wanted device's music each cycle, faster while one of them is playing Spotify."""

    label = "Music"

    def __init__(
        self,
        app: Flask,
        store: redis.Redis,
        clock: Callable[[], float] = time.time,
        images: requests.Session | None = None,
    ) -> None:
        """See ProviderLoop; `images` fetches the covers."""
        super().__init__(app, store, clock)
        self.art = AlbumArt(store, images)
        self._top_fetched: dict[str, TopFetched] = {}
        self._full: dict[str, FullCycle] = {}
        # The devices whose last cycle found Spotify playing. A device whose fetch fails stays as it was, so one
        # hiccup doesn't slow the screen in the middle of a track.
        self._playing: set[str] = set()
        # Service -> when it may be called again, after it answered 429.
        self._backoff_until: dict[str, float] = {}

    def interval_s(self) -> float:
        """Return BURST_INTERVAL_S while any device's Spotify is playing, else INTERVAL_S."""
        return BURST_INTERVAL_S if self._playing else INTERVAL_S

    def run_cycle(self) -> int:
        """Fetch every wanted device and write what came back; return how many of their keys were written.

        Not counting the covers. Raises RedisError if Redis can't be written, ending the cycle; a failing device only
        loses its own keys.
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
            self._full = {d: last for d, last in self._full.items() if d in ids}
            self._playing &= ids
            written = 0
            for want in wanted:
                if self._stopping.is_set():
                    break
                written += self._fetch_device(want)
        self.logger.debug(
            "Music: wrote %d keys for %d devices, and %d new covers", written, len(wanted), self.art.fetched
        )
        return written

    def _top_due(self, want: Wanted, now: float) -> bool:
        last = self._top_fetched.get(want.device_id)
        return last is None or last.services != want.services or now - last.at >= TOP_INTERVAL_S

    def _fetch_device(self, want: Wanted) -> int:
        """Fetch one device's services and write its keys; return how many were written.

        A full cycle fetches everything (the queue, Apple Music, the top lists when due) and is due every INTERVAL_S,
        or when the track changes. A cycle in between, while Spotify plays, fetches only its position.
        """
        now = self.clock()
        answer: dict | None = None
        if SPOTIFY in want.services:
            answer = self._fetch(spotify_playback, want.device_id, SPOTIFY, "position", now)
        answered_at = datetime.fromtimestamp(self.clock(), UTC)
        last = self._full.get(want.device_id)
        if last is not None and last.services == want.services and now - last.at < FULL_AFTER_S:
            if answer is None:
                # No position to add (not asked for, or the fetch failed, logged): the last cycle's keys stay.
                return 0
            # The queue shows the track it was fetched at: a new one (or none) needs it again.
            if track_id(answer) == current_track(last.parts):
                return self._write_position(want, last, answer, answered_at)
        return self._fetch_full(want, now, answer, answered_at)

    def _write_position(self, want: Wanted, last: FullCycle, answer: dict, answered_at: datetime) -> int:
        """Rewrite the player key with Spotify's new position and the last full cycle's data; return 1."""
        playback = playback_part(answer, answered_at)
        playing = bool(playback and playback["is_playing"])
        with self.store.pipeline(transaction=False) as pipe:
            pipe.set(player_key(want.device_id), stored_player(last.fetched_at, last.parts, playback), ex=PLAYER_TTL_S)
            self._write_pace(pipe, want.device_id, playing=playing)
            pipe.execute()
        self._queue_render_if_bursting(want.device_id, playing=playing)
        return 1

    def _fetch_full(self, want: Wanted, now: float, answer: dict | None, answered_at: datetime) -> int:
        """Fetch all of one device's services (and the top lists when due) and write its keys; return how many."""
        fetched_at = datetime.fromtimestamp(now, UTC)
        top_due = self._top_due(want, now)
        player, top = self._fetch_services(want, now, top_due=top_due)
        top_complete = set(top) == set(want.services)
        if not player and not top:
            return 0
        playback = None if answer is None else playback_part(answer, answered_at)
        playing = bool(playback and playback["is_playing"])
        if player:
            # Before the player key, so a renderer never reads a track whose cover isn't stored yet.
            self._keep_art(want.device_id, player)
        with self.store.pipeline(transaction=False) as pipe:
            if player:
                pipe.set(player_key(want.device_id), stored_player(fetched_at, player, playback), ex=PLAYER_TTL_S)
                if answer is not None:
                    self._write_pace(pipe, want.device_id, playing=playing)
            if top:
                # Missing a service: written only if there's nothing there, as an older full copy is better. The
                # next cycle tries again.
                pipe.set(top_key(want.device_id), stored(fetched_at, top), ex=TOP_TTL_S, nx=not top_complete)
            pipe.execute()
        if player:
            self._full[want.device_id] = FullCycle(now, want.services, player, fetched_at)
        if top_due and top_complete:
            self._top_fetched[want.device_id] = TopFetched(now, want.services)
        if player and answer is not None:
            self._queue_render_if_bursting(want.device_id, playing=playing)
        return bool(player) + bool(top)

    def _fetch_services(self, want: Wanted, now: float, *, top_due: bool) -> tuple[dict[str, dict], dict[str, dict]]:
        """Return the device's player parts, and its top lists if `top_due`, of the services that could be fetched."""
        player: dict[str, dict] = {}
        top: dict[str, dict] = {}
        try:
            for service in want.services:
                player_part = self._fetch(PLAYER_FETCHERS[service], want.device_id, service, "player", now)
                if player_part is None:
                    # Its top lists would fail the same way (each call tries to refresh a revoked token).
                    continue
                player[service] = player_part
                if top_due and (top_part := self._fetch(TOP_FETCHERS[service], want.device_id, service, "top", now)):
                    top[service] = top_part
        finally:
            # Short transactions: the web app writes to the same SQLite file.
            db.session.remove()
        return player, top

    def _queue_render_if_bursting(self, device_id: str, *, playing: bool) -> None:
        """Record whether the device is playing, and queue a render of its screen if it is or just stopped.

        After the write, so the render reads the new position. Never raises: the tick draws the screen anyway.
        """
        bursting = playing or device_id in self._playing
        (self._playing.add if playing else self._playing.discard)(device_id)
        if bursting:
            render_jobs.enqueue(self.store, [device_id])

    @staticmethod
    def _write_pace(pipe: redis.client.Pipeline, device_id: str, *, playing: bool) -> None:
        """Queue the pace key: set while Spotify plays (the screen follows it), else deleted."""
        key = pace_key(device_id)
        if playing:
            pipe.set(key, PACE_S, ex=PACE_TTL_S)
        else:
            pipe.delete(key)

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
            self.logger.exception("Music: couldn't store device %s's covers", device_id)

    def _fetch(
        self, fetcher: Callable[[str], dict], device_id: str, service: str, part: str, now: float
    ) -> dict | None:
        """Return what `fetcher` gets for the device, or None (logged) if it raises or `service` is backing off."""
        if now < self._backoff_until.get(service, 0):
            self.logger.debug("Music: skipping device %s's %s %s, rate-limited", device_id, service, part)
            return None
        try:
            return fetcher(device_id)
        except Exception as e:
            db.session.rollback()
            if (wait := retry_after(e)) is not None:
                self._backoff_until[service] = now + wait
                self.logger.warning("Music: %s is rate-limiting the app (429); calling it again in %ds", service, wait)
            else:
                self.logger.exception("Music: couldn't fetch device %s's %s %s", device_id, service, part)
            return None


def main() -> None:
    """Run the provider until SIGINT or SIGTERM, in the broker's full app: it writes to the database."""
    provider_loop.serve(MusicProvider, create_app)


if __name__ == "__main__":
    main()
