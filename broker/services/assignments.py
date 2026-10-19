"""A screen's stable name: its slug, given when it's paired (auto_name), and never changed.

The assignment is all that's left of the renderer the orchestrator used to start for a screen. A screen gets one the
moment it's paired, whose slug is its own device id; screens named before that keep the `<username>-<choice>` slug
their owner picked. The render pool draws every named screen it has something to draw for, under its slug
(services/render_screens.py); the slug is also the `device` label of the pool's metrics, so it never changes. What
the owner calls the screen is its friendly name (devices.device_name, clean_name), set on its settings page. A screen
unseen for SUSPEND_AFTER is left out of the pool until it's back (is_quiet). The assignment goes with its device.
"""

import re
import secrets
from datetime import UTC, datetime, timedelta

from broker.db import db
from broker.models import Device, RendererAssignment

# What a slug is made of (a device id, or one an owner chose before screens were named automatically). It names the
# screen in the pool's list and metrics, so it never changes.
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,31}$")

# How long a screen may go unseen before the render pool stops drawing it (and, on its return, draws it again).
SUSPEND_AFTER = timedelta(hours=24)


class NameTooLongError(ValueError):
    """The friendly name is longer than NAME_MAX."""


# The longest name an owner may give a screen (devices.device_name): it's only shown, in the screen switcher.
NAME_MAX = 40


def auto_name(device: Device) -> RendererAssignment | None:
    """Give a device its stable internal name, if it has none yet; return the assignment made (None if it had one).

    The slug is the device's own id, which no owner's slug can be (theirs have a dash in them). It's never shown: the
    owner calls the screen whatever they like (devices.device_name, on its settings page), and this stays what the
    render pool lists it under and labels its metrics with (services/render_screens.py), so a rename never breaks a
    series. Added to the session, not committed.
    """
    if device.assignment is not None:
        return None
    assignment = RendererAssignment(
        id=secrets.token_hex(6), device=device, slug=device.device_id, created_at=datetime.now(UTC)
    )
    db.session.add(assignment)
    return assignment


def clean_name(name: str) -> str | None:
    """Return the friendly name an owner typed, as stored: its spaces trimmed, None for none.

    Raises NameTooLongError if it's longer than NAME_MAX.
    """
    name = " ".join(name.split())
    if len(name) > NAME_MAX:
        raise NameTooLongError(name)
    return name or None


def is_quiet(assignment: RendererAssignment, now: datetime) -> bool:
    """Whether the assignment's screen has been unseen for SUSPEND_AFTER, so the render pool leaves it out.

    The assignment must have a device.
    """
    device = assignment.device
    # A screen named while it was away isn't left out before it's ever drawn.
    seen = max(filter(None, [device.display_last_seen_at, device.created_at, assignment.created_at]))
    return now - seen >= SUSPEND_AFTER
