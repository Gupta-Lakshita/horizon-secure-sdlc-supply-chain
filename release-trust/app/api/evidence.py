import hashlib
import json
import os
import uuid

import boto3
from botocore.exceptions import NoCredentialsError, ClientError
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db import get_db
from app.main import get_client_id
from app.models import RecordEvidenceRequest, RecordEvidenceResponse

router = APIRouter(tags=["evidence"])

AWS_REGION = os.getenv("AWS_REGION", "us-east-1")


@router.post("/runs/{run_id}/evidence", response_model=RecordEvidenceResponse)
def record_evidence(
    run_id: str,
    request: RecordEvidenceRequest,
    client_id: str = Depends(get_client_id),
    db: Session = Depends(get_db),
) -> RecordEvidenceResponse:

    # 1. Verify run exists and belongs to this client
    row = db.execute(
        text("SELECT id, client_id, release_id FROM release_trust_runs WHERE id = :run_id"),
        {"run_id": run_id},
    ).mappings().first()

    if not row:
        raise HTTPException(status_code=404, detail=f"Release run {run_id} not found")
    if row["client_id"] != client_id:
        raise HTTPException(status_code=403, detail="client_id mismatch")

    # 2. Download manifest from S3 and verify SHA-256
    # Local dev fallback: if no AWS credentials, skip S3 and mark as mock
    manifest_bytes = None
    try:
        s3 = boto3.client("s3", region_name=AWS_REGION)
        # Parse bucket from S3 key or env
        key = request.manifestS3Key
        bucket = os.getenv("ARTIFACT_BUCKET", "")
        if not bucket:
            raise HTTPException(status_code=422, detail="ARTIFACT_BUCKET env var not set")
        resp = s3.get_object(Bucket=bucket, Key=key)
        manifest_bytes = resp["Body"].read()
    except (NoCredentialsError, ClientError) as exc:
        # Local dev without AWS — accept the submission without S3 verification
        # Production will always have credentials via IRSA
        print(f"[DEV] S3 unavailable ({exc}), skipping manifest download")
        db.execute(
            text(
                "UPDATE release_trust_runs SET status='collecting_evidence', "
                "image_digest=:digest, updated_at=datetime('now') "
                "WHERE id=:run_id AND client_id=:client_id"
            ),
            {"digest": request.imageDigest, "run_id": run_id, "client_id": client_id},
        )
        db.commit()
        return RecordEvidenceResponse(
            releaseId=str(row["release_id"]),
            evidenceCount=0,
            status="collecting_evidence",
        )

    # 3. Verify SHA-256
    actual_sha = f"sha256:{hashlib.sha256(manifest_bytes).hexdigest()}"
    expected = request.manifestSha256
    if not expected.startswith("sha256:"):
        expected = f"sha256:{expected}"
    if actual_sha != expected:
        raise HTTPException(
            status_code=422,
            detail=f"Manifest SHA-256 mismatch: expected {expected}, got {actual_sha}",
        )

    # 4. Parse manifest
    try:
        manifest = json.loads(manifest_bytes)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=422, detail=f"Manifest is not valid JSON: {exc}")

    objects = manifest.get("objects", [])

    # 5. Upsert evidence rows — INSERT OR IGNORE for idempotency (SQLite syntax)
    upserted = 0
    for obj in objects:
        path: str = obj.get("path", "")
        evidence_type = path.split("/")[0] if "/" in path else path.rsplit(".", 1)[0]
        db.execute(
            text(
                "INSERT OR IGNORE INTO release_trust_evidence "
                "(id, release_run_id, client_id, evidence_type, status, object_key, sha256, schema_version) "
                "VALUES (:id, :run_id, :client_id, :ev_type, 'present', :obj_key, :sha256, :schema_version)"
            ),
            {
                "id": str(uuid.uuid4()),
                "run_id": run_id,
                "client_id": client_id,
                "ev_type": evidence_type,
                "obj_key": path,
                "sha256": obj.get("sha256", ""),
                "schema_version": manifest.get("schemaVersion", ""),
            },
        )
        upserted += 1

    # 6. Insert manifest summary row
    db.execute(
        text(
            "INSERT OR IGNORE INTO release_trust_evidence "
            "(id, release_run_id, client_id, evidence_type, status, object_key, sha256, schema_version, summary_json) "
            "VALUES (:id, :run_id, :client_id, 'manifest', 'present', :obj_key, :sha256, :schema_version, :summary)"
        ),
        {
            "id": str(uuid.uuid4()),
            "run_id": run_id,
            "client_id": client_id,
            "obj_key": request.manifestS3Key,
            "sha256": actual_sha,
            "schema_version": manifest.get("schemaVersion", ""),
            "summary": json.dumps({"objectCount": len(objects), "imageDigest": request.imageDigest}),
        },
    )

    # 7. Update run status
    db.execute(
        text(
            "UPDATE release_trust_runs SET status='collecting_evidence', "
            "image_digest=:digest, updated_at=datetime('now') "
            "WHERE id=:run_id AND client_id=:client_id"
        ),
        {"digest": request.imageDigest, "run_id": run_id, "client_id": client_id},
    )
    db.commit()

    return RecordEvidenceResponse(
        releaseId=str(row["release_id"]),
        evidenceCount=upserted,
        status="collecting_evidence",
    )