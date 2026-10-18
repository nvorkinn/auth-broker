"""The album covers the music provider stores beside what's playing, so the renderers never call the image hosts.

A renderer's now playing page shows the playing track's cover. Rather than each renderer downloading it from Spotify's
or Apple's image host whenever it draws the page, the music provider (broker/spotify_provider.py) fetches it once and
writes it to the store, keyed by the image's URL so that every screen showing the same album reads one copy:

    v1:art:<sha1 of the URL>  ->  the image file's bytes, as its host served it, with a TTL of ART_TTL_S

pi-dashboard's store.keys.album_art makes the same key. The value is the file's raw bytes, not base64: that would take a
third more of Redis's 128 MB, shared with every other key, for nothing. Writing bytes through the provider's client
needs nothing special (decode_responses only decodes replies, and the provider never reads a cover back: it only renews
their TTLs, and EXPIRE answers with a number); the renderer reads them with a client that doesn't decode
(pi-dashboard's store.reader.binary_store()).

Of an album's sizes, the one stored is the smallest at least ART_SIZE tall, else the tallest: Spotify's 300 px of its
640, 300 and 64, and the one 300 px image the broker asks Apple Music for (clients/apple_music.py's ARTWORK_SIZE).
pi-dashboard's store.music.art_image picks the same way to find the key, so change both together:
tests/fixtures/music_art.json, in both repositories' tests, is the keys stored for music_player.json.

A cover is fetched once, then its TTL renewed each cycle (every minute) while a player key still shows it, so it
outlives that key (PLAYER_TTL_S); a cover nothing shows any more lapses by itself. A cover that can't be fetched is
logged and left out, and tried again next cycle: the renderer then draws the page without one, as for a local file.
"""

import hashlib
import logging
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

import requests

if TYPE_CHECKING:
    from collections.abc import Iterable

    import redis

logger = logging.getLogger("broker.album_art")

# The tallest a renderer draws a cover: its Spotify area is 120 px high in every layout. It scales the stored one down.
ART_SIZE = 120
# Renewed every minute while it's shown: longer than the player key's three minutes, so a cover never goes before the
# key that shows it; short, so one nothing shows any more is soon out of Redis's way.
ART_TTL_S = 5 * 60
# A 300 px JPEG is about 30 KB (a 640 px one, for an album that has no 300, 100 to 200 KB): anything bigger isn't
# stored, so one odd image can't take a large part of Redis's 128 MB.
MAX_ART_BYTES = 256 * 1024
# Each cover is one request; a host that doesn't answer in this long is left alone until the next cycle.
TIMEOUT_S = 5
CHUNK_BYTES = 64 * 1024


def art_key(url: str) -> str:
    """Return the key of the cover at `url`, as its service wrote the URL (pi-dashboard's store.keys.album_art)."""
    return f"v1:art:{hashlib.sha1(url.encode(), usedforsecurity=False).hexdigest()}"


def art_url(track: object) -> str | None:
    """Return the URL of the cover to store for `track` (a track as the player part holds it); None if it has none.

    None for a local file (no images) and for anything that isn't a track with an album (a podcast episode).
    """
    album = track.get("album") if isinstance(track, dict) else None
    images = album.get("images") if isinstance(album, dict) else None
    sized = [
        (image["height"], image["url"])
        for image in images or ()
        if isinstance(image, dict) and isinstance(image.get("height"), int) and isinstance(image.get("url"), str)
    ]
    if not sized:
        return None
    tall_enough = [image for image in sized if image[0] >= ART_SIZE]
    if tall_enough:
        return min(tall_enough, key=lambda image: image[0])[1]
    return max(sized, key=lambda image: image[0])[1]


def art_urls(player: dict[str, dict]) -> list[str]:
    """Return the URLs of the covers the player parts show: each service's playing (or last played) track's.

    Both services', though the page shows only the first with something playing, so it has a cover either way.
    """
    urls = []
    for part in player.values():
        url = art_url(part.get("currently_playing")) if isinstance(part, dict) else None
        if url and url not in urls:
            urls.append(url)
    return urls


class AlbumArt:
    """Keeps the covers the player keys show in `store`, fetching each with `session` only when it isn't there."""

    def __init__(self, store: redis.Redis, session: requests.Session | None = None) -> None:
        """`store`: the music provider's client (decoding replies is fine: it never reads a cover back)."""
        self.store = store
        # A session of its own: the covers come from a couple of hosts, so it keeps their connections open.
        self.session = session or requests.Session()
        self.fetched = 0
        # This cycle's: covers already seen to (so screens showing the same album cost one call, or one renewal), and
        # hosts that didn't answer (so a host that's down costs one timeout a cycle, not one a cover).
        self._seen: set[str] = set()
        self._down: set[str] = set()

    def new_cycle(self) -> None:
        """Start a cycle: every cover is seen to again (renewed or fetched), and every host tried again."""
        self.fetched = 0
        self._seen.clear()
        self._down.clear()

    def keep(self, urls: Iterable[str]) -> None:
        """Make sure each cover is in the store for another ART_TTL_S: renew the ones there, fetch and write the rest.

        One that can't be fetched is logged and left out. Raises RedisError if Redis can't be written.
        """
        for url in urls:
            if url in self._seen:
                continue
            self._seen.add(url)
            key = art_key(url)
            # Still there: renewed, not fetched again. EXPIRE answers 0 for a key that's gone (lapsed, or evicted).
            if self.store.expire(key, ART_TTL_S):
                continue
            image = self._fetch(url)
            if image is not None:
                self.store.set(key, image, ex=ART_TTL_S)
                self.fetched += 1

    def _fetch(self, url: str) -> bytes | None:
        """Return the image at `url`, or None (logged) if it isn't an https URL, can't be fetched, or isn't an image."""
        try:
            parts = urlsplit(url)
        except ValueError:
            parts = None
        if parts is None or parts.scheme != "https" or not parts.hostname:
            logger.warning("Music: not fetching the cover %r: only an https URL is fetched", url)
            return None
        host = parts.hostname
        if host in self._down:
            return None
        try:
            with self.session.get(url, timeout=TIMEOUT_S, stream=True) as response:
                response.raise_for_status()
                content_type = response.headers.get("Content-Type", "")
                if not content_type.startswith("image/"):
                    logger.warning("Music: the cover %s isn't an image (%s); not storing it", url, content_type)
                    return None
                body = bytearray()
                for chunk in response.iter_content(CHUNK_BYTES):
                    body += chunk
                    if len(body) > MAX_ART_BYTES:
                        logger.warning("Music: the cover %s is over %d bytes; not storing it", url, MAX_ART_BYTES)
                        return None
                return bytes(body)
        except (requests.ConnectionError, requests.Timeout) as e:
            self._down.add(host)
            logger.warning("Music: couldn't reach %s for a cover (%s); trying it again next cycle", host, e)
        except requests.RequestException as e:
            logger.warning("Music: couldn't fetch the cover %s (%s); trying it again next cycle", url, e)
        return None
