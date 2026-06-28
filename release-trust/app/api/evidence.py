import hashlib
import json
import os

import boto3
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.main import get_client_id
from app.models import RecordEvidenceRequest, RecordEvidenceResponse

router = APIRouter(tags=["evidence"])

ARTIFACT_BUCKET = os.getenv("ARTIFACT_BUCKET", "")
AWS_REGION = os.getenv("AWS_REGION", "us-east-1")


def _s3_client():
    return boto3.client("s3", region_name=AWS_REGION)


@router.post("/runs/{run_id}/evidence", response_model=RecordEvidenceResponse)
async def record_evidence(
    run_id: str,
    request: RecordEvidenceRequest,
    client_id: str = Depends(get_client_id),
    db: AsyncSession = Depends(get_db),
) -> RecordEvidenceResponse:
    # Verify run belongs to this client
    row = (await db.execute(
        text(
            "SELECT id, client_id, release_id FROM release_trust_runs "
            "WHERE id = :run_id"
        ),
        {"run_id": run_id},
    )).mappings().first()

    if not row:
        raise HTTPException(status_code=404, detail=f"Release run {run_id} not found")
    if row["client_id"] != client_id:
        raise HTTPException(status_code=403, detail="client_id mismatch")

    # Download manifest from S3 and verify SHA-256
    try:
        s3 = _s3_client()
        resp = s3.get_object(Bucket=ARTIFACT_BUCKET, Key=request.manifestS3Key)
        manifest_bytes = resp["Body"].read()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Failed to download manifest from S3: {exc}")

    actual_sha = f"sha256:{hashlib.sha256(manifest_bytes).hexdigest()}"
    if actual_sha != request.manifestSha256:
        raise HTTPException(
            status_code=422,
            detail=f"Manifest SHA-256 mismatch: expected {request.manifestSha256}, got {actual_sha}",
        )

    try:
        manifest = json.loads(manifest_bytes)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=422, detail=f"Manifest is not valid JSON: {exc}")

    objects = manifest.get("objects", [])

    # Upsert evidence rows — one per object path
    upserted = 0
    for obj in objects:
        path: str = obj.get("path", "")
        sha256: str = obj.get("sha256", "")
        # Derive evidence_type from path prefix (e.g. "sbom/sbom.cyclonedx.json" → "sbom")
        evidence_type = path.split("/")[0] if "/" in path else path.rsplit(".", 1)[0]
        schema_version = manifest.get("schemaVersion", "")

        await db.execute(
            text(
                "INSERT INTO release_trust_evidence "
                "(release_run_id, client_id, evidence_type, status, object_key, sha256, schema_version) "
                "VALUES (:run_id, :client_id, :ev_type, 'present', :obj_key, :sha256, :schema_version) "
                "ON CONFLICT DO NOTHING"
            ),
            {
                "run_id": run_id,
                "client_id": client_id,
                "ev_type": evidence_type,
                "obj_key": obj.get("path"),
                "sha256": sha256,
                "schema_version": schema_version,
            },
        )
        upserted += 1

    # Also store summary row for the manifest itself
    await db.execute(
        text(
            "INSERT INTO release_trust_evidence "
            "(release_run_id, client_id, evidence_type, status, object_key, sha256, schema_version, summary_json) "
            "VALUES (:run_id, :client_id, 'manifest', 'present', :obj_key, :sha256, :schema_version, :summary) "
            "ON CONFLICT DO NOTHING"
        ),
        {
            "run_id": run_id,
            "client_id": client_id,
            "obj_key": request.manifestS3Key,
            "sha256": request.manifestSha256,
            "schema_version": manifest.get("schemaVersion", ""),
            "summary": json.dumps({"objectCount": len(objects), "imageDigest": request.imageDigest}),
        },
    )

    # Update run status and image_digest if provided
    update_params: dict = {"run_id": run_id, "client_id": client_id}
    if request.imageDigest:
        await db.execute(
            text(
                "UPDATE release_trust_runs SET status = 'collecting_evidence', "
                "image_digest = :digest, updated_at = NOW() "
                "WHERE id = :run_id AND client_id = :client_id"
            ),
            {**update_params, "digest": request.imageDigest},
        )
    else:
        await db.execute(
            text(
                "UPDATE release_trust_runs SET status = 'collecting_evidence', updated_at = NOW() "
                "WHERE id = :run_id AND client_id = :client_id"
            ),
            update_params,
        )

    await db.commit()

    return RecordEvidenceResponse(
        releaseId=str(row["release_id"]),
        evidenceCount=upserted,
        status="collecting_evidence",
    )


@router.get("/runs/{run_id}/evidence")
async def list_evidence(
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

    ev_rows = (await db.execute(
        text(
            "SELECT evidence_type, status, object_key, sha256, schema_version, created_at "
            "FROM release_trust_evidence WHERE release_run_id = :run_id AND client_id = :client_id "
            "ORDER BY created_at"
        ),
        {"run_id": run_id, "client_id": client_id},
    )).mappings().all()

    return [
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
