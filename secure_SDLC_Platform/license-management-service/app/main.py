from contextlib import asynccontextmanager
from typing import List

from fastapi import Depends, FastAPI, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import models
from .database import Base, engine, get_db
from .license_service import (
    active_subscription,
    assert_account_allowed,
    audit,
    generate_activation_token,
    get_client_or_404,
    get_or_create_installation,
    hash_activation_token,
    issue_license,
    utc_now,
    validate_activation_token,
)
from .schemas import (
    ActivationTokenCreate,
    ActivationTokenOut,
    AuditEventOut,
    ClientCreate,
    ClientOut,
    LicenseOut,
    LicenseSyncRequest,
    LicenseSyncResponse,
    PlanCreate,
    PlanOut,
    SubscriptionCreate,
    SubscriptionOut,
    SubscriptionPatch,
    UsageEventCreate,
    UsageEventOut,
)
from .security import require_admin
from .settings import get_settings


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    if settings.auto_create_tables:
        Base.metadata.create_all(bind=engine)
    yield


app = FastAPI(
    title="Horizon License Management Service",
    version="0.1.0",
    description="Horizon-owned license, entitlement, activation, and usage service for client-hosted deployments.",
    lifespan=lifespan,
)


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "service": "horizon-license-management-service"}


def _apply_plan_defaults(body: SubscriptionCreate, plan: models.Plan | None) -> dict:
    return {
        "license_type": body.license_type or (plan.license_type if plan else "trial"),
        "enabled_pipelines": body.enabled_pipelines or (plan.enabled_pipelines if plan else []),
        "enabled_features": body.enabled_features or (plan.enabled_features if plan else []),
        "allowed_environments": body.allowed_environments or (plan.allowed_environments if plan else []),
        "limits": body.limits or (plan.limits if plan else {}),
    }


@app.post("/api/v1/licenses/sync", response_model=LicenseSyncResponse)
def sync_license(request: LicenseSyncRequest, db: Session = Depends(get_db)) -> LicenseSyncResponse:
    client: models.Client | None = None
    try:
        client = get_client_or_404(db, request.client_id)
        if client.status != "active":
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Client is not active.")

        activation = validate_activation_token(db, client, request.activation_token)
        subscription = active_subscription(db, client)
        if activation.subscription_id and activation.subscription_id != subscription.id:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Activation token does not match the active subscription.")

        assert_account_allowed(subscription, request.aws_account_id)
        installation = get_or_create_installation(db, client, request)
        license_record = issue_license(db, client, subscription, installation)

        audit(
            db,
            "license.sync",
            client_id=client.id,
            resource_type="license",
            resource_id=license_record.license_key,
            metadata={
                "installation_id": installation.installation_key,
                "aws_account_id": request.aws_account_id,
                "region": request.region,
                "license_type": subscription.license_type,
            },
        )
        db.commit()
        db.refresh(license_record)
        return LicenseSyncResponse(
            status="synced",
            message=f"{subscription.license_type.title()} license synced. Expires at {license_record.license_payload['expires_at']}.",
            license=license_record.license_payload,
        )
    except HTTPException as exc:
        db.rollback()
        audit(
            db,
            "license.sync",
            client_id=client.id if client else None,
            status_value="failed",
            metadata={"reason": exc.detail, "requested_client_id": request.client_id, "aws_account_id": request.aws_account_id},
        )
        db.commit()
        raise


@app.post("/api/v1/usage/events", response_model=UsageEventOut, dependencies=[Depends(require_admin)])
def record_usage_event(body: UsageEventCreate, db: Session = Depends(get_db)) -> models.UsageEvent:
    client = get_client_or_404(db, body.client_id)
    event = models.UsageEvent(
        client_id=client.id,
        installation_id=body.installation_id,
        event_type=body.event_type,
        quantity=body.quantity,
        metadata_json=body.metadata,
    )
    db.add(event)
    audit(db, "usage.record", client_id=client.id, resource_type="usage_event", resource_id=event.event_type, metadata=body.metadata)
    db.commit()
    db.refresh(event)
    return event


@app.post("/api/v1/admin/plans", response_model=PlanOut, dependencies=[Depends(require_admin)])
def create_plan(body: PlanCreate, db: Session = Depends(get_db)) -> models.Plan:
    existing = db.scalar(select(models.Plan).where(models.Plan.code == body.code))
    if existing:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Plan code already exists.")
    plan = models.Plan(**body.model_dump())
    db.add(plan)
    audit(db, "plan.create", resource_type="plan", resource_id=body.code, actor="admin")
    db.commit()
    db.refresh(plan)
    return plan


@app.get("/api/v1/admin/plans", response_model=List[PlanOut], dependencies=[Depends(require_admin)])
def list_plans(db: Session = Depends(get_db)) -> list[models.Plan]:
    return list(db.scalars(select(models.Plan).order_by(models.Plan.code)))


@app.post("/api/v1/admin/clients", response_model=ClientOut, dependencies=[Depends(require_admin)])
def create_client(body: ClientCreate, db: Session = Depends(get_db)) -> models.Client:
    existing = db.scalar(select(models.Client).where(models.Client.client_key == body.client_key))
    if existing:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Client key already exists.")
    client = models.Client(**body.model_dump())
    db.add(client)
    db.flush()
    audit(db, "client.create", client_id=client.id, resource_type="client", resource_id=client.client_key, actor="admin")
    db.commit()
    db.refresh(client)
    return client


@app.get("/api/v1/admin/clients", response_model=List[ClientOut], dependencies=[Depends(require_admin)])
def list_clients(db: Session = Depends(get_db)) -> list[models.Client]:
    return list(db.scalars(select(models.Client).order_by(models.Client.client_key)))


@app.get("/api/v1/admin/clients/{client_key}", response_model=ClientOut, dependencies=[Depends(require_admin)])
def get_client(client_key: str, db: Session = Depends(get_db)) -> models.Client:
    return get_client_or_404(db, client_key)


@app.post("/api/v1/admin/clients/{client_key}/subscriptions", response_model=SubscriptionOut, dependencies=[Depends(require_admin)])
def create_subscription(client_key: str, body: SubscriptionCreate, db: Session = Depends(get_db)) -> models.Subscription:
    client = get_client_or_404(db, client_key)
    plan = None
    if body.plan_code:
        plan = db.scalar(select(models.Plan).where(models.Plan.code == body.plan_code))
        if not plan:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Plan was not found.")
    defaults = _apply_plan_defaults(body, plan)
    subscription = models.Subscription(
        client_id=client.id,
        plan_id=plan.id if plan else None,
        status=body.status,
        license_type=defaults["license_type"],
        starts_at=body.starts_at or utc_now(),
        expires_at=body.expires_at,
        enabled_pipelines=defaults["enabled_pipelines"],
        enabled_features=defaults["enabled_features"],
        allowed_environments=defaults["allowed_environments"],
        allowed_aws_account_ids=body.allowed_aws_account_ids,
        limits=defaults["limits"],
        notes=body.notes,
    )
    db.add(subscription)
    db.flush()
    audit(db, "subscription.create", client_id=client.id, resource_type="subscription", resource_id=subscription.id, actor="admin")
    db.commit()
    db.refresh(subscription)
    return subscription


@app.patch("/api/v1/admin/subscriptions/{subscription_id}", response_model=SubscriptionOut, dependencies=[Depends(require_admin)])
def patch_subscription(subscription_id: str, body: SubscriptionPatch, db: Session = Depends(get_db)) -> models.Subscription:
    subscription = db.get(models.Subscription, subscription_id)
    if not subscription:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Subscription was not found.")
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(subscription, field, value)
    audit(db, "subscription.update", client_id=subscription.client_id, resource_type="subscription", resource_id=subscription.id, actor="admin")
    db.commit()
    db.refresh(subscription)
    return subscription


@app.post("/api/v1/admin/clients/{client_key}/activation-tokens", response_model=ActivationTokenOut, dependencies=[Depends(require_admin)])
def create_activation_token(client_key: str, body: ActivationTokenCreate, db: Session = Depends(get_db)) -> ActivationTokenOut:
    client = get_client_or_404(db, client_key)
    if body.subscription_id:
        subscription = db.get(models.Subscription, body.subscription_id)
        if not subscription or subscription.client_id != client.id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Subscription was not found for this client.")
    raw_token = generate_activation_token()
    activation = models.ActivationToken(
        client_id=client.id,
        subscription_id=body.subscription_id,
        name=body.name,
        token_hash=hash_activation_token(raw_token),
        expires_at=body.expires_at,
        max_uses=body.max_uses,
    )
    db.add(activation)
    db.flush()
    audit(db, "activation_token.create", client_id=client.id, resource_type="activation_token", resource_id=activation.id, actor="admin")
    db.commit()
    db.refresh(activation)
    response = ActivationTokenOut.model_validate(activation)
    response.activation_token = raw_token
    return response


@app.get("/api/v1/admin/clients/{client_key}/subscriptions", response_model=List[SubscriptionOut], dependencies=[Depends(require_admin)])
def list_client_subscriptions(client_key: str, db: Session = Depends(get_db)) -> list[models.Subscription]:
    client = get_client_or_404(db, client_key)
    return list(db.scalars(select(models.Subscription).where(models.Subscription.client_id == client.id).order_by(models.Subscription.created_at.desc())))


@app.get("/api/v1/admin/clients/{client_key}/licenses", response_model=List[LicenseOut], dependencies=[Depends(require_admin)])
def list_client_licenses(client_key: str, db: Session = Depends(get_db)) -> list[models.License]:
    client = get_client_or_404(db, client_key)
    return list(db.scalars(select(models.License).where(models.License.client_id == client.id).order_by(models.License.issued_at.desc())))


@app.get("/api/v1/admin/usage/events", response_model=List[UsageEventOut], dependencies=[Depends(require_admin)])
def list_usage_events(
    client_key: str | None = None,
    event_type: str | None = None,
    limit: int = Query(default=100, ge=1, le=1000),
    db: Session = Depends(get_db),
) -> list[models.UsageEvent]:
    statement = select(models.UsageEvent).order_by(models.UsageEvent.occurred_at.desc()).limit(limit)
    if client_key:
        client = get_client_or_404(db, client_key)
        statement = statement.where(models.UsageEvent.client_id == client.id)
    if event_type:
        statement = statement.where(models.UsageEvent.event_type == event_type)
    return list(db.scalars(statement))


@app.get("/api/v1/admin/audit-events", response_model=List[AuditEventOut], dependencies=[Depends(require_admin)])
def list_audit_events(
    client_key: str | None = None,
    action: str | None = None,
    limit: int = Query(default=100, ge=1, le=1000),
    db: Session = Depends(get_db),
) -> list[models.AuditEvent]:
    statement = select(models.AuditEvent).order_by(models.AuditEvent.occurred_at.desc()).limit(limit)
    if client_key:
        client = get_client_or_404(db, client_key)
        statement = statement.where(models.AuditEvent.client_id == client.id)
    if action:
        statement = statement.where(models.AuditEvent.action == action)
    return list(db.scalars(statement))
