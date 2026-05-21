from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class PlanCreate(BaseModel):
    code: str
    name: str
    license_type: str = "trial"
    description: str = ""
    enabled_pipelines: List[str] = Field(default_factory=list)
    enabled_features: List[str] = Field(default_factory=list)
    allowed_environments: List[str] = Field(default_factory=list)
    limits: Dict[str, Any] = Field(default_factory=dict)


class PlanOut(PlanCreate):
    id: str
    created_at: datetime

    model_config = {"from_attributes": True}


class ClientCreate(BaseModel):
    client_key: str = Field(..., examples=["regeneron-healthcare"])
    name: str
    industry: str = ""
    status: str = "active"


class ClientOut(ClientCreate):
    id: str
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class SubscriptionCreate(BaseModel):
    plan_code: Optional[str] = None
    status: str = "active"
    license_type: Optional[str] = None
    starts_at: Optional[datetime] = None
    expires_at: datetime
    enabled_pipelines: List[str] = Field(default_factory=list)
    enabled_features: List[str] = Field(default_factory=list)
    allowed_environments: List[str] = Field(default_factory=list)
    allowed_aws_account_ids: List[str] = Field(default_factory=list)
    limits: Dict[str, Any] = Field(default_factory=dict)
    notes: str = ""


class SubscriptionPatch(BaseModel):
    status: Optional[str] = None
    license_type: Optional[str] = None
    expires_at: Optional[datetime] = None
    enabled_pipelines: Optional[List[str]] = None
    enabled_features: Optional[List[str]] = None
    allowed_environments: Optional[List[str]] = None
    allowed_aws_account_ids: Optional[List[str]] = None
    limits: Optional[Dict[str, Any]] = None
    notes: Optional[str] = None


class SubscriptionOut(BaseModel):
    id: str
    client_id: str
    plan_id: Optional[str] = None
    status: str
    license_type: str
    starts_at: datetime
    expires_at: datetime
    enabled_pipelines: List[str]
    enabled_features: List[str]
    allowed_environments: List[str]
    allowed_aws_account_ids: List[str]
    limits: Dict[str, Any]
    notes: str
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class ActivationTokenCreate(BaseModel):
    name: str = "trial activation"
    subscription_id: Optional[str] = None
    expires_at: Optional[datetime] = None
    max_uses: int = 0


class ActivationTokenOut(BaseModel):
    id: str
    client_id: str
    subscription_id: Optional[str] = None
    name: str
    status: str
    expires_at: Optional[datetime] = None
    max_uses: int
    used_count: int
    created_at: datetime
    last_used_at: Optional[datetime] = None
    activation_token: Optional[str] = None

    model_config = {"from_attributes": True}


class LicenseSyncRequest(BaseModel):
    client_id: str
    client_name: Optional[str] = None
    installation_id: str
    activation_token: str
    aws_account_id: Optional[str] = None
    region: Optional[str] = None
    product_version: Optional[str] = None
    current_license_key: Optional[str] = None
    current_expires_at: Optional[str] = None
    force: bool = False
    platform: Dict[str, Any] = Field(default_factory=dict)


class LicenseSyncResponse(BaseModel):
    status: str
    message: str
    license: Dict[str, Any]


class LicenseOut(BaseModel):
    id: str
    client_id: str
    subscription_id: str
    installation_id: Optional[str] = None
    license_key: str
    license_payload: Dict[str, Any]
    signature: str
    status: str
    issued_at: datetime
    expires_at: datetime
    revoked_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


class UsageEventCreate(BaseModel):
    client_id: str
    installation_id: Optional[str] = None
    event_type: str
    quantity: int = 1
    metadata: Dict[str, Any] = Field(default_factory=dict)


class UsageEventOut(BaseModel):
    id: str
    client_id: str
    installation_id: Optional[str] = None
    event_type: str
    quantity: int
    metadata: Dict[str, Any] = Field(alias="metadata_json")
    occurred_at: datetime

    model_config = {"from_attributes": True, "populate_by_name": True}


class AuditEventOut(BaseModel):
    id: str
    actor: str
    action: str
    client_id: Optional[str] = None
    resource_type: str
    resource_id: str
    status: str
    metadata: Dict[str, Any] = Field(alias="metadata_json")
    occurred_at: datetime

    model_config = {"from_attributes": True, "populate_by_name": True}
