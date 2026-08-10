from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class PlanOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    price_monthly: float
    price_yearly: float
    original_price_monthly: float | None
    original_price_yearly: float | None
    max_users: int | None
    max_orders: int | None
    max_storage_gb: float | None
    features: list[str]
    is_active: bool
    is_default: bool
    created_at: datetime
    updated_at: datetime


class PlanCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    price_monthly: float = Field(ge=0)
    price_yearly: float = Field(ge=0)
    original_price_monthly: float | None = Field(default=None, ge=0)
    original_price_yearly: float | None = Field(default=None, ge=0)
    max_users: int | None = Field(default=None, ge=0)
    max_orders: int | None = Field(default=None, ge=0)
    max_storage_gb: float | None = Field(default=None, ge=0, description="Upload quota; null = unlimited")
    features: list[str] = Field(default_factory=list)
    is_default: bool = False


class PlanStatusUpdate(BaseModel):
    is_active: bool


class PlanUpdate(BaseModel):
    """Partial update — only provided fields are changed."""

    name: str | None = Field(default=None, min_length=1, max_length=100)
    price_monthly: float | None = Field(default=None, ge=0)
    price_yearly: float | None = Field(default=None, ge=0)
    original_price_monthly: float | None = Field(default=None, ge=0)
    original_price_yearly: float | None = Field(default=None, ge=0)
    max_users: int | None = Field(default=None, ge=0)
    max_orders: int | None = Field(default=None, ge=0)
    max_storage_gb: float | None = Field(default=None, ge=0)
    features: list[str] | None = None
    is_active: bool | None = None
    is_default: bool | None = None
