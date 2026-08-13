from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class VehicleOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    organization_id: str
    vehicle_number: str
    vehicle_type: str | None = None
    capacity_kg: float | None = None
    default_driver_id: str | None = None
    is_active: bool
    created_at: datetime
    updated_at: datetime


class VehicleCreate(BaseModel):
    vehicle_number: str = Field(min_length=1, max_length=30, examples=["DL 8S AB 2481"])
    vehicle_type: str | None = Field(default=None, max_length=50, examples=["Tempo", "Truck"])
    capacity_kg: float | None = Field(default=None, ge=0)
    default_driver_id: str | None = Field(
        default=None, description="An employee in the firm. A delivery can still name anybody.")
    is_active: bool = True


class VehicleUpdate(BaseModel):
    """Partial update — only the fields you send change."""

    vehicle_number: str | None = Field(default=None, min_length=1, max_length=30)
    vehicle_type: str | None = Field(default=None, max_length=50)
    capacity_kg: float | None = Field(default=None, ge=0)
    default_driver_id: str | None = None
    is_active: bool | None = None
