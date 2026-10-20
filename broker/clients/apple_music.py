"""Talks to Apple Music on a device's behalf, and keeps the log of plays its top lists are counted from.

Apple Music's web API has no now playing, no queue and no top tracks or artists: only the tracks played recently,
newest first and without times. So the music area gets the last played track and the ones before it, and the top lists
are counted from the plays this broker has seen (see record_plays), all in Spotify's shapes.

Calls need two tokens: a developer token, which the broker signs itself with the MusicKit key from the owner's Apple
Developer account (APPLE_MUSIC_*), and the listener's Music User Token, which MusicKit JS gets on the settings page
when they sign in to Apple.
"""

import base64
import json
import time
from collections import Counter
from datetime import UTC, datetime, timedelta
from http import HTTPStatus

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from flask import current_app
from sqlalchemy import select

from broker.db import db
from broker.models import AppleMusicPlay, AppleMusicToken

API_URL = "https://api.music.apple.com/v1"
# How many pages of recently played tracks are read on each call: Apple hands them over ten to a page.
RECENT_PAGES = 2
# How far back the top lists count, by Spotify's time_range names.
TIME_RANGES = {"short_term": timedelta(weeks=4), "medium_term": timedelta(days=183), "long_term": timedelta(days=365)}
# The cover size asked for; the renderer scales it down to its own.
ARTWORK_SIZE = 300
# A developer token may last up to six months; a fresh one each half day keeps a leaked one short-lived.
DEVELOPER_TOKEN_LIFETIME = timedelta(hours=12)

_developer_token: tuple[str, float] | None = None


class AppleMusicRefusedError(Exception):
    """Apple refused the listener's token: it expired or was revoked, so they have to connect again."""


def configured() -> bool:
    """Whether this broker has a MusicKit key to sign developer tokens with."""
    return all(
        current_app.config[key] for key in ("APPLE_MUSIC_TEAM_ID", "APPLE_MUSIC_KEY_ID", "APPLE_MUSIC_PRIVATE_KEY")
    )


def linked(device_id: str) -> bool:
    """Whether the device has an Apple Music token that Apple hasn't refused."""
    stored = db.session.get(AppleMusicToken, device_id)
    return stored is not None and stored.token_encrypted is not None


def developer_token() -> str:
    """Return a developer token: a JWT signed with ES256 by the MusicKit key, reused until it is half spent."""
    global _developer_token  # noqa: PLW0603 -- one token for the whole process, like a connection pool
    now = time.time()
    if _developer_token is not None and _developer_token[1] - DEVELOPER_TOKEN_LIFETIME.total_seconds() / 2 > now:
        return _developer_token[0]
    expires = now + DEVELOPER_TOKEN_LIFETIME.total_seconds()
    header = {"alg": "ES256", "kid": current_app.config["APPLE_MUSIC_KEY_ID"]}
    claims = {"iss": current_app.config["APPLE_MUSIC_TEAM_ID"], "iat": int(now), "exp": int(expires)}
    signing_input = f"{_b64(json.dumps(header).encode())}.{_b64(json.dumps(claims).encode())}"
    key = serialization.load_pem_private_key(current_app.config["APPLE_MUSIC_PRIVATE_KEY"].encode(), password=None)
    if not isinstance(key, ec.EllipticCurvePrivateKey):
        msg = "APPLE_MUSIC_PRIVATE_KEY is not a MusicKit (P-256) key"
        raise TypeError(msg)
    r, s = decode_dss_signature(key.sign(signing_input.encode(), ec.ECDSA(hashes.SHA256())))
    token = f"{signing_input}.{_b64(r.to_bytes(32, 'big') + s.to_bytes(32, 'big'))}"
    _developer_token = (token, expires)
    return token


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def recently_played(user_token: str) -> list[dict]:
    """Return the listener's recently played songs, newest first, as Apple's song resources.

    Raises AppleMusicRefusedError when Apple refuses the user token.
    """
    headers = {"Authorization": f"Bearer {developer_token()}", "Music-User-Token": user_token}
    songs: list[dict] = []
    path: str | None = "/me/recent/played/tracks"
    for _ in range(RECENT_PAGES):
        if path is None:
            break
        response = requests.get(f"{API_URL}{path.removeprefix('/v1')}", headers=headers, timeout=10)
        if response.status_code in {HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN}:
            raise AppleMusicRefusedError
        response.raise_for_status()
        body = response.json()
        songs += [item for item in body.get("data", []) if item.get("type") in {"songs", "library-songs"}]
        path = body.get("next")
    return songs


def as_track(song: dict) -> dict:
    """Return Apple's song resource in the shape of a Spotify track, as far as the renderer reads one."""
    attributes = song.get("attributes", {})
    artwork = attributes.get("artwork")
    images = []
    if artwork and artwork.get("url"):
        url = artwork["url"].replace("{w}", str(ARTWORK_SIZE)).replace("{h}", str(ARTWORK_SIZE))
        images.append({"width": ARTWORK_SIZE, "height": ARTWORK_SIZE, "url": url})
    return {
        "name": attributes.get("name", ""),
        "duration_ms": attributes.get("durationInMillis", 0),
        "artists": [{"name": attributes.get("artistName", "")}],
        "album": {"name": attributes.get("albumName", ""), "images": images},
    }


def new_plays(seen: list[str], recent: list[str]) -> list[str]:
    """Return the songs at the head of `recent` that weren't there when it was last read as `seen`; both newest first.

    The list has no times, so a play is found by where the last list picks up again: the first place `recent` carries
    on as `seen` began. The same song played twice in a row is still told apart that way. If `seen` is nowhere in it
    (more plays than a page since last time, or nothing seen yet), all of `recent` is new.
    """
    for start in range(len(recent)):
        overlap = min(len(seen), len(recent) - start)
        if overlap and recent[start : start + overlap] == seen[:overlap]:
            return recent[:start]
    return recent


def recent_tracks(device_id: str) -> list[dict] | None:
    """Return the device's listener's recently played tracks, newest first, logging any new plays on the way.

    None when Apple Music isn't connected, or Apple has refused the token (which is then forgotten, so the settings
    page asks to connect again).
    """
    stored = db.session.get(AppleMusicToken, device_id)
    if stored is None or stored.token_encrypted is None:
        return None
    try:
        songs = recently_played(stored.token)
    except AppleMusicRefusedError:
        # The log and where it got to stay, so connecting again carries on rather than counting the same plays twice.
        stored.token = None
        db.session.commit()
        return None
    record_plays(stored, songs)
    return [as_track(song) for song in songs]


def record_plays(stored: AppleMusicToken, songs: list[dict]) -> None:
    """Log the plays in `songs` that weren't in the list last time, so the top lists can be counted."""
    ids = [song["id"] for song in songs]
    now = datetime.now(UTC)
    by_id = {song["id"]: song for song in songs}
    new = new_plays(stored.seen_ids or [], ids)
    # Oldest first, a second apart, so the log keeps their order.
    for offset, song_id in enumerate(reversed(new)):
        attributes = by_id[song_id].get("attributes", {})
        db.session.add(
            AppleMusicPlay(
                device_id=stored.device_id,
                played_at=now - timedelta(seconds=len(new) - offset),
                song_id=song_id,
                track=as_track(by_id[song_id]),
                artist=attributes.get("artistName", ""),
            )
        )
    stored.seen_ids = ids
    db.session.commit()


def top(device_id: str, type_: str, time_range: str, limit: int) -> dict[str, list[dict]] | None:
    """Return the most played tracks or artists (`type_`) over `time_range`, in the shape of Spotify's /me/top.

    Counted from the plays logged since the listener connected; None when Apple Music isn't connected.
    """
    if not linked(device_id):
        return None
    since = datetime.now(UTC) - TIME_RANGES.get(time_range, TIME_RANGES["medium_term"])
    plays = db.session.scalars(
        select(AppleMusicPlay)
        .where(AppleMusicPlay.device_id == device_id, AppleMusicPlay.played_at >= since)
        .order_by(AppleMusicPlay.played_at.desc())
    ).all()
    if type_ == "artists":
        counts = Counter(play.artist for play in plays if play.artist)
        return {"items": [{"name": artist} for artist, _ in counts.most_common(limit)]}
    counts = Counter(play.song_id for play in plays)
    # The newest play of each song carries its details; most_common keeps the first seen of a tie, the newest.
    tracks = {}
    for play in plays:
        tracks.setdefault(play.song_id, play.track)
    return {"items": [tracks[song_id] for song_id, _ in counts.most_common(limit)]}
