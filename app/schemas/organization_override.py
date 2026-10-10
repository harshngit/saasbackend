from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.entitlements import ALL_ENTITLEMENT_KEYS, ALL_LIMIT_KEYS, normalize_entitlement_key


class FeatureOverrideUpsert(BaseModel):
    entitlement_key: str = Field(..., description="Canonical entitlement key to override")
    effect: str = Field(default="ALLOW", description="'ALLOW' or 'BLOCK'")
    expires_at: Optional[datetime] = Field(default=None, description="Optional expiry timestamp (UTC)")
    reason: Optional[str] = Field(default=None, max_length=500, description="Audit reason for override")

    @field_validator("entitlement_key")
    @classmethod
    def validate_key(cls, v: str) -> str:
        norm_v = normalize_entitlement_key(v)
        if norm_v not in ALL_ENTITLEMENT_KEYS:
            raise ValueError(f"Unknown entitlement key: '{v}'. Must be one of canonical entitlement keys.")
        return norm_v


    @field_validator("effect")
    @classmethod
    def validate_effect(cls, v: str) -> str:
        upper_v = v.upper()
        if upper_v not in ("ALLOW", "BLOCK"):
            raise ValueError("Effect must be 'ALLOW' or 'BLOCK'")
        return upper_v


class FeatureOverrideOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    organization_id: str
    entitlement_key: str
    effect: str
    is_allowed: bool
    expires_at: Optional[datetime]
    is_active: bool
    reason: Optional[str]
    created_by_user_id: Optional[str]
    created_at: datetime
    updated_at: datetime


class LimitOverrideUpsert(BaseModel):
    limit_key: str = Field(..., description="Canonical limit key to override")
    value: Optional[int] = Field(default=None, ge=0, description="Numeric limit value; null = unlimited")
    expires_at: Optional[datetime] = Field(default=None, description="Optional expiry timestamp (UTC)")
    reason: Optional[str] = Field(default=None, max_length=500, description="Audit reason for override")

    @field_validator("limit_key")
    @classmethod
    def validate_key(cls, v: str) -> str:
        if v not in ALL_LIMIT_KEYS:
            raise ValueError(f"Unknown limit key: '{v}'. Must be one of canonical limit keys.")
        return v


class LimitOverrideOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    organization_id: str
    limit_key: str
    value: Optional[int]
    expires_at: Optional[datetime]
    is_active: bool
    reason: Optional[str]
    created_by_user_id: Optional[str]
    created_at: datetime
    updated_at: datetime


class OrganizationEntitlementsOut(BaseModel):
    organization_id: str
    organization_name: str
    subscription_status: str
    plan: Optional[Dict[str, Any]] = None
    trial_ends_at: Optional[datetime] = None
    trial_days_left: Optional[int] = None
    plan_expires_at: Optional[datetime] = None
    days_left: Optional[int] = None
    upgrade_status: Optional[str] = None
    features: Dict[str, bool]
    limits: Dict[str, Optional[int]]
    active_feature_overrides_count: int = 0
    active_limit_overrides_count: int = 0


class OrganizationAdminEntitlementsOut(OrganizationEntitlementsOut):
    feature_overrides: List[FeatureOverrideOut] = Field(default_factory=list)
    limit_overrides: List[LimitOverrideOut] = Field(default_factory=list)
