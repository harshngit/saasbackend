from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.enums import BillingCycle, OrganizationStatus, UpgradeStatus
from app.schemas.plan import PlanOut


class OrganizationInventoryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    created_at: datetime
    plan_id: str | None = None
    status: OrganizationStatus | str
    user_count: int = 0
    customer_count: int = 0
    order_count: int = 0
    invoice_count: int = 0
    last_activity_date: datetime | None = None
    created_by_seed: bool = False


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

    # Paid-plan period (Razorpay Phase 1 — see app.models.organization.Organization.
    # days_left). None for an org that has never had a paid plan activated online;
    # distinct from trial_days_left, whose trial is already over once a paid plan exists.
    plan_expires_at: datetime | None = None
    days_left: int | None = None

    @field_validator("logo_url", mode="after")
    @classmethod
    def _normalize_logo_url(cls, v: str | None) -> str | None:
        from app.core.files import normalize_file_url
        return normalize_file_url(v)


class UpgradeRequest(BaseModel):
    requested_plan_id: str
    billing_cycle: BillingCycle = BillingCycle.MONTHLY


class RejectUpgrade(BaseModel):
    reason: str | None = Field(default=None, max_length=500)


class OrgStatusUpdate(BaseModel):
    status: OrganizationStatus


class OrganizationStorageUsageOut(BaseModel):
    organization_id: str
    files_documents_bytes: int = Field(..., description="Bytes used by uploaded files and documents")
    stored_reports_bytes: int = Field(default=0, description="Bytes used by persisted reports (0 for ephemeral)")
    application_data_bytes: int = Field(..., description="Approximate logical bytes used by tenant application records")
    total_used_bytes: int = Field(..., description="Total accounted bytes")
    effective_limit_bytes: int | None = Field(default=None, description="Plan or override storage limit in bytes; null if unlimited")
    effective_limit_gb: float | None = Field(default=None, description="Plan or override storage limit in GB; null if unlimited")
    remaining_bytes: int | None = Field(default=None, description="Available bytes remaining; null if unlimited")
    percentage_used: float | None = Field(default=None, description="Percentage of quota used; null if unlimited")
    is_unlimited: bool = Field(..., description="True if organization has no numeric storage cap")
    is_over_limit: bool = Field(..., description="True if total_used_bytes exceeds effective_limit_bytes")
    limit_source: str | None = Field(default=None, description="'plan', 'organization_override', or 'fallback'")
    calculated_at: datetime = Field(..., description="Timestamp when usage was accounted")
