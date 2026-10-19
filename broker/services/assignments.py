"""The renderer a screen is to have.

Its owner names the screen (the slug), which creates an assignment; the orchestrator (see its README
in the pi-dashboard repo) polls them, starts a renderer container for each, and reports back. The
renderer then registers with the assignment's claim token (services/registration.py), which attaches
it to the screen's device.

An assignment goes pending -> provisioned, and between those and suspended as the screen goes quiet
and returns; failed is the orchestrator's report of a failure, retried on its next pass. It becomes
deleting when its device is forgotten, and is removed once the orchestrator confirms it has cleaned up.
"""

import re
import secrets
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from broker.db import db
from broker.models import Device, RendererAssignment

# What the orchestrator accepts; it also names the container and its volumes, so it never changes.
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,31}$")
SLUG_MAX = 32
# What a screen's name is made of: the owner's username (cut down to fit) and what they choose.
PREFIX_MAX = 16
PREFIX_FALLBACK = "user"
SUFFIX_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")

# How long a screen may go unseen before its renderer is stopped (and, on its return, started again).
SUSPEND_AFTER = timedelta(hours=24)

# What the orchestrator may report for an assignment.
REPORTABLE = {"provisioned", "failed", "deleted"}
# The states in which a report of progress still means something; in any other the broker has since
# changed its mind (suspended it, queued its deletion) and the report is ignored.
IN_PROGRESS = {"pending", "provisioned", "failed"}


class InvalidSlugError(ValueError):
    """The chosen name isn't one the orchestrator accepts, or makes too long a slug."""


class NameTooLongError(ValueError):
    """The friendly name is longer than NAME_MAX."""


class SlugTakenError(ValueError):
    """Another screen already has the slug."""


class NotNeededError(ValueError):
    """The device already has a renderer or an assignment."""


def slug_prefix(username: str | None) -> str:
    """Return the first part of every slug the user makes.

    That's their username lowercased, anything the orchestrator doesn't accept turned into a dash, and
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


def needs_renderer(device: Device) -> bool:
    """Whether the device has neither a renderer nor an assignment for one yet."""
    return device.renderer_secret_hash is None and device.assignment is None


def create(device: Device, suffix: str, username: str | None) -> RendererAssignment:
    """Name the device's screen "<owner's username>-<suffix>", which asks the orchestrator for a renderer.

    Raises InvalidSlugError, SlugTakenError (unique across the whole broker) or NotNeededError.
    """
    slug = full_slug(username, suffix)
    if not needs_renderer(device):
        raise NotNeededError(device.device_id)
    if db.session.scalar(select(RendererAssignment.id).where(RendererAssignment.slug == slug)):
        raise SlugTakenError(slug)

    assignment = RendererAssignment(
        id=secrets.token_hex(6),
        device=device,
        slug=slug,
        claim_token=secrets.token_urlsafe(32),
        status="pending",
        created_at=datetime.now(UTC),
    )
    db.session.add(assignment)
    try:
        db.session.commit()
    except IntegrityError:
        # Someone took the slug (or this device got its assignment) since the check above.
        db.session.rollback()
        raise SlugTakenError(slug) from None
    return assignment


def listing() -> list[RendererAssignment]:
    """Every assignment the orchestrator should act on, after applying the suspension policy.

    A renderer whose screen has been unseen for SUSPEND_AFTER is suspended, and pending again once the
    screen is back. (Only the broker knows when screens were last seen.)
    """
    now = datetime.now(UTC)
    assignments = db.session.scalars(select(RendererAssignment).order_by(RendererAssignment.created_at)).all()
    for assignment in assignments:
        if assignment.device is None:
            continue
        quiet = is_quiet(assignment, now)
        if quiet and assignment.status in {"pending", "provisioned"}:
            assignment.status = "suspended"
        elif not quiet and assignment.status == "suspended":
            assignment.status = "pending"
    db.session.commit()
    return list(assignments)


def is_quiet(assignment: RendererAssignment, now: datetime) -> bool:
    """Whether the assignment's screen has been unseen for SUSPEND_AFTER, so its renderer is to be suspended.

    The assignment must have a device. The render pool's list of screens (services/render_screens.py) leaves its
    screen out by the same rule.
    """
    device = assignment.device
    # A new assignment for a screen that was away isn't suspended before it ever runs.
    seen = max(filter(None, [device.display_last_seen_at, device.created_at, assignment.created_at]))
    return now - seen >= SUSPEND_AFTER


def report(assignment: RendererAssignment, status: str, detail: str) -> bool:
    """Record the orchestrator's report.

    False for `deleted` on an assignment that isn't being deleted, which is refused; a progress report
    that no longer applies is ignored (True).
    """
    if status == "deleted":
        if assignment.status != "deleting":
            return False
        db.session.delete(assignment)
    elif assignment.status in IN_PROGRESS:
        assignment.status = status
        assignment.detail = detail[:500]
    db.session.commit()
    return True


def mark_deleting(device: Device) -> None:
    """For a device about to be forgotten: its renderer is to be removed, volumes and all.

    The assignment outlives the device, so the orchestrator still hears of it.
    """
    assignment = device.assignment
    if assignment is not None:
        assignment.status = "deleting"
        assignment.claim_token = None
        assignment.device = None
