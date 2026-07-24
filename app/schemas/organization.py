from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import BillingCycle, OrganizationStatus, UpgradeStatus
from app.schemas.plan import PlanOut


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
    status: OrganizationStatus

    # Subscription: plan_id + the joined plan detail (and requested plan, if pending).
    plan_id: str | None = None
    plan: PlanOut | None = None
    requested_plan_id: str | None = None
    requested_plan: PlanOut | None = None
    billing_cycle: BillingCycle | None = None

    # Trial & upgrade lifecycle
    trial_ends_at: datetime | None = None
    trial_days_left: int | None = None
    upgrade_status: UpgradeStatus | None = None
    upgrade_requested_at: datetime | None = None
    upgrade_reject_reason: str | None = None
    created_at: datetime


class UpgradeRequest(BaseModel):
    requested_plan_id: str
    billing_cycle: BillingCycle = BillingCycle.MONTHLY


class RejectUpgrade(BaseModel):
    reason: str | None = Field(default=None, max_length=500)


class OrgStatusUpdate(BaseModel):
    status: OrganizationStatus
