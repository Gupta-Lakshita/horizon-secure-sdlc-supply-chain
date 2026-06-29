import json
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db import get_db
from app.main import get_client_id
from app.models import (
    ExceptionRequest,
    ExceptionApprovalRequest,
    ExceptionRevocationRequest,
    ExceptionResponse,
)

router = APIRouter(tags=["exceptions"])

NON_EXCEPTED_RULES = {"rule_requires_digest", "rule_requires_same_digest"}


def _get_exception_or_404(
    run_id: str,
    exception_id: str,
    client_id: str,
    db: Session,
) -> dict:
    """Load exception and verify ownership. Raises 404 or 403."""
    run = db.execute(
        text("SELECT client_id FROM release_trust_runs WHERE id = :run_id"),
        {"run_id": run_id},
    ).mappings().first()

    if not run:
        raise HTTPException(status_code=404, detail=f"Release run {run_id} not found")
    if run["client_id"] != client_id:
        raise HTTPException(status_code=403, detail="client_id mismatch")

    exc = db.execute(
        text(
            "SELECT id, rule_id, release_scope, environment_scope, reason, "
            "compensating_control, requester, approver, issue_reference, "
            "expires_at, status, revocation_history, created_at "
            "FROM release_trust_exceptions "
            "WHERE id = :exc_id AND release_run_id = :run_id AND client_id = :client_id"
        ),
        {"exc_id": exception_id, "run_id": run_id, "client_id": client_id},
    ).mappings().first()

    if not exc:
        raise HTTPException(status_code=404, detail=f"Exception {exception_id} not found")

    return dict(exc)


def _row_to_response(row: dict) -> ExceptionResponse:
    return ExceptionResponse(
        id=str(row["id"]),
        ruleId=row["rule_id"],
        status=row["status"],
        requester=row["requester"],
        approver=row.get("approver"),
        expiresAt=str(row["expires_at"]) if row.get("expires_at") else None,
        createdAt=str(row["created_at"]),
    )


@router.post("/runs/{run_id}/exceptions", status_code=201, response_model=ExceptionResponse)
def request_exception(
    run_id: str,
    request: ExceptionRequest,
    client_id: str = Depends(get_client_id),
    db: Session = Depends(get_db),
) -> ExceptionResponse:
    # Verify run ownership
    run = db.execute(
        text("SELECT client_id FROM release_trust_runs WHERE id = :run_id"),
        {"run_id": run_id},
    ).mappings().first()

    if not run:
        raise HTTPException(status_code=404, detail=f"Release run {run_id} not found")
    if run["client_id"] != client_id:
        raise HTTPException(status_code=403, detail="client_id mismatch")

    # Reject non-excepted rules
    if request.ruleId in NON_EXCEPTED_RULES:
        raise HTTPException(
            status_code=422,
            detail=f"Rule {request.ruleId} is not exception-eligible: "
                   f"identity and digest rules cannot be excepted",
        )

    exc_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()

    db.execute(
        text(
            "INSERT INTO release_trust_exceptions "
            "(id, release_run_id, client_id, rule_id, release_scope, environment_scope, "
            "reason, compensating_control, requester, issue_reference, expires_at, "
            "status, revocation_history) "
            "VALUES (:id, :run_id, :client_id, :rule_id, :release_scope, :env_scope, "
            ":reason, :compensating_control, :requester, :issue_reference, :expires_at, "
            "'requested', '[]')"
        ),
        {
            "id": exc_id,
            "run_id": run_id,
            "client_id": client_id,
            "rule_id": request.ruleId,
            "release_scope": request.releaseScope,
            "env_scope": request.environmentScope,
            "reason": request.reason,
            "compensating_control": request.compensatingControl,
            "requester": request.requester,
            "issue_reference": request.issueReference,
            "expires_at": request.expiresAt,
        },
    )
    db.commit()

    row = db.execute(
        text("SELECT id, rule_id, status, requester, approver, expires_at, created_at "
             "FROM release_trust_exceptions WHERE id = :id"),
        {"id": exc_id},
    ).mappings().first()

    return _row_to_response(dict(row))


@router.patch(
    "/runs/{run_id}/exceptions/{exception_id}/approve",
    response_model=ExceptionResponse,
)
def approve_exception(
    run_id: str,
    exception_id: str,
    request: ExceptionApprovalRequest,
    client_id: str = Depends(get_client_id),
    db: Session = Depends(get_db),
) -> ExceptionResponse:
    exc = _get_exception_or_404(run_id, exception_id, client_id, db)

    if exc["status"] not in {"requested"}:
        raise HTTPException(
            status_code=409,
            detail=f"Exception is already {exc['status']} and cannot be approved",
        )

    db.execute(
        text(
            "UPDATE release_trust_exceptions "
            "SET status='approved', approver=:approver "
            "WHERE id=:exc_id AND client_id=:client_id"
        ),
        {"approver": request.approver, "exc_id": exception_id, "client_id": client_id},
    )
    db.commit()

    updated = _get_exception_or_404(run_id, exception_id, client_id, db)
    return _row_to_response(updated)


@router.patch(
    "/runs/{run_id}/exceptions/{exception_id}/revoke",
    response_model=ExceptionResponse,
)
def revoke_exception(
    run_id: str,
    exception_id: str,
    request: ExceptionRevocationRequest,
    client_id: str = Depends(get_client_id),
    db: Session = Depends(get_db),
) -> ExceptionResponse:
    exc = _get_exception_or_404(run_id, exception_id, client_id, db)

    if exc["status"] == "revoked":
        raise HTTPException(status_code=409, detail="Exception is already revoked")

    # Append to revocation history
    try:
        history = json.loads(exc["revocation_history"] or "[]")
    except (json.JSONDecodeError, TypeError):
        history = []

    history.append({
        "revokedBy": request.revokedBy,
        "reason": request.reason,
        "revokedAt": datetime.now(timezone.utc).isoformat(),
    })

    db.execute(
        text(
            "UPDATE release_trust_exceptions "
            "SET status='revoked', revocation_history=:history "
            "WHERE id=:exc_id AND client_id=:client_id"
        ),
        {
            "history": json.dumps(history),
            "exc_id": exception_id,
            "client_id": client_id,
        },
    )
    db.commit()

    updated = _get_exception_or_404(run_id, exception_id, client_id, db)
    return _row_to_response(updated)


@router.get("/runs/{run_id}/exceptions", response_model=list[ExceptionResponse])
def list_exceptions(
    run_id: str,
    client_id: str = Depends(get_client_id),
    db: Session = Depends(get_db),
) -> list[ExceptionResponse]:
    run = db.execute(
        text("SELECT client_id FROM release_trust_runs WHERE id = :run_id"),
        {"run_id": run_id},
    ).mappings().first()

    if not run:
        raise HTTPException(status_code=404, detail=f"Release run {run_id} not found")
    if run["client_id"] != client_id:
        raise HTTPException(status_code=403, detail="client_id mismatch")

    rows = db.execute(
        text(
            "SELECT id, rule_id, status, requester, approver, expires_at, created_at "
            "FROM release_trust_exceptions "
            "WHERE release_run_id=:run_id AND client_id=:client_id "
            "ORDER BY created_at DESC"
        ),
        {"run_id": run_id, "client_id": client_id},
    ).mappings().all()

    return [_row_to_response(dict(r)) for r in rows]