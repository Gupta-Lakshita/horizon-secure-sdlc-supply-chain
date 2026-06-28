import json
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.main import get_client_id
from app.models import (
    ExceptionApprovalRequest,
    ExceptionRequest,
    ExceptionResponse,
    ExceptionRevocationRequest,
)

router = APIRouter(tags=["exceptions"])

# These rules are never exception-eligible (identity / digest / tenant violations)
_NON_EXCEPTED_RULES = {"rule_requires_digest", "rule_requires_same_digest"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@router.post("/runs/{run_id}/exceptions", status_code=201, response_model=ExceptionResponse)
async def request_exception(
    run_id: str,
    request: ExceptionRequest,
    client_id: str = Depends(get_client_id),
    db: AsyncSession = Depends(get_db),
) -> ExceptionResponse:
    row = (await db.execute(
        text("SELECT client_id FROM release_trust_runs WHERE id = :run_id"),
        {"run_id": run_id},
    )).mappings().first()

    if not row:
        raise HTTPException(status_code=404, detail=f"Release run {run_id} not found")
    if row["client_id"] != client_id:
        raise HTTPException(status_code=403, detail="client_id mismatch")

    if request.ruleId in _NON_EXCEPTED_RULES:
        raise HTTPException(
            status_code=422,
            detail=f"Rule {request.ruleId} is not exception-eligible (identity/digest/tenant violations cannot be excepted)",
        )

    result = (await db.execute(
        text(
            "INSERT INTO release_trust_exceptions "
            "(release_run_id, client_id, rule_id, release_scope, environment_scope, reason, "
            "compensating_control, requester, issue_reference, expires_at) "
            "VALUES (:run_id, :client_id, :rule_id, :rel_scope, :env_scope, :reason, "
            ":comp_ctrl, :requester, :issue_ref, :expires_at) "
            "RETURNING id, status, created_at"
        ),
        {
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
    )).mappings().first()
    await db.commit()

    return ExceptionResponse(
        id=str(result["id"]),
        ruleId=request.ruleId,
        status=result["status"],
        requester=request.requester,
        approver=None,
        expiresAt=request.expiresAt,
        createdAt=result["created_at"].isoformat(),
    )


@router.post("/runs/{run_id}/exceptions/{exception_id}/approve", response_model=ExceptionResponse)
async def approve_exception(
    run_id: str,
    exception_id: str,
    request: ExceptionApprovalRequest,
    client_id: str = Depends(get_client_id),
    db: AsyncSession = Depends(get_db),
) -> ExceptionResponse:
    exc_row = (await db.execute(
        text(
            "SELECT e.id, e.rule_id, e.status, e.requester, e.expires_at, e.created_at "
            "FROM release_trust_exceptions e "
            "JOIN release_trust_runs r ON r.id = e.release_run_id "
            "WHERE e.id = :exc_id AND e.release_run_id = :run_id AND r.client_id = :client_id"
        ),
        {"exc_id": exception_id, "run_id": run_id, "client_id": client_id},
    )).mappings().first()

    if not exc_row:
        raise HTTPException(status_code=404, detail=f"Exception {exception_id} not found")
    if exc_row["status"] != "requested":
        raise HTTPException(status_code=409, detail=f"Exception is already in status '{exc_row['status']}'")

    await db.execute(
        text(
            "UPDATE release_trust_exceptions SET status = 'approved', approver = :approver "
            "WHERE id = :exc_id AND client_id = :client_id"
        ),
        {"exc_id": exception_id, "client_id": client_id, "approver": request.approver},
    )
    await db.commit()

    return ExceptionResponse(
        id=str(exc_row["id"]),
        ruleId=exc_row["rule_id"],
        status="approved",
        requester=exc_row["requester"],
        approver=request.approver,
        expiresAt=exc_row["expires_at"].isoformat() if exc_row["expires_at"] else None,
        createdAt=exc_row["created_at"].isoformat(),
    )


@router.post("/runs/{run_id}/exceptions/{exception_id}/revoke", response_model=ExceptionResponse)
async def revoke_exception(
    run_id: str,
    exception_id: str,
    request: ExceptionRevocationRequest,
    client_id: str = Depends(get_client_id),
    db: AsyncSession = Depends(get_db),
) -> ExceptionResponse:
    exc_row = (await db.execute(
        text(
            "SELECT e.id, e.rule_id, e.status, e.requester, e.approver, e.expires_at, "
            "e.created_at, e.revocation_history "
            "FROM release_trust_exceptions e "
            "JOIN release_trust_runs r ON r.id = e.release_run_id "
            "WHERE e.id = :exc_id AND e.release_run_id = :run_id AND r.client_id = :client_id"
        ),
        {"exc_id": exception_id, "run_id": run_id, "client_id": client_id},
    )).mappings().first()

    if not exc_row:
        raise HTTPException(status_code=404, detail=f"Exception {exception_id} not found")

    history = list(exc_row["revocation_history"] or [])
    history.append({
        "revokedBy": request.revokedBy,
        "reason": request.reason,
        "revokedAt": _now(),
        "previousStatus": exc_row["status"],
    })

    await db.execute(
        text(
            "UPDATE release_trust_exceptions SET status = 'revoked', "
            "revocation_history = :history "
            "WHERE id = :exc_id AND client_id = :client_id"
        ),
        {"exc_id": exception_id, "client_id": client_id, "history": json.dumps(history)},
    )
    await db.commit()

    return ExceptionResponse(
        id=str(exc_row["id"]),
        ruleId=exc_row["rule_id"],
        status="revoked",
        requester=exc_row["requester"],
        approver=exc_row["approver"],
        expiresAt=exc_row["expires_at"].isoformat() if exc_row["expires_at"] else None,
        createdAt=exc_row["created_at"].isoformat(),
    )


@router.get("/runs/{run_id}/exceptions")
async def list_exceptions(
    run_id: str,
    client_id: str = Depends(get_client_id),
    db: AsyncSession = Depends(get_db),
):
    row = (await db.execute(
        text("SELECT client_id FROM release_trust_runs WHERE id = :run_id"),
        {"run_id": run_id},
    )).mappings().first()

    if not row:
        raise HTTPException(status_code=404, detail=f"Release run {run_id} not found")
    if row["client_id"] != client_id:
        raise HTTPException(status_code=403, detail="client_id mismatch")

    rows = (await db.execute(
        text(
            "SELECT id, rule_id, status, requester, approver, expires_at, created_at "
            "FROM release_trust_exceptions WHERE release_run_id = :run_id AND client_id = :client_id "
            "ORDER BY created_at DESC"
        ),
        {"run_id": run_id, "client_id": client_id},
    )).mappings().all()

    return [
        ExceptionResponse(
            id=str(r["id"]),
            ruleId=r["rule_id"],
            status=r["status"],
            requester=r["requester"],
            approver=r["approver"],
            expiresAt=r["expires_at"].isoformat() if r["expires_at"] else None,
            createdAt=r["created_at"].isoformat(),
        )
        for r in rows
    ]
