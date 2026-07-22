from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.models.enums import OrganizationStatus, PlanTier


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
    created_at: datetime
