from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

VEHICLE_STATUSES = {"active", "inactive", "maintenance"}


class DeliveryPartnerBrief(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    email: str | None = None
    phone: str | None = None


class VehicleOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    organization_id: str
    vehicle_number: str
    vehicle_type: str | None = None
    capacity_kg: float | None = None
    default_driver_id: str | None = None
    assigned_delivery_partner: DeliveryPartnerBrief | None = None
    status: str = "active"
    is_active: bool = True
    created_at: datetime
    updated_at: datetime


class VehicleCreate(BaseModel):
    vehicle_number: str = Field(min_length=1, max_length=30, examples=["DL 8S AB 2481"])
    vehicle_type: str | None = Field(default=None, max_length=50, examples=["Tempo", "Truck"])
    capacity_kg: float | None = Field(default=None, ge=0)
    default_driver_id: str | None = Field(
        default=None, description="An employee in the firm. A delivery can still name anybody."
    )
    status: str | None = Field(default="active", description="active | inactive | maintenance")
    is_active: bool = True

    @field_validator("status")
    @classmethod
    def _validate_status(cls, v: str | None) -> str | None:
        if v is not None and v not in VEHICLE_STATUSES:
            raise ValueError(f"status must be one of {sorted(VEHICLE_STATUSES)}")
        return v


class VehicleUpdate(BaseModel):
    """Partial update — only the fields you send change."""

    vehicle_number: str | None = Field(default=None, min_length=1, max_length=30)
    vehicle_type: str | None = Field(default=None, max_length=50)
    capacity_kg: float | None = Field(default=None, ge=0)
    default_driver_id: str | None = None
    status: str | None = Field(default=None, description="active | inactive | maintenance")
    is_active: bool | None = None

    @field_validator("status")
    @classmethod
    def _validate_status(cls, v: str | None) -> str | None:
        if v is not None and v not in VEHICLE_STATUSES:
            raise ValueError(f"status must be one of {sorted(VEHICLE_STATUSES)}")
        return v


class VehicleAssignmentHistoryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    vehicle_id: str
    delivery_partner_id: str | None = None
    assigned_delivery_partner: DeliveryPartnerBrief | None = None
    assigned_by_id: str | None = None
    assigned_at: datetime
    unassigned_at: datetime | None = None
    notes: str | None = None


class VehicleActivityOut(BaseModel):
    type: str
    timestamp: datetime
    description: str
    reference_type: str | None = None
    reference_id: str | None = None
