import json
import os
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field


app = FastAPI(title="Horizon Release Trust Service", version="0.1.0")


class ReleaseTrustConfig:
    client_id = os.getenv("HORIZON_CLIENT_ID", "")
    installation_id = os.getenv("HORIZON_INSTALLATION_ID", "")
    work_dir = Path(os.getenv("HORIZON_TRUST_WORK_DIR", "/var/lib/horizon-release-trust"))
    default_role_arn = os.getenv("HORIZON_TRUST_ROLE_ARN", "")
    default_region = os.getenv("HORIZON_TRUST_AWS_REGION", "us-east-1")


config = ReleaseTrustConfig()
config.work_dir.mkdir(parents=True, exist_ok=True)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True))


def run_command(
    args: List[str],
    *,
    env: Optional[Dict[str, str]] = None,
    check: bool = True,
) -> subprocess.CompletedProcess:
    print(f"$ {' '.join(args)}", flush=True)
    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)
    result = subprocess.run(args, env=merged_env, capture_output=True, text=True, timeout=60)
    if result.stdout:
        print(result.stdout, flush=True)
    if result.stderr:
        print(result.stderr, flush=True)
    if check and result.returncode != 0:
        raise HTTPException(
            status_code=500,
            detail=f"Command failed ({result.returncode}): {' '.join(args)}",
        )
    return result


def assume_role_env(role_arn: str, region: str, session_name: str) -> Dict[str, str]:
    if not role_arn:
        return {"AWS_REGION": region, "AWS_DEFAULT_REGION": region}
    result = run_command([
        "aws", "sts", "assume-role",
        "--role-arn", role_arn,
        "--role-session-name", session_name,
        "--query", "Credentials",
        "--output", "json",
        "--region", region,
    ])
    creds = json.loads(result.stdout)
    return {
        "AWS_ACCESS_KEY_ID": creds["AccessKeyId"],
        "AWS_SECRET_ACCESS_KEY": creds["SecretAccessKey"],
        "AWS_SESSION_TOKEN": creds["SessionToken"],
        "AWS_REGION": region,
        "AWS_DEFAULT_REGION": region,
    }


def resolve_client_id(x_client_id: Optional[str]) -> str:
    return x_client_id or config.client_id


def validate_image_digest(image_digest: str) -> None:
    """HR-POL-PROD-002: containers must use immutable sha256 digest."""
    if not image_digest.startswith("sha256:") or len(image_digest) != 71:
        raise HTTPException(
            status_code=422,
            detail=(
                "HR-POL-PROD-002 image reference must be an immutable sha256 digest "
                "(sha256:<64 hex characters>)"
            ),
        )


def digest_slug(image_digest: str) -> str:
    return image_digest.replace("sha256:", "sha256-")


class AttestRequest(BaseModel):
    imageDigest: str
    imageRepo: str
    targetEnv: str
    stage: str
    attestedBy: Optional[str] = None
    changeTicket: Optional[str] = None
    evidenceBucket: str
    evidencePrefix: str
    evidenceRegion: str
    roleArn: Optional[str] = None
    attestationId: str = Field(default_factory=lambda: str(uuid.uuid4()))


class AttestResponse(BaseModel):
    status: str
    attestationId: str
    imageDigest: str
    targetEnv: str
    stage: str
    recordedAt: str
    evidencePath: str


class GateRequest(BaseModel):
    imageDigest: str
    imageRepo: str
    targetEnv: str
    changeTicket: Optional[str] = None
    requiredStages: List[str] = Field(default_factory=list)
    evidenceBucket: str
    evidencePrefix: str
    evidenceRegion: str
    roleArn: Optional[str] = None
    requestId: str = Field(default_factory=lambda: str(uuid.uuid4()))


class PolicyViolation(BaseModel):
    policyId: str
    message: str


class GateResponse(BaseModel):
    status: str
    requestId: str
    imageDigest: str
    targetEnv: str
    evaluatedAt: str
    violations: List[PolicyViolation] = Field(default_factory=list)
    attestationCount: int = 0
    evidencePath: str


@app.get("/healthz")
def healthz() -> Dict[str, Any]:
    return {
        "status": "ok",
        "service": "horizon-release-trust",
        "version": "0.1.0",
        "clientId": config.client_id,
        "installationId": config.installation_id,
    }


@app.post("/v1/attest", response_model=AttestResponse)
def attest(
    request: AttestRequest,
    x_client_id: Optional[str] = Header(default=None),
) -> AttestResponse:
    client_id = resolve_client_id(x_client_id)
    validate_image_digest(request.imageDigest)

    region = request.evidenceRegion
    role_env = assume_role_env(
        request.roleArn or config.default_role_arn,
        region,
        f"horizon-trust-attest-{request.attestationId[:8]}",
    )

    recorded_at = utc_now().isoformat()
    attestation = {
        "attestationId": request.attestationId,
        "clientId": client_id,
        "imageDigest": request.imageDigest,
        "imageRepo": request.imageRepo,
        "targetEnv": request.targetEnv,
        "stage": request.stage,
        "attestedBy": request.attestedBy or "horizon-release-trust",
        "changeTicket": request.changeTicket,
        "recordedAt": recorded_at,
        "service": "horizon-release-trust",
        "version": "0.1.0",
    }

    local_path = config.work_dir / f"attest-{request.attestationId}.json"
    write_json(local_path, attestation)

    slug = digest_slug(request.imageDigest)
    s3_key = (
        f"{request.evidencePrefix.strip('/')}/trust"
        f"/{slug}/{request.stage}/attestation.json"
    )
    run_command(
        ["aws", "s3", "cp", str(local_path), f"s3://{request.evidenceBucket}/{s3_key}", "--region", region],
        env=role_env,
    )
    local_path.unlink(missing_ok=True)

    return AttestResponse(
        status="attested",
        attestationId=request.attestationId,
        imageDigest=request.imageDigest,
        targetEnv=request.targetEnv,
        stage=request.stage,
        recordedAt=recorded_at,
        evidencePath=f"s3://{request.evidenceBucket}/{s3_key}",
    )


@app.post("/v1/gate", response_model=GateResponse)
def gate(
    request: GateRequest,
    x_client_id: Optional[str] = Header(default=None),
) -> GateResponse:
    client_id = resolve_client_id(x_client_id)
    violations: List[Dict[str, Any]] = []
    evaluated_at = utc_now().isoformat()

    # HR-POL-PROD-002: production containers must reference images by immutable digest
    if not request.imageDigest.startswith("sha256:"):
        violations.append({
            "policyId": "HR-POL-PROD-002",
            "message": (
                f"HR-POL-PROD-002 production container {request.imageRepo} "
                "must use immutable image digest"
            ),
        })

    # HR-POL-PROD-001: production deployments require a change-ticket
    if request.targetEnv.lower() in {"prod", "production"} and not request.changeTicket:
        violations.append({
            "policyId": "HR-POL-PROD-001",
            "message": (
                "HR-POL-PROD-001 production releases require a change-ticket reference"
            ),
        })

    region = request.evidenceRegion
    role_env = assume_role_env(
        request.roleArn or config.default_role_arn,
        region,
        f"horizon-trust-gate-{request.requestId[:8]}",
    )

    slug = digest_slug(request.imageDigest)
    trust_prefix = f"{request.evidencePrefix.strip('/')}/trust/{slug}"

    attestations: List[Dict[str, Any]] = []
    list_result = run_command(
        ["aws", "s3", "ls", f"s3://{request.evidenceBucket}/{trust_prefix}/", "--region", region, "--recursive"],
        env=role_env,
        check=False,
    )
    if list_result.returncode == 0:
        for line in list_result.stdout.splitlines():
            parts = line.strip().split()
            if len(parts) >= 4 and parts[-1].endswith("attestation.json"):
                s3_key = parts[-1]
                local_attest = config.work_dir / f"gate-{request.requestId}-{uuid.uuid4().hex[:8]}.json"
                dl = run_command(
                    ["aws", "s3", "cp", f"s3://{request.evidenceBucket}/{s3_key}", str(local_attest), "--region", region],
                    env=role_env,
                    check=False,
                )
                if dl.returncode == 0 and local_attest.exists():
                    try:
                        attestations.append(json.loads(local_attest.read_text()))
                    except json.JSONDecodeError:
                        pass
                    local_attest.unlink(missing_ok=True)

    # HR-POL-TRUST-001: all required pipeline stages must have an attestation
    attested_stages = {a.get("stage") for a in attestations}
    for required_stage in request.requiredStages:
        if required_stage not in attested_stages:
            violations.append({
                "policyId": "HR-POL-TRUST-001",
                "message": (
                    f"HR-POL-TRUST-001 required attestation for stage "
                    f"'{required_stage}' is missing"
                ),
            })

    status = "DENIED" if violations else "APPROVED"

    gate_decision = {
        "requestId": request.requestId,
        "clientId": client_id,
        "imageDigest": request.imageDigest,
        "imageRepo": request.imageRepo,
        "targetEnv": request.targetEnv,
        "changeTicket": request.changeTicket,
        "status": status,
        "violations": violations,
        "attestationCount": len(attestations),
        "evaluatedAt": evaluated_at,
        "service": "horizon-release-trust",
    }

    local_gate = config.work_dir / f"gate-{request.requestId}.json"
    write_json(local_gate, gate_decision)

    s3_gate_key = (
        f"{request.evidencePrefix.strip('/')}/trust"
        f"/{slug}/gate/{request.requestId}/decision.json"
    )
    run_command(
        ["aws", "s3", "cp", str(local_gate), f"s3://{request.evidenceBucket}/{s3_gate_key}", "--region", region],
        env=role_env,
    )
    local_gate.unlink(missing_ok=True)

    return GateResponse(
        status=status,
        requestId=request.requestId,
        imageDigest=request.imageDigest,
        targetEnv=request.targetEnv,
        evaluatedAt=evaluated_at,
        violations=[PolicyViolation(**v) for v in violations],
        attestationCount=len(attestations),
        evidencePath=f"s3://{request.evidenceBucket}/{s3_gate_key}",
    )
