import base64
import hashlib
import hmac
import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel


app = FastAPI(title="Horizon Relevance License Server")


class LicenseSyncRequest(BaseModel):
    client_id: str
    client_name: Optional[str] = None
    activation_token: str
    current_license_key: Optional[str] = None
    current_expires_at: Optional[str] = None
    force: bool = False
    platform: Dict[str, Any] = {}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def split_csv(value: str) -> List[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def canonical_payload(payload: Dict[str, Any]) -> bytes:
    signed_payload = {k: v for k, v in payload.items() if k not in {"signature", "license_signature"}}
    return json.dumps(signed_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sign_license(payload: Dict[str, Any], secret: str) -> str:
    digest = hmac.new(secret.encode("utf-8"), canonical_payload(payload), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode("utf-8").rstrip("=")


def client_subscription(client_id: str) -> Dict[str, Any]:
    if client_id == "regeneron-healthcare":
        return {
            "client_id": "regeneron-healthcare",
            "client_name": "Regeneron",
            "activation_token": os.getenv("REGENERON_ACTIVATION_TOKEN", "regeneron-demo-token"),
            "license_type": os.getenv("REGENERON_LICENSE_TYPE", "trial"),
            "trial_days": int(os.getenv("REGENERON_TRIAL_DAYS", "2")),
            "enabled_pipelines": split_csv(os.getenv("REGENERON_ENABLED_PIPELINES", "Devops Pipeline,Test Devops Pipeline")),
            "enabled_features": split_csv(os.getenv("REGENERON_ENABLED_FEATURES", "build,artifact_publish,code_scan,image_scan,policy_validation,static_application_security,test_suites,notifications")),
            "allowed_environments": split_csv(os.getenv("REGENERON_ALLOWED_ENVIRONMENTS", "DEV,QA")),
            "max_repos": int(os.getenv("REGENERON_MAX_REPOS", "3")),
            "max_builds_per_month": int(os.getenv("REGENERON_MAX_BUILDS_PER_MONTH", "100")),
            "max_users": int(os.getenv("REGENERON_MAX_USERS", "10")),
            "status": os.getenv("REGENERON_SUBSCRIPTION_STATUS", "active"),
        }
    return {}


@app.get("/health")
def health():
    return {"status": "ok", "service": "horizon-license-server"}


@app.post("/api/v1/licenses/sync")
def sync_license(request: LicenseSyncRequest):
    signing_secret = os.getenv("LICENSE_SIGNING_SECRET", "").strip()
    if not signing_secret:
        return JSONResponse(status_code=500, content={"status": "sync_failed", "error": "License signing secret is not configured."})

    subscription = client_subscription(request.client_id)
    if not subscription:
        return JSONResponse(status_code=404, content={"status": "sync_failed", "error": "Client subscription was not found."})

    if subscription.get("status") != "active":
        return JSONResponse(status_code=403, content={"status": "sync_denied", "error": "Client subscription is not active."})

    expected_token = subscription.get("activation_token", "")
    if not hmac.compare_digest(request.activation_token, expected_token):
        return JSONResponse(status_code=403, content={"status": "sync_denied", "error": "Activation token is invalid."})

    issued_at = utc_now()
    expires_at = issued_at + timedelta(days=subscription["trial_days"])
    license_doc = {
        "client_id": subscription["client_id"],
        "client_name": subscription["client_name"],
        "license_key": f"hr-{subscription['client_id']}-{uuid.uuid4().hex[:12]}",
        "license_type": subscription["license_type"],
        "issued_at": iso(issued_at),
        "expires_at": iso(expires_at),
        "enabled_pipelines": subscription["enabled_pipelines"],
        "enabled_features": subscription["enabled_features"],
        "allowed_environments": subscription["allowed_environments"],
        "max_repos": subscription["max_repos"],
        "max_builds_per_month": subscription["max_builds_per_month"],
        "max_users": subscription["max_users"],
        "license_mode": "online-sync",
        "issuer": "Horizon Relevance",
    }
    license_doc["signature"] = sign_license(license_doc, signing_secret)

    return {
        "status": "synced",
        "message": f"{subscription['license_type'].title()} license synced. Expires at {license_doc['expires_at']}.",
        "license": license_doc,
    }

