from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.models.enums import OrganizationStatus, PlanTier


class OrganizationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    gst_number: str | None
    pan_number: str | None
    phone: str | None
    email: str | None
    plan: PlanTier
    status: OrganizationStatus
    created_at: datetime
