from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db import get_db
from app.main import get_client_id
from app.models import BundleResponse

router = APIRouter(tags=["bundle"])


@router.get("/runs/{run_id}/bundle", response_model=BundleResponse)
def get_bundle(
    run_id: str,
    client_id: str = Depends(get_client_id),
    db: Session = Depends(get_db),
) -> BundleResponse:
    row = db.execute(
        text("SELECT client_id, release_id, image_digest FROM release_trust_runs WHERE id=:run_id"),
        {"run_id": run_id},
    ).mappings().first()

    if not row:
        raise HTTPException(status_code=404, detail=f"Release run {run_id} not found")
    if row["client_id"] != client_id:
        raise HTTPException(status_code=403, detail="client_id mismatch")

    ev_rows = db.execute(
        text("SELECT evidence_type, status, object_key, sha256, schema_version, created_at "
             "FROM release_trust_evidence "
             "WHERE release_run_id=:run_id AND client_id=:client_id ORDER BY created_at"),
        {"run_id": run_id, "client_id": client_id},
    ).mappings().all()

    eval_rows = db.execute(
        text("SELECT id, environment, policy_version, decision, evaluated_at "
             "FROM release_trust_evaluations "
             "WHERE release_run_id=:run_id AND client_id=:client_id ORDER BY evaluated_at DESC"),
        {"run_id": run_id, "client_id": client_id},
    ).mappings().all()

    manifest_sha = next(
        (e["sha256"] for e in ev_rows if e["evidence_type"] == "manifest"), None
    )

    return BundleResponse(
        releaseId=str(row["release_id"]),
        imageDigest=row["image_digest"],
        manifestSha256=manifest_sha,
        evidenceCount=len(ev_rows),
        evidence=[{"evidenceType": e["evidence_type"], "status": e["status"],
                   "objectKey": e["object_key"], "sha256": e["sha256"],
                   "schemaVersion": e["schema_version"], "createdAt": str(e["created_at"])}
                  for e in ev_rows],
        evaluations=[{"id": str(e["id"]), "environment": e["environment"],
                      "policyVersion": e["policy_version"], "decision": e["decision"],
                      "evaluatedAt": str(e["evaluated_at"])} for e in eval_rows],
    )