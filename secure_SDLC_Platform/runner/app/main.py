import base64
import json
import os
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field


app = FastAPI(title="Horizon Thin Runner", version="0.1.0")


class RunnerRequest(BaseModel):
    pipelineType: Optional[str] = None
    pipelineKind: Optional[str] = None
    serviceName: Optional[str] = None
    requestId: str = Field(default_factory=lambda: str(uuid.uuid4()))
    clientId: Optional[str] = None
    installationId: Optional[str] = None
    requestedBy: Optional[str] = None
    jobName: Optional[str] = None
    buildNumber: Optional[str] = None
    buildUrl: Optional[str] = None
    executionMode: Optional[str] = None
    parameters: Dict[str, Any] = Field(default_factory=dict)
    payload: Dict[str, Any] = Field(default_factory=dict)


class RunnerResponse(BaseModel):
    status: str
    requestId: str
    planId: Optional[str] = None
    message: str
    executedActions: List[str] = Field(default_factory=list)


class RunnerConfig:
    client_id = os.getenv("HORIZON_CLIENT_ID", "")
    installation_id = os.getenv("HORIZON_INSTALLATION_ID", "")
    activation_token = os.getenv("HORIZON_ACTIVATION_TOKEN", "")
    plan_endpoint = os.getenv("HORIZON_EXECUTION_PLAN_ENDPOINT", "")
    event_endpoint = os.getenv("HORIZON_EXECUTION_EVENT_ENDPOINT", "")
    public_key_path = os.getenv("HORIZON_PUBLIC_KEY_PATH", "")
    allow_shell = os.getenv("HORIZON_RUNNER_ALLOW_SHELL", "false").lower() == "true"
    execute_actions = os.getenv("HORIZON_RUNNER_EXECUTE", "false").lower() == "true"
    work_dir = Path(os.getenv("HORIZON_RUNNER_WORK_DIR", os.getenv("HORIZON_RUNNER_WORKDIR", "/var/lib/horizon-runner")))
    timeout_seconds = int(os.getenv("HORIZON_RUNNER_ACTION_TIMEOUT_SECONDS", "1800"))


config = RunnerConfig()
config.work_dir.mkdir(parents=True, exist_ok=True)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def safe_file_token(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in ("-", "_", ".") else "-" for ch in value)[:160] or str(uuid.uuid4())


def load_public_key():
    if not config.public_key_path:
        return None
    path = Path(config.public_key_path)
    if not path.exists():
        raise HTTPException(status_code=500, detail="Configured public key path does not exist")
    return serialization.load_pem_public_key(path.read_bytes())


def verify_plan_signature(plan: Dict[str, Any]) -> None:
    signature = plan.get("signature")
    signed_payload = plan.get("signedPayload") or plan.get("plan")
    if not signature or not signed_payload:
        raise HTTPException(status_code=502, detail="Execution plan is missing signature or signed payload")

    public_key = load_public_key()
    if public_key is None:
        raise HTTPException(status_code=500, detail="Execution plan public key is not configured")

    signature_bytes = base64.b64decode(signature)
    payload_bytes = canonical_json(signed_payload)
    try:
        public_key.verify(signature_bytes, payload_bytes, padding.PKCS1v15(), hashes.SHA256())
    except InvalidSignature:
        try:
            public_key.verify(
                signature_bytes,
                payload_bytes,
                padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.MAX_LENGTH),
                hashes.SHA256(),
            )
        except InvalidSignature as exc:
            raise HTTPException(status_code=502, detail="Execution plan signature verification failed") from exc
    except ValueError as exc:
        raise HTTPException(status_code=502, detail="Execution plan signature could not be decoded") from exc


def normalize_request(request: RunnerRequest) -> Dict[str, Any]:
    body = request.model_dump(exclude_none=True)
    body["pipelineType"] = body.get("pipelineType") or body.get("pipelineKind") or "BUILD_DEPLOY"
    body["clientId"] = body.get("clientId") or config.client_id
    body["installationId"] = body.get("installationId") or config.installation_id
    body["payload"] = body.get("payload") or body.get("parameters") or {}
    return body


def validate_plan(plan: Dict[str, Any]) -> Dict[str, Any]:
    signed_payload = plan.get("signedPayload") or plan.get("plan") or plan
    expires_at = signed_payload.get("expiresAt") or signed_payload.get("expires_at")
    if expires_at and parse_time(expires_at) < utc_now():
        raise HTTPException(status_code=410, detail="Execution plan has expired")
    verify_plan_signature(plan)
    return signed_payload


def request_execution_plan(request: RunnerRequest) -> Dict[str, Any]:
    if not config.plan_endpoint:
        raise HTTPException(status_code=503, detail="Horizon execution plan endpoint is not configured")

    headers = {"Content-Type": "application/json"}
    if config.activation_token:
        headers["Authorization"] = f"Bearer {config.activation_token}"

    try:
        response = requests.post(config.plan_endpoint, json=normalize_request(request), headers=headers, timeout=30)
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail=f"Unable to reach Horizon execution service: {exc}") from exc

    if response.status_code >= 400:
        raise HTTPException(
            status_code=502,
            detail=f"Horizon execution service rejected request: {response.status_code} {response.text}",
        )
    plan = response.json()
    (config.work_dir / f"plan-{safe_file_token(request.requestId)}.json").write_text(json.dumps(plan, indent=2))
    return plan


def emit_event(event_type: str, request: RunnerRequest, plan_id: Optional[str], detail: Dict[str, Any]) -> None:
    event = {
        "eventType": event_type,
        "requestId": request.requestId,
        "planId": plan_id,
        "clientId": request.clientId or config.client_id,
        "installationId": request.installationId or config.installation_id,
        "pipelineType": request.pipelineType or request.pipelineKind,
        "detail": detail,
        "timestamp": utc_now().isoformat(),
    }
    (config.work_dir / f"event-{safe_file_token(request.requestId)}-{event_type}.json").write_text(json.dumps(event, indent=2))
    if not config.event_endpoint:
        return
    headers = {"Content-Type": "application/json"}
    if config.activation_token:
        headers["Authorization"] = f"Bearer {config.activation_token}"
    try:
        requests.post(config.event_endpoint, json=event, headers=headers, timeout=10)
    except requests.RequestException:
        pass


def execute_actions(actions: List[Dict[str, Any]]) -> List[str]:
    executed = []
    for idx, action in enumerate(actions):
        action_type = action.get("type") or action.get("action")
        name = action.get("name") or f"action-{idx + 1}"

        if action_type == "log":
            print(action.get("message", name), flush=True)
            executed.append(name)
            continue

        if action_type == "shell":
            if not config.allow_shell:
                raise HTTPException(status_code=403, detail="Shell actions are disabled for this runner")
            if not config.execute_actions:
                executed.append(f"{name}:planned")
                continue
            subprocess.run(
                action.get("command", ""),
                shell=True,
                check=True,
                cwd=str(config.work_dir),
                timeout=config.timeout_seconds,
            )
            executed.append(name)
            continue

        raise HTTPException(status_code=422, detail=f"Unsupported runner action type: {action_type}")
    return executed


@app.get("/healthz")
def healthz() -> Dict[str, Any]:
    return {
        "status": "ok",
        "mode": os.getenv("HORIZON_RUNNER_MODE", "signed-plan"),
        "clientId": config.client_id,
        "installationId": config.installation_id,
        "executionPlanEndpointConfigured": bool(config.plan_endpoint),
        "publicKeyConfigured": bool(config.public_key_path),
    }


@app.post("/v1/execute", response_model=RunnerResponse)
def execute(request: RunnerRequest) -> RunnerResponse:
    emit_event("requested", request, None, {"payloadKeys": sorted((request.payload or request.parameters).keys())})
    plan = request_execution_plan(request)
    signed_plan = validate_plan(plan)
    plan_id = signed_plan.get("planId") or signed_plan.get("plan_id") or plan.get("planId")
    actions = signed_plan.get("actions") or signed_plan.get("steps") or []
    executed = execute_actions(actions)
    emit_event("completed", request, plan_id, {"executedActions": executed})
    return RunnerResponse(
        status="completed",
        requestId=request.requestId,
        planId=plan_id,
        message="Execution plan completed",
        executedActions=executed,
    )


@app.post("/v1/validate")
async def validate(request: Request) -> Dict[str, Any]:
    plan = await request.json()
    signed_plan = validate_plan(plan)
    return {"status": "valid", "planId": signed_plan.get("planId") or signed_plan.get("plan_id")}
