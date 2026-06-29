import json
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db import get_db_sync as get_db
from app.deps import get_client_id
from app.models import (
    ExceptionApprovalRequest,
    ExceptionRequest,
    ExceptionResponse,
    ExceptionRevocationRequest,
)

router = APIRouter(tags=["exceptions"])

_NON_EXCEPTED_RULES = {"rule_requires_digest", "rule_requires_same_digest"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _get_run_client_id(db: Session, run_id: str) -> str:
    row = db.execute(
        text("SELECT client_id FROM release_trust_runs WHERE id = :run_id"),
        {"run_id": run_id},
    ).mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail=f"Release run {run_id} not found")
    return row["client_id"]


def _get_exception(db: Session, exc_id: str, run_id: str, client_id: str):
    row = db.execute(
        text(
            "SELECT id, rule_id, status, requester, approver, expires_at, "
            "created_at, revocation_history "
            "FROM release_trust_exceptions "
            "WHERE id = :exc_id AND release_run_id = :run_id AND client_id = :client_id"
        ),
        {"exc_id": exc_id, "run_id": run_id, "client_id": client_id},
    ).mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail=f"Exception {exc_id} not found")
    return row


def _row_to_response(row) -> ExceptionResponse:
    expires = row["expires_at"]
    if expires and not isinstance(expires, str):
        expires = expires.isoformat()
    created = row["created_at"]
    if created and not isinstance(created, str):
        created = created.isoformat()
    return ExceptionResponse(
        id=str(row["id"]),
        ruleId=row["rule_id"],
        status=row["status"],
        requester=row["requester"],
        approver=row["approver"],
        expiresAt=expires,
        createdAt=created,
    )


@router.post("/runs/{run_id}/exceptions", status_code=201, response_model=ExceptionResponse)
def request_exception(
    run_id: str,
    request: ExceptionRequest,
    client_id: str = Depends(get_client_id),
    db: Session = Depends(get_db),
) -> ExceptionResponse:
    run_client_id = _get_run_client_id(db, run_id)
    if run_client_id != client_id:
        raise HTTPException(status_code=403, detail="client_id mismatch")

    if request.ruleId in _NON_EXCEPTED_RULES:
        raise HTTPException(
            status_code=422,
            detail="This rule is not exception-eligible: identity and digest rules cannot be excepted",
        )

    exc_id = str(uuid.uuid4())
    now = _now()

    db.execute(
        text(
            "INSERT OR IGNORE INTO release_trust_exceptions "
            "(id, release_run_id, client_id, rule_id, release_scope, environment_scope, "
            "reason, compensating_control, requester, issue_reference, expires_at, "
            "status, revocation_history, created_at) "
            "VALUES (:id, :run_id, :client_id, :rule_id, :rel_scope, :env_scope, "
            ":reason, :comp_ctrl, :requester, :issue_ref, :expires_at, "
            "'requested', '[]', datetime('now'))"
        ),
        {
            "id": exc_id,
            "run_id": run_id,
            "client_id": client_id,
            "rule_id": request.ruleId,
            "rel_scope": request.releaseScope,
            "env_scope": request.environmentScope,
            "reason": request.reason,
            "comp_ctrl": request.compensatingControl,
            "requester": request.requester,
            "issue_ref": request.issueReference,
            "expires_at": request.expiresAt,
        },
    )
    db.commit()

    row = db.execute(
        text(
            "SELECT id, rule_id, status, requester, approver, expires_at, created_at "
            "FROM release_trust_exceptions WHERE id = :id"
        ),
        {"id": exc_id},
    ).mappings().first()

    return _row_to_response(row)


@router.patch("/runs/{run_id}/exceptions/{exception_id}/approve", response_model=ExceptionResponse)
def approve_exception(
    run_id: str,
    exception_id: str,
    request: ExceptionApprovalRequest,
    client_id: str = Depends(get_client_id),
    db: Session = Depends(get_db),
) -> ExceptionResponse:
    run_client_id = _get_run_client_id(db, run_id)
    if run_client_id != client_id:
        raise HTTPException(status_code=403, detail="client_id mismatch")

    exc_row = _get_exception(db, exception_id, run_id, client_id)

    db.execute(
        text(
            "UPDATE release_trust_exceptions "
            "SET status = 'approved', approver = :approver "
            "WHERE id = :exc_id AND client_id = :client_id"
        ),
        {"exc_id": exception_id, "client_id": client_id, "approver": request.approver},
    )
    db.commit()

    updated = db.execute(
        text(
            "SELECT id, rule_id, status, requester, approver, expires_at, created_at "
            "FROM release_trust_exceptions WHERE id = :id"
        ),
        {"id": exception_id},
    ).mappings().first()

    return _row_to_response(updated)


@router.patch("/runs/{run_id}/exceptions/{exception_id}/revoke", response_model=ExceptionResponse)
def revoke_exception(
    run_id: str,
    exception_id: str,
    request: ExceptionRevocationRequest,
    client_id: str = Depends(get_client_id),
    db: Session = Depends(get_db),
) -> ExceptionResponse:
    run_client_id = _get_run_client_id(db, run_id)
    if run_client_id != client_id:
        raise HTTPException(status_code=403, detail="client_id mismatch")

    exc_row = _get_exception(db, exception_id, run_id, client_id)

    history = exc_row["revocation_history"]
    if isinstance(history, str):
        history = json.loads(history) if history else []
    elif history is None:
        history = []
    history.append({
        "revokedBy": request.revokedBy,
        "reason": request.reason,
        "revokedAt": _now(),
        "previousStatus": exc_row["status"],
    })

    db.execute(
        text(
            "UPDATE release_trust_exceptions "
            "SET status = 'revoked', revocation_history = :history "
            "WHERE id = :exc_id AND client_id = :client_id"
        ),
        {
            "exc_id": exception_id,
            "client_id": client_id,
            "history": json.dumps(history),
        },
    )
    db.commit()

    updated = db.execute(
        text(
            "SELECT id, rule_id, status, requester, approver, expires_at, created_at "
            "FROM release_trust_exceptions WHERE id = :id"
        ),
        {"id": exception_id},
    ).mappings().first()

    return _row_to_response(updated)


@router.get("/runs/{run_id}/exceptions", response_model=list[ExceptionResponse])
def list_exceptions(
    run_id: str,
    client_id: str = Depends(get_client_id),
    db: Session = Depends(get_db),
) -> list[ExceptionResponse]:
    run_client_id = _get_run_client_id(db, run_id)
    if run_client_id != client_id:
        raise HTTPException(status_code=403, detail="client_id mismatch")

    rows = db.execute(
        text(
            "SELECT id, rule_id, status, requester, approver, expires_at, created_at "
            "FROM release_trust_exceptions "
            "WHERE release_run_id = :run_id AND client_id = :client_id "
            "ORDER BY created_at DESC"
        ),
        {"run_id": run_id, "client_id": client_id},
    ).mappings().all()

    return [_row_to_response(r) for r in rows]
