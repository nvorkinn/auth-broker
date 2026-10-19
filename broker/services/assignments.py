"""A screen's name: its slug, which its owner chooses once.

Its owner names the screen, which creates its assignment (the name is all that's left of the renderer the orchestrator
used to start for it). The render pool draws every named screen it has something to draw for, under its slug
(services/render_screens.py); the slug is also the `device` label of the pool's metrics. A screen unseen for
SUSPEND_AFTER is left out of the pool until it's back (is_quiet). The assignment goes with its device.
"""

import re
import secrets
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from broker.db import db
from broker.models import Device, RendererAssignment

# What a slug is made of. It names the screen in the pool's metrics and dashboards, so it never changes.
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,31}$")
SLUG_MAX = 32
# What a screen's name is made of: the owner's username (cut down to fit) and what they choose.
PREFIX_MAX = 16
PREFIX_FALLBACK = "user"
SUFFIX_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")

# How long a screen may go unseen before the render pool stops drawing it (and, on its return, draws it again).
SUSPEND_AFTER = timedelta(hours=24)


class InvalidSlugError(ValueError):
    """The chosen name isn't one a slug may be, or makes too long a slug."""


class SlugTakenError(ValueError):
    """Another screen already has the slug."""


class NotNeededError(ValueError):
    """The device is named already."""


def slug_prefix(username: str | None) -> str:
    """Return the first part of every slug the user makes.

    That's their username lowercased, anything a slug can't have turned into a dash, and
    cut to PREFIX_MAX. Fixed when the screen is named, so a later change of username doesn't rename it.
    Falls back to "user" when nothing usable is left.
    """
    cleaned = re.sub(r"[^a-z0-9_-]+", "-", (username or "").lower())
    cleaned = re.sub(r"-{2,}", "-", cleaned).strip("-_")[:PREFIX_MAX].rstrip("-_")
    return cleaned or PREFIX_FALLBACK


def full_slug(username: str | None, suffix: str) -> str:
    """Return the slug for what the user chose, "<prefix>-<suffix>".

    Raises InvalidSlugError if it's not allowed or too long.
    """
    prefix = slug_prefix(username)
    slug = f"{prefix}-{suffix}"
    if not SUFFIX_RE.match(suffix) or len(slug) > SLUG_MAX or not SLUG_RE.match(slug):
        raise InvalidSlugError(suffix)
    return slug


def max_suffix_length(username: str | None) -> int:
    """Return how long a suffix the user can choose, after their prefix and its dash."""
    return SLUG_MAX - len(slug_prefix(username)) - 1


def needs_name(device: Device) -> bool:
    """Whether the device has no name (assignment) yet."""
    return device.assignment is None


def create(device: Device, suffix: str, username: str | None) -> RendererAssignment:
    """Name the device's screen "<owner's username>-<suffix>", which puts it on the render pool.

    Raises InvalidSlugError, SlugTakenError (unique across the whole broker) or NotNeededError.
    """
    slug = full_slug(username, suffix)
    if not needs_name(device):
        raise NotNeededError(device.device_id)
    if db.session.scalar(select(RendererAssignment.id).where(RendererAssignment.slug == slug)):
        raise SlugTakenError(slug)

    assignment = RendererAssignment(id=secrets.token_hex(6), device=device, slug=slug, created_at=datetime.now(UTC))
    db.session.add(assignment)
    try:
        db.session.commit()
    except IntegrityError:
        # Someone took the slug (or this device got its assignment) since the check above.
        db.session.rollback()
        raise SlugTakenError(slug) from None
    return assignment


def is_quiet(assignment: RendererAssignment, now: datetime) -> bool:
    """Whether the assignment's screen has been unseen for SUSPEND_AFTER, so the render pool leaves it out.

    The assignment must have a device.
    """
    device = assignment.device
    # A screen named while it was away isn't left out before it's ever drawn.
    seen = max(filter(None, [device.display_last_seen_at, device.created_at, assignment.created_at]))
    return now - seen >= SUSPEND_AFTER
