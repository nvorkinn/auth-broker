"""The renderer a screen is to have. Its owner names the screen (the slug), which creates an
assignment; the orchestrator (see its README in the pi-dashboard repo) polls them, starts a renderer
container for each, and reports back. The renderer then registers with the assignment's claim token
(services/registration.py), which attaches it to the screen's device.

An assignment goes pending -> provisioned, and between those and suspended as the screen goes quiet
and returns; failed is the orchestrator's report of a failure, retried on its next pass. It becomes
deleting when its device is forgotten, and is removed once the orchestrator confirms it has cleaned up."""

import re
import secrets
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from ..db import db
from ..models import Device, RendererAssignment

# What the orchestrator accepts; it also names the container and its volumes, so it never changes.
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,31}$")

# How long a screen may go unseen before its renderer is stopped (and, on its return, started again).
SUSPEND_AFTER = timedelta(hours=24)

# What the orchestrator may report for an assignment.
REPORTABLE = {"provisioned", "failed", "deleted"}
# The states in which a report of progress still means something; in any other the broker has since
# changed its mind (suspended it, queued its deletion) and the report is ignored.
IN_PROGRESS = {"pending", "provisioned", "failed"}


class InvalidSlug(ValueError):
    pass


class SlugTaken(ValueError):
    pass


class NotNeeded(ValueError):
    """The device already has a renderer or an assignment."""


def needs_renderer(device: Device) -> bool:
    return device.renderer_secret_hash is None and device.assignment is None


def create(device: Device, slug: str) -> RendererAssignment:
    """Names the device's screen, which asks the orchestrator for a renderer. Raises InvalidSlug,
    SlugTaken (unique across the whole broker) or NotNeeded."""
    if not SLUG_RE.match(slug):
        raise InvalidSlug(slug)
    if not needs_renderer(device):
        raise NotNeeded(device.device_id)
    if db.session.scalar(select(RendererAssignment.id).where(RendererAssignment.slug == slug)):
        raise SlugTaken(slug)

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
        raise SlugTaken(slug) from None
    return assignment


def listing() -> list[RendererAssignment]:
    """Every assignment the orchestrator should act on, after applying the suspension policy: a
    renderer whose screen has been unseen for SUSPEND_AFTER is suspended, and pending again once the
    screen is back. (Only the broker knows when screens were last seen.)"""
    now = datetime.now(UTC)
    assignments = db.session.scalars(select(RendererAssignment).order_by(RendererAssignment.created_at)).all()
    for assignment in assignments:
        device = assignment.device
        if device is None:
            continue
        # A new assignment for a screen that was away isn't suspended before it ever runs.
        seen = max(filter(None, [device.display_last_seen_at, device.created_at, assignment.created_at]))
        quiet = now - seen >= SUSPEND_AFTER
        if quiet and assignment.status in {"pending", "provisioned"}:
            assignment.status = "suspended"
        elif not quiet and assignment.status == "suspended":
            assignment.status = "pending"
    db.session.commit()
    return list(assignments)


def report(assignment: RendererAssignment, status: str, detail: str) -> bool:
    """Records the orchestrator's report. False for `deleted` on an assignment that isn't being
    deleted, which is refused; a progress report that no longer applies is ignored (True)."""
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
    """For a device about to be forgotten: its renderer is to be removed, volumes and all. The
    assignment outlives the device, so the orchestrator still hears of it."""
    assignment = device.assignment
    if assignment is not None:
        assignment.status = "deleting"
        assignment.claim_token = None
        assignment.device = None
