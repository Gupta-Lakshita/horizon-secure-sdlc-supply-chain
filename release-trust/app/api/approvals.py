import json
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db import get_db
from app.main import get_client_id
from app.models import ApprovalRequest, ApprovalResponse

router = APIRouter(tags=["approvals"])


@router.post("/runs/{run_id}/approve", status_code=201, response_model=ApprovalResponse)
def approve_run(
    run_id: str,
    request: ApprovalRequest,
    client_id: str = Depends(get_client_id),
    db: Session = Depends(get_db),
) -> ApprovalResponse:
    row = db.execute(
        text("SELECT client_id, release_id FROM release_trust_runs WHERE id = :run_id"),
        {"run_id": run_id},
    ).mappings().first()

    if not row:
        raise HTTPException(status_code=404, detail=f"Release run {run_id} not found")
    if row["client_id"] != client_id:
        raise HTTPException(status_code=403, detail="client_id mismatch")

    now = datetime.now(timezone.utc).isoformat()
    approval_id = str(uuid.uuid4())
    summary = json.dumps({"approvedBy": request.approvedBy,
                          "targetEnvironment": request.targetEnvironment,
                          "notes": request.notes, "approvedAt": now})

    db.execute(
        text("INSERT INTO release_trust_evidence "
             "(id, release_run_id, client_id, evidence_type, status, schema_version, summary_json) "
             "VALUES (:id, :run_id, :client_id, 'approval', 'present', '2026-06-approval-v1', :summary)"),
        {"id": approval_id, "run_id": run_id, "client_id": client_id, "summary": summary},
    )
    db.execute(
        text("UPDATE release_trust_runs SET status='approved', updated_at=datetime('now') "
             "WHERE id=:run_id AND client_id=:client_id"),
        {"run_id": run_id, "client_id": client_id},
    )
    db.commit()

    return ApprovalResponse(id=approval_id, releaseId=str(row["release_id"]),
                            approvedBy=request.approvedBy,
                            targetEnvironment=request.targetEnvironment,
                            createdAt=now)


@router.get("/runs/{run_id}/approvals")
def list_approvals(
    run_id: str,
    client_id: str = Depends(get_client_id),
    db: Session = Depends(get_db),
):
    row = db.execute(
        text("SELECT client_id FROM release_trust_runs WHERE id = :run_id"),
        {"run_id": run_id},
    ).mappings().first()

    if not row:
        raise HTTPException(status_code=404, detail=f"Release run {run_id} not found")
    if row["client_id"] != client_id:
        raise HTTPException(status_code=403, detail="client_id mismatch")

    rows = db.execute(
        text("SELECT id, summary_json, created_at FROM release_trust_evidence "
             "WHERE release_run_id=:run_id AND client_id=:client_id AND evidence_type='approval' "
             "ORDER BY created_at"),
        {"run_id": run_id, "client_id": client_id},
    ).mappings().all()

    result = []
    for r in rows:
        try:
            summary = json.loads(r["summary_json"]) if r["summary_json"] else {}
        except (json.JSONDecodeError, TypeError):
            summary = {}
        result.append({"id": str(r["id"]), **summary, "createdAt": str(r["created_at"])})
    return result