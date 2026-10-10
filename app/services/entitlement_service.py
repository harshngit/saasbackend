"""Centralized Entitlement and Limit Resolution Service.

Resolution order for features:
1. Active, unexpired organization feature override (ALLOW/BLOCK).
2. Plan default (from plan.entitlements or canonical default if empty).
3. Safe fallback (DEFAULT_FALLBACK_ENTITLEMENTS).

Resolution order for limits:
1. Active, unexpired organization limit override.
2. Plan default (from plan.max_* or canonical default).
3. Safe fallback (DEFAULT_FALLBACK_LIMITS).
"""

from datetime import datetime, timezone
from typing import Any, Dict, Optional, Union

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.core.entitlements import (
    ALL_ENTITLEMENT_KEYS,
    ALL_LIMIT_KEYS,
    CANONICAL_ENTITLEMENTS,
    CANONICAL_LIMITS,
    DEFAULT_FALLBACK_ENTITLEMENTS,
    DEFAULT_FALLBACK_LIMITS,
    default_entitlements_for_plan,
    default_limits_for_plan,
    normalize_entitlement_key,
)
from app.models.organization import Organization
from app.models.organization_override import (
    OrganizationFeatureOverride,
    OrganizationLimitOverride,
)
from app.models.plan import Plan


def _is_override_active(expires_at: Optional[datetime]) -> bool:
    if expires_at is None:
        return True
    exp = expires_at
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    return exp > datetime.now(timezone.utc)


def get_effective_feature_overrides(
    db: Session, organization_id: str
) -> Dict[str, OrganizationFeatureOverride]:
    """Retrieve all active, unexpired feature overrides for an organization."""
    overrides = (
        db.query(OrganizationFeatureOverride)
        .filter(OrganizationFeatureOverride.organization_id == organization_id)
        .all()
    )
    return {normalize_entitlement_key(ov.entitlement_key): ov for ov in overrides if _is_override_active(ov.expires_at)}


def get_effective_limit_overrides(
    db: Session, organization_id: str
) -> Dict[str, OrganizationLimitOverride]:
    """Retrieve all active, unexpired limit overrides for an organization."""
    overrides = (
        db.query(OrganizationLimitOverride)
        .filter(OrganizationLimitOverride.organization_id == organization_id)
        .all()
    )
    return {ov.limit_key: ov for ov in overrides if _is_override_active(ov.expires_at)}


def has_entitlement(
    db: Session,
    organization: Union[Organization, str, None],
    entitlement_key: str,
) -> bool:
    """Evaluate whether an organization is entitled to a specific feature key.

    Resolution:
    1. Active unexpired org override.
    2. Plan default.
    3. Safe fallback.
    """
    key = normalize_entitlement_key(entitlement_key)
    if not organization or not key:
        return False

    org_id: str
    plan: Optional[Plan] = None
    if isinstance(organization, str):
        org = db.query(Organization).filter(Organization.id == organization).first()
        if not org:
            return False
        org_id = org.id
        plan = org.plan
    else:
        org_id = organization.id
        plan = organization.plan

    # If org.plan was not joined/loaded, fetch it
    if plan is None and getattr(organization, "plan_id", None):
        plan = db.query(Plan).filter(Plan.id == organization.plan_id).first()

    # 1. Organization Override (check canonical key and legacy alias if applicable)
    override = (
        db.query(OrganizationFeatureOverride)
        .filter(
            OrganizationFeatureOverride.organization_id == org_id,
            OrganizationFeatureOverride.entitlement_key.in_([key, "report.profit_and_loss"] if key == "report.profit_loss" else [key]),
        )
        .first()
    )
    if override and _is_override_active(override.expires_at):
        if override.effect == "ALLOW" or override.is_allowed is True:
            return True
        if override.effect == "BLOCK" or override.is_allowed is False:
            return False

    # 2. Plan Default
    if plan:
        entitlements = plan.entitlements or {}
        if key in entitlements:
            return bool(entitlements[key])
        if key == "report.profit_loss" and "report.profit_and_loss" in entitlements:
            return bool(entitlements["report.profit_and_loss"])
        # If plan entitlements dict was empty or missing this key, check canonical plan defaults
        plan_defaults = default_entitlements_for_plan(plan.name)
        if key in plan_defaults:
            return bool(plan_defaults[key])

    # 3. Safe fallback
    return DEFAULT_FALLBACK_ENTITLEMENTS.get(key, False)



def get_effective_limit(
    db: Session,
    organization: Union[Organization, str, None],
    limit_key: str,
) -> Optional[int]:
    """Evaluate the effective numeric limit for an organization.

    Returns an integer limit, or None for unlimited.
    """
    if not organization or not limit_key:
        return None

    org_id: str
    plan: Optional[Plan] = None
    if isinstance(organization, str):
        org = db.query(Organization).filter(Organization.id == organization).first()
        if not org:
            return None
        org_id = org.id
        plan = org.plan
    else:
        org_id = organization.id
        plan = organization.plan

    if plan is None and getattr(organization, "plan_id", None):
        plan = db.query(Plan).filter(Plan.id == organization.plan_id).first()

    # 1. Organization Override
    override = (
        db.query(OrganizationLimitOverride)
        .filter(
            OrganizationLimitOverride.organization_id == org_id,
            OrganizationLimitOverride.limit_key == limit_key,
        )
        .first()
    )
    if override and _is_override_active(override.expires_at):
        return override.value

    # 2. Plan Default
    if plan:
        if limit_key == "max_users":
            if plan.max_users is not None:
                return plan.max_users
            return default_limits_for_plan(plan.name).get("max_users")
        elif limit_key == "max_warehouses":
            if plan.max_warehouses is not None:
                return plan.max_warehouses
            return default_limits_for_plan(plan.name).get("max_warehouses")
        elif limit_key == "max_orders":
            if plan.max_orders is not None:
                return plan.max_orders
            return default_limits_for_plan(plan.name).get("max_orders")

    # 3. Safe fallback
    return DEFAULT_FALLBACK_LIMITS.get(limit_key, None)


def check_entitlement(
    db: Session,
    organization: Union[Organization, str, None],
    entitlement_key: str,
) -> None:
    """Raise HTTP 403 PLAN_FEATURE_NOT_AVAILABLE if the feature is not entitled."""
    norm_key = normalize_entitlement_key(entitlement_key)
    if not has_entitlement(db, organization, norm_key):
        info = CANONICAL_ENTITLEMENTS.get(norm_key)
        display_name = info.get("name", norm_key) if isinstance(info, dict) else norm_key
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "PLAN_FEATURE_NOT_AVAILABLE",
                "feature": norm_key,
                "message": f"Feature '{display_name}' ({norm_key}) is not available on your current plan.",
            },
        )



def check_limit(
    db: Session,
    organization: Union[Organization, str, None],
    limit_key: str,
    current_count: int,
) -> None:
    """Raise HTTP 403 PLAN_LIMIT_REACHED if current_count meets or exceeds the effective limit."""
    effective_limit = get_effective_limit(db, organization, limit_key)
    if effective_limit is not None and current_count >= effective_limit:
        info = CANONICAL_LIMITS.get(limit_key)
        display_name = info.get("name", limit_key) if isinstance(info, dict) else limit_key
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "PLAN_LIMIT_REACHED",
                "limit_key": limit_key,
                "limit": effective_limit,
                "current": current_count,
                "message": f"Plan limit reached for {display_name}. Allowed: {effective_limit}, Current: {current_count}.",
            },
        )


def get_effective_organization_entitlements(
    db: Session,
    organization: Organization,
) -> Dict[str, Any]:
    """Build a complete summary of effective entitlements, limits, and plan details."""
    plan = organization.plan
    if plan is None and organization.plan_id:
        plan = db.query(Plan).filter(Plan.id == organization.plan_id).first()

    features: Dict[str, bool] = {}
    for key in ALL_ENTITLEMENT_KEYS:
        features[key] = has_entitlement(db, organization, key)

    limits: Dict[str, Optional[int]] = {}
    for key in ALL_LIMIT_KEYS:
        limits[key] = get_effective_limit(db, organization, key)

    # Active overrides (for non-sensitive metadata representation)
    feature_ovs = get_effective_feature_overrides(db, organization.id)
    limit_ovs = get_effective_limit_overrides(db, organization.id)

    return {
        "organization_id": organization.id,
        "organization_name": organization.name,
        "subscription_status": organization.subscription_status,
        "plan": {
            "id": plan.id if plan else None,
            "name": plan.name if plan else "Free",
            "price_monthly": plan.price_monthly if plan else 0.0,
            "price_yearly": plan.price_yearly if plan else 0.0,
            "is_default": plan.is_default if plan else False,
        } if plan else None,
        "trial_ends_at": organization.trial_ends_at,
        "trial_days_left": organization.trial_days_left,
        "plan_expires_at": organization.plan_expires_at,
        "days_left": organization.days_left,
        "upgrade_status": organization.upgrade_status,
        "features": features,
        "limits": limits,
        "active_feature_overrides_count": len(feature_ovs),
        "active_limit_overrides_count": len(limit_ovs),
    }
