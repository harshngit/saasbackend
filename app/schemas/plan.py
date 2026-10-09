from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.entitlements import ALL_ENTITLEMENT_KEYS


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
    max_warehouses: int | None = None
    max_storage_gb: float | None
    features: list[str]
    entitlements: dict[str, bool] = Field(default_factory=dict)
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
    max_warehouses: int | None = Field(default=None, ge=0)
    max_storage_gb: float | None = Field(default=None, ge=0, description="Upload quota; null = unlimited")
    features: list[str] = Field(default_factory=list)
    entitlements: dict[str, bool] = Field(default_factory=dict)
    is_default: bool = False

    @field_validator("entitlements")
    @classmethod
    def validate_entitlements(cls, v: dict[str, bool]) -> dict[str, bool]:
        if not v:
            return v
        unknown_keys = set(v.keys()) - ALL_ENTITLEMENT_KEYS
        if unknown_keys:
            raise ValueError(f"Unknown entitlement key(s): {', '.join(sorted(unknown_keys))}")
        return v


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
    max_warehouses: int | None = Field(default=None, ge=0)
    max_storage_gb: float | None = Field(default=None, ge=0)
    features: list[str] | None = None
    entitlements: dict[str, bool] | None = None
    is_active: bool | None = None
    is_default: bool | None = None

    @field_validator("entitlements")
    @classmethod
    def validate_entitlements(cls, v: dict[str, bool] | None) -> dict[str, bool] | None:
        if v is None:
            return v
        unknown_keys = set(v.keys()) - ALL_ENTITLEMENT_KEYS
        if unknown_keys:
            raise ValueError(f"Unknown entitlement key(s): {', '.join(sorted(unknown_keys))}")
        return v

