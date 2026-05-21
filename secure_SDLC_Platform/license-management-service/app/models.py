import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from .database import Base


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def uuid_str() -> str:
    return str(uuid.uuid4())


class Client(Base):
    __tablename__ = "clients"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    client_key: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(200))
    industry: Mapped[str] = mapped_column(String(120), default="")
    status: Mapped[str] = mapped_column(String(40), default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)

    subscriptions: Mapped[list["Subscription"]] = relationship(back_populates="client")
    installations: Mapped[list["Installation"]] = relationship(back_populates="client")
    activation_tokens: Mapped[list["ActivationToken"]] = relationship(back_populates="client")


class Plan(Base):
    __tablename__ = "plans"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    code: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(160))
    license_type: Mapped[str] = mapped_column(String(60), default="trial")
    description: Mapped[str] = mapped_column(Text, default="")
    enabled_pipelines: Mapped[list[str]] = mapped_column(JSON, default=list)
    enabled_features: Mapped[list[str]] = mapped_column(JSON, default=list)
    allowed_environments: Mapped[list[str]] = mapped_column(JSON, default=list)
    limits: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class Subscription(Base):
    __tablename__ = "subscriptions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    client_id: Mapped[str] = mapped_column(ForeignKey("clients.id"), index=True)
    plan_id: Mapped[str | None] = mapped_column(ForeignKey("plans.id"), nullable=True)
    status: Mapped[str] = mapped_column(String(40), default="active")
    license_type: Mapped[str] = mapped_column(String(60), default="trial")
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    enabled_pipelines: Mapped[list[str]] = mapped_column(JSON, default=list)
    enabled_features: Mapped[list[str]] = mapped_column(JSON, default=list)
    allowed_environments: Mapped[list[str]] = mapped_column(JSON, default=list)
    allowed_aws_account_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    limits: Mapped[dict] = mapped_column(JSON, default=dict)
    notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)

    client: Mapped["Client"] = relationship(back_populates="subscriptions")
    plan: Mapped["Plan | None"] = relationship()


class Installation(Base):
    __tablename__ = "installations"
    __table_args__ = (UniqueConstraint("client_id", "installation_key", name="uq_installation_client_key"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    client_id: Mapped[str] = mapped_column(ForeignKey("clients.id"), index=True)
    installation_key: Mapped[str] = mapped_column(String(160), index=True)
    aws_account_id: Mapped[str] = mapped_column(String(32), default="")
    region: Mapped[str] = mapped_column(String(40), default="")
    product_version: Mapped[str] = mapped_column(String(80), default="")
    status: Mapped[str] = mapped_column(String(40), default="active")
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    metadata_json: Mapped[dict] = mapped_column("metadata", JSON, default=dict)

    client: Mapped["Client"] = relationship(back_populates="installations")


class ActivationToken(Base):
    __tablename__ = "activation_tokens"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    client_id: Mapped[str] = mapped_column(ForeignKey("clients.id"), index=True)
    subscription_id: Mapped[str | None] = mapped_column(ForeignKey("subscriptions.id"), nullable=True)
    name: Mapped[str] = mapped_column(String(160), default="trial activation")
    token_hash: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    status: Mapped[str] = mapped_column(String(40), default="active")
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    max_uses: Mapped[int] = mapped_column(Integer, default=0)
    used_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    client: Mapped["Client"] = relationship(back_populates="activation_tokens")
    subscription: Mapped["Subscription | None"] = relationship()


class License(Base):
    __tablename__ = "licenses"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    client_id: Mapped[str] = mapped_column(ForeignKey("clients.id"), index=True)
    subscription_id: Mapped[str] = mapped_column(ForeignKey("subscriptions.id"), index=True)
    installation_id: Mapped[str | None] = mapped_column(ForeignKey("installations.id"), nullable=True)
    license_key: Mapped[str] = mapped_column(String(160), unique=True, index=True)
    license_payload: Mapped[dict] = mapped_column(JSON, default=dict)
    signature: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(40), default="active")
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class UsageEvent(Base):
    __tablename__ = "usage_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    client_id: Mapped[str] = mapped_column(ForeignKey("clients.id"), index=True)
    installation_id: Mapped[str | None] = mapped_column(ForeignKey("installations.id"), nullable=True)
    event_type: Mapped[str] = mapped_column(String(80), index=True)
    quantity: Mapped[int] = mapped_column(Integer, default=1)
    metadata_json: Mapped[dict] = mapped_column("metadata", JSON, default=dict)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    actor: Mapped[str] = mapped_column(String(160), default="system")
    action: Mapped[str] = mapped_column(String(120), index=True)
    client_id: Mapped[str | None] = mapped_column(ForeignKey("clients.id"), nullable=True)
    resource_type: Mapped[str] = mapped_column(String(80), default="")
    resource_id: Mapped[str] = mapped_column(String(160), default="")
    status: Mapped[str] = mapped_column(String(40), default="success")
    metadata_json: Mapped[dict] = mapped_column("metadata", JSON, default=dict)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
