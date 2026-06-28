from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.main import get_client_id
from app.models import BundleResponse

router = APIRouter(tags=["bundle"])


@router.get("/runs/{run_id}/bundle", response_model=BundleResponse)
async def get_bundle(
    run_id: str,
    client_id: str = Depends(get_client_id),
    db: AsyncSession = Depends(get_db),
) -> BundleResponse:
    row = (await db.execute(
        text(
            "SELECT client_id, release_id, image_digest FROM release_trust_runs WHERE id = :run_id"
        ),
        {"run_id": run_id},
    )).mappings().first()

    if not row:
        raise HTTPException(status_code=404, detail=f"Release run {run_id} not found")
    if row["client_id"] != client_id:
        raise HTTPException(status_code=403, detail="client_id mismatch")

    ev_rows = (await db.execute(
        text(
            "SELECT evidence_type, status, object_key, sha256, schema_version, created_at "
            "FROM release_trust_evidence WHERE release_run_id = :run_id AND client_id = :client_id "
            "ORDER BY created_at"
        ),
        {"run_id": run_id, "client_id": client_id},
    )).mappings().all()

    eval_rows = (await db.execute(
        text(
            "SELECT id, environment, policy_version, decision, evaluated_at "
            "FROM release_trust_evaluations WHERE release_run_id = :run_id AND client_id = :client_id "
            "ORDER BY evaluated_at DESC"
        ),
        {"run_id": run_id, "client_id": client_id},
    )).mappings().all()

    # Extract manifest SHA from evidence
    manifest_sha = None
    for e in ev_rows:
        if e["evidence_type"] == "manifest":
            manifest_sha = e["sha256"]
            break

    evidence = [
        {
            "evidenceType": e["evidence_type"],
            "status": e["status"],
            "objectKey": e["object_key"],
            "sha256": e["sha256"],
            "schemaVersion": e["schema_version"],
            "createdAt": e["created_at"].isoformat(),
        }
        for e in ev_rows
    ]

    evaluations = [
        {
            "id": str(e["id"]),
            "environment": e["environment"],
            "policyVersion": e["policy_version"],
            "decision": e["decision"],
            "evaluatedAt": e["evaluated_at"].isoformat(),
        }
        for e in eval_rows
    ]

    return BundleResponse(
        releaseId=str(row["release_id"]),
        imageDigest=row["image_digest"],
        manifestSha256=manifest_sha,
        evidenceCount=len(evidence),
        evidence=evidence,
        evaluations=evaluations,
    )
