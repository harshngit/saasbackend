"""Platform staff (Super Admin + Sales Manager) and their organization requests."""
from datetime import datetime
from typing import Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.core.entitlements import ALL_ENTITLEMENT_KEYS, normalize_entitlement_key
from app.models.enums import BillingCycle

PlatformStaffRole = Literal["sales_manager"]


# ------------------------------------------------------------------ staff


class PlatformStaffCreate(BaseModel):
    name: str = Field(min_length=1, max_length=150)
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    phone: str | None = None
    role: PlatformStaffRole = "sales_manager"


class PlatformStaffUpdate(BaseModel):
    """Partial update: only the fields sent change."""

    name: str | None = Field(default=None, min_length=1, max_length=150)
    email: EmailStr | None = None
    phone: str | None = None
    password: str | None = Field(default=None, min_length=8, max_length=128)
    role: PlatformStaffRole | None = None
    is_active: bool | None = None


class PlatformStaffOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    email: str
    phone: str | None = None
    role: str = Field(validation_alias=AliasChoices("system_role", "role"))
    is_active: bool
    last_login: datetime | None = None
    created_at: datetime


# ------------------------------------------------------------------ shared briefs


class PlanBrief(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str


class UserBrief(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    email: str


class OrganizationBrief(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    company_code: str | None = None


# ------------------------------------------------------------------ upgrade requests


class UpgradeRequestCreate(BaseModel):
    requested_plan_id: str
    billing_cycle: BillingCycle | None = None
    reason: str = Field(min_length=1, max_length=500)
    notes: str | None = Field(default=None, max_length=2000)


class UpgradeRequestOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    organization: OrganizationBrief | None
    current_plan: PlanBrief | None
    requested_plan: PlanBrief | None
    billing_cycle: str | None
    reason: str
    notes: str | None
    status: str
    requested_by: UserBrief | None
    decided_by: UserBrief | None
    decided_at: datetime | None
    decision_note: str | None
    created_at: datetime


# ------------------------------------------------------------------ feature requests


class FeatureRequestCreate(BaseModel):
    entitlement_key: str
    action: Literal["unlock", "restrict"]
    reason: str = Field(min_length=1, max_length=500)

    @field_validator("entitlement_key")
    @classmethod
    def _known_key(cls, v: str) -> str:
        key = normalize_entitlement_key(v)
        if key not in ALL_ENTITLEMENT_KEYS:
            raise ValueError(f"Unknown feature: '{v}'")
        return key


class FeatureRequestOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    organization: OrganizationBrief | None
    entitlement_key: str
    feature_name: str
    action: str
    reason: str
    status: str
    requested_by: UserBrief | None
    decided_by: UserBrief | None
    decided_at: datetime | None
    decision_note: str | None
    created_at: datetime


# ------------------------------------------------------------------ misc


class RequestDecision(BaseModel):
    """Optional note on approve; the rejection reason on reject."""

    note: str | None = Field(default=None, max_length=500)


class FeatureCatalogItem(BaseModel):
    key: str
    name: str
    category: str
    category_name: str
    description: str


class PlatformSummary(BaseModel):
    organizations: int
    pending_upgrade_requests: int
    pending_feature_requests: int
