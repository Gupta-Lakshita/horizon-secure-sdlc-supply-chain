from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.main import get_client_id
from app.models import ApprovalRequest, ApprovalResponse

router = APIRouter(tags=["approvals"])


@router.post("/runs/{run_id}/approve", status_code=201, response_model=ApprovalResponse)
async def approve_run(
    run_id: str,
    request: ApprovalRequest,
    client_id: str = Depends(get_client_id),
    db: AsyncSession = Depends(get_db),
) -> ApprovalResponse:
    row = (await db.execute(
        text("SELECT client_id, release_id FROM release_trust_runs WHERE id = :run_id"),
        {"run_id": run_id},
    )).mappings().first()

    if not row:
        raise HTTPException(status_code=404, detail=f"Release run {run_id} not found")
    if row["client_id"] != client_id:
        raise HTTPException(status_code=403, detail="client_id mismatch")

    # Record approval as a release_trust_evidence row (type="approval")
    now = datetime.now(timezone.utc).isoformat()
    import json
    summary = json.dumps({
        "approvedBy": request.approvedBy,
        "targetEnvironment": request.targetEnvironment,
        "notes": request.notes,
        "approvedAt": now,
    })
    result = (await db.execute(
        text(
            "INSERT INTO release_trust_evidence "
            "(release_run_id, client_id, evidence_type, status, schema_version, summary_json) "
            "VALUES (:run_id, :client_id, 'approval', 'present', '2026-06-approval-v1', :summary) "
            "RETURNING id, created_at"
        ),
        {"run_id": run_id, "client_id": client_id, "summary": summary},
    )).mappings().first()

    await db.execute(
        text(
            "UPDATE release_trust_runs SET status = 'approved', updated_at = NOW() "
            "WHERE id = :run_id AND client_id = :client_id"
        ),
        {"run_id": run_id, "client_id": client_id},
    )
    await db.commit()

    return ApprovalResponse(
        id=str(result["id"]),
        releaseId=str(row["release_id"]),
        approvedBy=request.approvedBy,
        targetEnvironment=request.targetEnvironment,
        createdAt=result["created_at"].isoformat(),
    )


@router.get("/runs/{run_id}/approvals")
async def list_approvals(
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
            "SELECT id, summary_json, created_at FROM release_trust_evidence "
            "WHERE release_run_id = :run_id AND client_id = :client_id AND evidence_type = 'approval' "
            "ORDER BY created_at"
        ),
        {"run_id": run_id, "client_id": client_id},
    )).mappings().all()

    return [
        {"id": str(r["id"]), **(r["summary_json"] or {}), "createdAt": r["created_at"].isoformat()}
        for r in rows
    ]
