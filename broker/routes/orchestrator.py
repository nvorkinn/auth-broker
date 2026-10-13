"""What the orchestrator that starts renderers calls (see services/assignments.py), with its own bearer
token rather than a device secret."""

from flask import Blueprint, jsonify, request

from ..auth import require_orchestrator_token
from ..db import db
from ..models import RendererAssignment
from ..services import assignments

bp = Blueprint("orchestrator", __name__, url_prefix="/api/orchestrator")


@bp.get("/assignments")
@require_orchestrator_token
def list_assignments():
    """Every assignment that isn't deleted. `claim_token` is null once the renderer has claimed it."""
    return jsonify(
        assignments=[
            {"id": a.id, "slug": a.slug, "claim_token": a.claim_token, "status": a.status}
            for a in assignments.listing()
        ]
    )


@bp.post("/assignments/<assignment_id>/status")
@require_orchestrator_token
def report_status(assignment_id):
    """{"status": "provisioned" | "failed" | "deleted", "detail": ""}. `deleted` removes the assignment
    for good, and is only accepted for one that's being deleted."""
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
