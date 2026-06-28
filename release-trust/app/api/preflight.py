import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.db import get_db
from app.main import get_client_id
from app.models import PreflightRequest, PreflightResponse, RuleResult
from app.policy.engine import POLICY_VERSION

router = APIRouter(tags=["preflight"])


@router.post("/{release_id}/preflight", response_model=PreflightResponse)
def preflight_check(
    release_id: str,
    request: PreflightRequest,
    client_id: str = Depends(get_client_id),
    db: Session = Depends(get_db),
) -> PreflightResponse:
    # Full implementation in Phase 3 — stub returns observe-mode pass for local dev
    expires_at = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
    return PreflightResponse(
        allowed=True,
        decision="pass",
        expiresAt=expires_at,
        requestBinding=str(uuid.uuid4()),
        policyVersion=POLICY_VERSION,
        manifestDigest="sha256:stub",
        rules=[],
        blockers=[],
    )