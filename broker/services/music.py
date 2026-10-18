"""The music area's data in the shapes the screen reads, which the music provider (broker/spotify_provider.py) stores.

Spotify's are Spotify's own (broker/clients/spotify.py hands them over as they come); Apple Music's are made to look
like them (broker/clients/apple_music.py), so the screen draws both with the same panels.
"""

from broker.clients import apple_music

# How many of the tracks before the last played the screen gets, in place of Spotify's queue.
EARLIER = 5


def nothing_playing() -> dict[str, object]:
    """Return a queue with nothing playing and nothing up next, in the shape of Spotify's /me/player/queue."""
    return {"currently_playing": None, "queue": []}


def apple_music_queue(device_id: str) -> dict[str, object]:
    """Return the last played Apple Music track and the ones before it, as Spotify's queue.

    Apple Music has no now playing or queue, so this is the nearest it has. currently_playing is null when this broker
    has no MusicKit key, the device isn't connected, or nothing has been played. Logs any new plays on the way (see
    apple_music.recent_tracks). Raises requests.RequestException if Apple can't be reached.
    """
    tracks = apple_music.recent_tracks(device_id) if apple_music.configured() else None
    if not tracks:
        return nothing_playing()
    return {"currently_playing": tracks[0], "queue": tracks[1 : 1 + EARLIER]}


def apple_music_top(device_id: str, type_: str, time_range: str, limit: int) -> dict[str, list[dict]]:
    """Return the device's most played Apple Music tracks or artists, as Spotify's /me/top; empty if not connected."""
    return apple_music.top(device_id, type_, time_range, limit) or {"items": []}
