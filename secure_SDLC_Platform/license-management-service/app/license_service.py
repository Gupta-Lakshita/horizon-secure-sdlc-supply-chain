import base64
import hashlib
import hmac
import json
import secrets
import uuid
from datetime import datetime, timezone
from typing import Any, Dict

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import models
from .settings import get_settings


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def canonical_payload(payload: Dict[str, Any]) -> bytes:
    signed_payload = {k: v for k, v in payload.items() if k not in {"signature", "license_signature"}}
    return json.dumps(signed_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def generate_activation_token() -> str:
    return secrets.token_urlsafe(32)


def hash_activation_token(token: str) -> str:
    settings = get_settings()
    digest = hmac.new(settings.token_pepper.encode("utf-8"), token.encode("utf-8"), hashlib.sha256).hexdigest()
    return digest


def sign_license(payload: Dict[str, Any]) -> str:
    settings = get_settings()
    if settings.signing_mode == "aws-kms":
        return sign_with_kms(payload)

    digest = hmac.new(settings.signing_secret.encode("utf-8"), canonical_payload(payload), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode("utf-8").rstrip("=")


def sign_with_kms(payload: Dict[str, Any]) -> str:
    settings = get_settings()
    if not settings.kms_key_id:
        raise HTTPException(status_code=500, detail="KMS signing mode is enabled but kms_key_id is not configured.")

    import boto3

    digest = hashlib.sha256(canonical_payload(payload)).digest()
    kms = boto3.client("kms", region_name=settings.kms_region)
    response = kms.sign(
        KeyId=settings.kms_key_id,
        Message=digest,
        MessageType="DIGEST",
        SigningAlgorithm=settings.kms_signing_algorithm,
    )
    return base64.urlsafe_b64encode(response["Signature"]).decode("utf-8").rstrip("=")


def audit(db: Session, action: str, *, client_id: str | None = None, status_value: str = "success", metadata: Dict[str, Any] | None = None, actor: str = "system", resource_type: str = "", resource_id: str = "") -> None:
    db.add(models.AuditEvent(
        actor=actor,
        action=action,
        client_id=client_id,
        resource_type=resource_type,
        resource_id=resource_id,
        status=status_value,
        metadata_json=metadata or {},
    ))


def get_client_or_404(db: Session, client_key: str) -> models.Client:
    client = db.scalar(select(models.Client).where(models.Client.client_key == client_key))
    if not client:
        raise HTTPException(status_code=404, detail="Client was not found.")
    return client


def active_subscription(db: Session, client: models.Client) -> models.Subscription:
    now = utc_now()
    subscription = db.scalar(
        select(models.Subscription)
        .where(models.Subscription.client_id == client.id)
        .where(models.Subscription.status.in_(["active", "trialing"]))
        .where(models.Subscription.expires_at > now)
        .order_by(models.Subscription.expires_at.desc())
    )
    if not subscription:
        raise HTTPException(status_code=403, detail="Client subscription is not active or has expired.")
    return subscription


def validate_activation_token(db: Session, client: models.Client, token: str) -> models.ActivationToken:
    token_hash = hash_activation_token(token)
    activation = db.scalar(
        select(models.ActivationToken)
        .where(models.ActivationToken.client_id == client.id)
        .where(models.ActivationToken.token_hash == token_hash)
    )
    now = utc_now()
    if not activation or activation.status != "active":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Activation token is invalid.")
    if activation.expires_at and activation.expires_at <= now:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Activation token has expired.")
    if activation.max_uses and activation.used_count >= activation.max_uses:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Activation token use limit has been reached.")
    activation.used_count += 1
    activation.last_used_at = now
    return activation


def get_or_create_installation(db: Session, client: models.Client, request: Any) -> models.Installation:
    installation = db.scalar(
        select(models.Installation)
        .where(models.Installation.client_id == client.id)
        .where(models.Installation.installation_key == request.installation_id)
    )
    now = utc_now()
    if installation:
        installation.last_seen_at = now
        installation.aws_account_id = request.aws_account_id or installation.aws_account_id
        installation.region = request.region or installation.region
        installation.product_version = request.product_version or installation.product_version
        installation.metadata_json = request.platform or installation.metadata_json
        return installation

    installation = models.Installation(
        client_id=client.id,
        installation_key=request.installation_id,
        aws_account_id=request.aws_account_id or "",
        region=request.region or "",
        product_version=request.product_version or "",
        metadata_json=request.platform or {},
    )
    db.add(installation)
    db.flush()
    return installation


def assert_account_allowed(subscription: models.Subscription, aws_account_id: str | None) -> None:
    allowed = subscription.allowed_aws_account_ids or []
    if allowed and aws_account_id and aws_account_id not in allowed:
        raise HTTPException(status_code=403, detail="AWS account is not entitled by this license.")


def issue_license(db: Session, client: models.Client, subscription: models.Subscription, installation: models.Installation | None = None) -> models.License:
    issued_at = utc_now()
    license_key = f"hr-{client.client_key}-{uuid.uuid4().hex[:12]}"
    payload: Dict[str, Any] = {
        "client_id": client.client_key,
        "client_name": client.name,
        "installation_id": installation.installation_key if installation else "",
        "license_key": license_key,
        "license_type": subscription.license_type,
        "issued_at": iso(issued_at),
        "expires_at": iso(subscription.expires_at),
        "enabled_pipelines": subscription.enabled_pipelines,
        "enabled_features": subscription.enabled_features,
        "allowed_environments": subscription.allowed_environments,
        "allowed_aws_account_ids": subscription.allowed_aws_account_ids,
        "limits": subscription.limits,
        "license_mode": "online-sync",
        "issuer": get_settings().issuer,
    }
    signature = sign_license(payload)
    payload["signature"] = signature

    license_record = models.License(
        client_id=client.id,
        subscription_id=subscription.id,
        installation_id=installation.id if installation else None,
        license_key=license_key,
        license_payload=payload,
        signature=signature,
        issued_at=issued_at,
        expires_at=subscription.expires_at,
    )
    db.add(license_record)
    return license_record

