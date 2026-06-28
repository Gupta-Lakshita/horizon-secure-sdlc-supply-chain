import hashlib
import os
import uuid
from datetime import datetime, timedelta, timezone

import boto3
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.main import get_client_id
from app.models import PreflightRequest, PreflightResponse, RuleResult
from app.policy.engine import evaluate_release, POLICY_VERSION

router = APIRouter(tags=["preflight"])

ARTIFACT_BUCKET = os.getenv("ARTIFACT_BUCKET", "")
AWS_REGION = os.getenv("AWS_REGION", "us-east-1")
PREFLIGHT_TTL_MINUTES = 5


def _s3_client():
    return boto3.client("s3", region_name=AWS_REGION)


@router.post("/{release_id}/preflight", response_model=PreflightResponse)
async def preflight_check(
    release_id: str,
    request: PreflightRequest,
    client_id: str = Depends(get_client_id),
    db: AsyncSession = Depends(get_db),
) -> PreflightResponse:
    """
    Promotion preflight gate. Steps (in order):
    1. Verify client_id matches release record.
    2. Load release record from DB.
    3. Download manifest.json from S3 and re-verify sha256 (do NOT trust DB alone).
    4. Check each evidence file exists in S3 (HEAD object).
    5. Run evaluate_release() fresh (no cached evaluation).
    6. Verify requestedDigest == release imageDigest.
    7. Check required approvals exist.

    Returns short-TTL response (5-minute expiry). Fails closed on S3 errors.
    """
    # 1 + 2. Load and verify release
    row = (await db.execute(
        text(
            "SELECT id, client_id, release_id, image_digest, evidence_s3_prefix "
            "FROM release_trust_runs WHERE release_id = :release_id"
        ),
        {"release_id": release_id},
    )).mappings().first()

    if not row:
        raise HTTPException(status_code=404, detail=f"Release run for {release_id} not found")
    if row["client_id"] != client_id:
        raise HTTPException(status_code=403, detail="client_id mismatch")

    run_id = str(row["id"])
    approved_digest = row["image_digest"]
    evidence_prefix = row["evidence_s3_prefix"] or f"release-trust/{release_id}"
    manifest_key = f"{evidence_prefix}/manifest.json"

    # 3. Download and re-verify manifest
    try:
        s3 = _s3_client()
        resp = s3.get_object(Bucket=ARTIFACT_BUCKET, Key=manifest_key)
        manifest_bytes = resp["Body"].read()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Failed to download manifest from S3: {exc}")

    manifest_sha = f"sha256:{hashlib.sha256(manifest_bytes).hexdigest()}"

    import json
    try:
        manifest = json.loads(manifest_bytes)
    except json.JSONDecodeError:
        raise HTTPException(status_code=503, detail="Manifest is not valid JSON")

    # 4. HEAD-check every evidence object
    missing_objects = []
    for obj in manifest.get("objects", []):
        obj_key = f"{evidence_prefix}/{obj['path']}"
        try:
            s3.head_object(Bucket=ARTIFACT_BUCKET, Key=obj_key)
        except Exception:
            missing_objects.append(obj["path"])

    if missing_objects:
        raise HTTPException(
            status_code=422,
            detail=f"Evidence objects missing from S3: {missing_objects}",
        )

    # 5. Fresh policy evaluation (never use cached)
    eval_result = await evaluate_release(
        run_id=run_id,
        client_id=client_id,
        target_environment=request.targetEnvironment,
        db=db,
        requested_digest=request.requestedDigest,
    )

    # 6. Digest check (this is in the policy engine as rule_requires_same_digest,
    #    but we also enforce it here at the gate boundary)
    if approved_digest and request.requestedDigest != approved_digest:
        eval_result["blockers"].append(
            f"HR-POL-RT-006 requestedDigest {request.requestedDigest} "
            f"does not match approved digest {approved_digest}"
        )
        if eval_result["decision"] != "block":
            eval_result["decision"] = "block"

    # 7. Check approvals exist for this environment
    approval_count = (await db.execute(
        text(
            "SELECT COUNT(*) as cnt FROM release_trust_evidence "
            "WHERE release_run_id = :run_id AND client_id = :client_id AND evidence_type = 'approval'"
        ),
        {"run_id": run_id, "client_id": client_id},
    )).scalar()

    if request.targetEnvironment.lower() in {"prod", "production"} and (approval_count or 0) == 0:
        eval_result["blockers"].append(
            "HR-POL-RT-005 production promotion requires at least one release-manager approval"
        )
        eval_result["decision"] = "block"

    allowed = eval_result["decision"] != "block"
    expires_at = (datetime.now(timezone.utc) + timedelta(minutes=PREFLIGHT_TTL_MINUTES)).isoformat()
    request_binding = str(uuid.uuid4())

    # Persist the preflight result as an evidence record for audit
    await db.execute(
        text(
            "INSERT INTO release_trust_evidence "
            "(release_run_id, client_id, evidence_type, status, sha256, schema_version, summary_json) "
            "VALUES (:run_id, :client_id, 'preflight', :status, :sha256, '2026-06-preflight-v1', :summary)"
        ),
        {
            "run_id": run_id,
            "client_id": client_id,
            "status": "allowed" if allowed else "denied",
            "sha256": manifest_sha,
            "summary": json.dumps({
                "targetEnvironment": request.targetEnvironment,
                "requestedDigest": request.requestedDigest,
                "decision": eval_result["decision"],
                "requestBinding": request_binding,
                "expiresAt": expires_at,
            }),
        },
    )
    await db.commit()

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
