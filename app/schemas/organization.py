from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import OrganizationStatus, PlanTier, UpgradeStatus


class OrganizationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    business_type: str | None
    gst_number: str | None
    pan_number: str | None
    phone: str | None
    email: str | None
    address: str | None
    financial_year: str | None
    logo_url: str | None
    plan: PlanTier
    status: OrganizationStatus
    # Trial & upgrade lifecycle
    trial_ends_at: datetime | None = None
    trial_days_left: int | None = None
    requested_plan: PlanTier | None = None
    upgrade_status: UpgradeStatus | None = None
    upgrade_requested_at: datetime | None = None
    upgrade_reject_reason: str | None = None
    created_at: datetime


class UpgradeRequest(BaseModel):
    requested_plan: PlanTier


class RejectUpgrade(BaseModel):
    reason: str | None = Field(default=None, max_length=500)


class OrgStatusUpdate(BaseModel):
    status: OrganizationStatus
