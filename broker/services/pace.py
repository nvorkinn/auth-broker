"""How often a screen refreshes: BASE_INTERVAL, or faster while a provider's data is bursty.

Nobody sets it. A provider (the developer one, while a release runs) writes a pace key to the store; the renderer reads
it and sends the fastest as `X-Refresh-Hint` with each frame it uploads; the broker keeps it for PACE_LEASE_S and
answers the screen with it as `Retry-After`. A renderer that stops asking (the release finished, or it died) lapses
back to BASE_INTERVAL by itself.
"""

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from broker.models import BASE_INTERVAL, MIN_INTERVAL, PACE_LEASE_S

if TYPE_CHECKING:
    from broker.models import Frame

HINT_HEADER = "X-Refresh-Hint"


def parse_hint(value: str | None) -> int | None:
    """Return the seconds a renderer asked for, kept in MIN_INTERVAL..BASE_INTERVAL; None if it didn't, or it's junk.

    Only faster than usual is a request, so a larger number is just BASE_INTERVAL, i.e. none.
    """
    if value is None or not value.strip().isdigit():
        return None
    seconds = max(int(value.strip()), MIN_INTERVAL)
    return seconds if seconds < BASE_INTERVAL else None


def store_hint(frame_values: dict, hint: int | None, now: datetime | None = None) -> None:
    """Put the hint and when it lapses into the values a frame upload writes (both None to clear it)."""
    now = now or datetime.now(UTC)
    frame_values["pace"] = hint
    frame_values["pace_until"] = now + timedelta(seconds=PACE_LEASE_S) if hint else None


def effective_interval(frame: Frame | None, now: datetime | None = None) -> int:
    """Return the seconds the screen should wait: the renderer's live request, else BASE_INTERVAL."""
    now = now or datetime.now(UTC)
    if frame is not None and frame.pace and frame.pace_until and frame.pace_until > now:
        return frame.pace
    return BASE_INTERVAL
