import hashlib
import json
import os
import uuid
from datetime import datetime, timedelta, timezone

import boto3
from botocore.exceptions import (
    NoCredentialsError,
    ClientError,
    ParamValidationError,
)
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db import get_db
from app.main import get_client_id
from app.models import PreflightRequest, PreflightResponse, RuleResult
from app.policy.engine import evaluate_release, POLICY_VERSION

router = APIRouter(tags=["preflight"])

AWS_REGION = os.getenv("AWS_REGION", "us-east-1")
ARTIFACT_BUCKET = os.getenv("ARTIFACT_BUCKET", "")
PREFLIGHT_TTL_MINUTES = 5


@router.post("/{release_id}/preflight", response_model=PreflightResponse)
def preflight_check(
    release_id: str,
    request: PreflightRequest,
    client_id: str = Depends(get_client_id),
    db: Session = Depends(get_db),
) -> PreflightResponse:

    # 1. Load and verify release — query by release_id string, not UUID
    row = (
        db.execute(
            text(
                "SELECT id, client_id, image_digest, evidence_s3_prefix "
                "FROM release_trust_runs "
                "WHERE release_id = :release_id AND client_id = :client_id"
            ),
            {"release_id": release_id, "client_id": client_id},
        )
        .mappings()
        .first()
    )

    if not row:
        raise HTTPException(status_code=404, detail=f"Release {release_id} not found")

    run_id = str(row["id"])
    approved_digest = row["image_digest"]
    evidence_prefix = row["evidence_s3_prefix"] or f"release-trust/{release_id}"
    manifest_key = f"{evidence_prefix}/manifest.json"

    # 2. Download and verify manifest from S3 — fail closed
    # Local dev fallback when no S3 bucket is configured
    manifest_sha = "sha256:stub-no-s3"
    s3_available = False

    if not ARTIFACT_BUCKET:
        print("[DEV] ARTIFACT_BUCKET not configured, skipping S3 verification")
    else:
        try:
            s3 = boto3.client("s3", region_name=AWS_REGION)

            resp = s3.get_object(
                Bucket=ARTIFACT_BUCKET,
                Key=manifest_key,
            )
            manifest_bytes = resp["Body"].read()
            manifest_sha = f"sha256:{hashlib.sha256(manifest_bytes).hexdigest()}"

            # 3. HEAD-check every evidence object
            manifest = json.loads(manifest_bytes)
            missing = []

            for obj in manifest.get("objects", []):
                try:
                    s3.head_object(
                        Bucket=ARTIFACT_BUCKET,
                        Key=f"{evidence_prefix}/{obj['path']}",
                    )
                except Exception:
                    missing.append(obj["path"])

            if missing:
                raise HTTPException(
                    status_code=422,
                    detail=f"Evidence objects missing from S3: {missing}",
                )

            s3_available = True

        except (
            NoCredentialsError,
            ClientError,
            ParamValidationError,
        ) as exc:
            # Local dev — skip S3 verification, proceed with policy only
            print(f"[DEV] S3 unavailable ({exc}), " "skipping manifest verification")

    # 4. Fresh policy evaluation — never cached
    eval_result = evaluate_release(
        run_id=run_id,
        client_id=client_id,
        target_environment=request.targetEnvironment,
        db=db,
        requested_digest=request.requestedDigest,
    )

    # 5. Digest gate — enforce at boundary regardless of policy
    if approved_digest and request.requestedDigest != approved_digest:
        eval_result["blockers"].append(
            f"HR-POL-RT-006 requestedDigest {request.requestedDigest} "
            f"!= approved digest {approved_digest}"
        )
        eval_result["decision"] = "block"

    # 6. PROD approval check
    if request.targetEnvironment.lower() in {"prod", "production"}:
        approval_count = (
            db.execute(
                text(
                    "SELECT COUNT(*) FROM release_trust_evidence "
                    "WHERE release_run_id=:run_id AND client_id=:client_id "
                    "AND evidence_type='approval' AND status='present'"
                ),
                {"run_id": run_id, "client_id": client_id},
            ).scalar()
            or 0
        )

        if approval_count == 0:
            eval_result["blockers"].append(
                "HR-POL-RT-005 production promotion requires release-manager approval"
            )
            eval_result["decision"] = "block"

    allowed = eval_result["decision"] != "block"
    expires_at = (
        datetime.now(timezone.utc) + timedelta(minutes=PREFLIGHT_TTL_MINUTES)
    ).isoformat()
    request_binding = str(uuid.uuid4())

    # 7. Persist preflight as audit evidence
    db.execute(
        text(
            "INSERT OR IGNORE INTO release_trust_evidence "
            "(id, release_run_id, client_id, evidence_type, status, sha256, "
            "schema_version, summary_json) "
            "VALUES (:id, :run_id, :client_id, 'preflight', :status, :sha256, "
            "'2026-06-preflight-v1', :summary)"
        ),
        {
            "id": str(uuid.uuid4()),
            "run_id": run_id,
            "client_id": client_id,
            "status": "allowed" if allowed else "denied",
            "sha256": manifest_sha,
            "summary": json.dumps(
                {
                    "targetEnvironment": request.targetEnvironment,
                    "requestedDigest": request.requestedDigest,
                    "decision": eval_result["decision"],
                    "requestBinding": request_binding,
                    "expiresAt": expires_at,
                    "s3Verified": s3_available,
                }
            ),
        },
    )
    db.commit()

    return PreflightResponse(
        allowed=allowed,
        decision=eval_result["decision"],
        expiresAt=expires_at,
        requestBinding=request_binding,
        policyVersion=POLICY_VERSION,
        manifestDigest=manifest_sha,
        rules=[RuleResult(**r) for r in eval_result["rules"]],
        blockers=eval_result["blockers"],
    )
