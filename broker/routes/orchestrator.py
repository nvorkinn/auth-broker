"""What the orchestrator that starts renderers calls (see services/assignments.py).

It uses its own bearer token rather than a device secret.
"""

from typing import TYPE_CHECKING

from flask import Blueprint, jsonify, request

from broker.auth import require_orchestrator_token
from broker.db import db
from broker.models import RendererAssignment
from broker.services import assignments

if TYPE_CHECKING:
    from flask.typing import ResponseReturnValue

bp = Blueprint("orchestrator", __name__, url_prefix="/api/orchestrator")


@bp.get("/assignments")
@require_orchestrator_token
def list_assignments() -> ResponseReturnValue:
    """Every assignment that isn't deleted. `claim_token` is null once the renderer has claimed it.

    `device_id` is the screen's (its `v1:config:` and `v1:frame:` keys are under it), so the orchestrator can hand the
    screen to the render pool; null once the device is forgotten (the assignment is then `deleting`).
    """
    return jsonify(
        assignments=[
            {"id": a.id, "slug": a.slug, "claim_token": a.claim_token, "status": a.status, "device_id": a.device_id}
            for a in assignments.listing()
        ]
    )


@bp.post("/assignments/<assignment_id>/status")
@require_orchestrator_token
def report_status(assignment_id: str) -> ResponseReturnValue:
    """Record what the orchestrator did with an assignment.

    {"status": "provisioned" | "failed" | "deleted", "detail": ""}. `deleted` removes the assignment
    for good, and is only accepted for one that's being deleted.
    """
    assignment = db.session.get(RendererAssignment, assignment_id)
    if assignment is None:
        return jsonify(error="not found"), 404

    body = request.get_json(silent=True) or {}
    status, detail = body.get("status"), body.get("detail", "")
    if status not in assignments.REPORTABLE or not isinstance(detail, str):
        return jsonify(error=f"status must be one of {', '.join(sorted(assignments.REPORTABLE))}"), 400
    if not assignments.report(assignment, status, detail):
        return jsonify(error="only an assignment being deleted can be reported deleted"), 409
    return jsonify(status=status)
