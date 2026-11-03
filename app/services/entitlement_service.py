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

import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, NamedTuple, Optional, Union

from fastapi import HTTPException, status
from sqlalchemy import event
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


class _FeatureRow(NamedTuple):
    entitlement_key: str
    effect: Optional[str]
    is_allowed: Optional[bool]
    expires_at: Optional[datetime]


class _LimitRow(NamedTuple):
    limit_key: str
    value: Any
    expires_at: Optional[datetime]


# An organization's override rows, cached briefly as plain tuples (never ORM objects) so
# a request that checks several features -- or the entitlements summary, which checks
# all of them -- reads them once instead of once per key. Any write to an override
# clears the cache (see _clear_on_override_write), so a change applies immediately in
# this process; another instance sees it within CACHE_SECONDS.
CACHE_SECONDS = 30.0
_cache: dict[tuple[str, str], tuple[float, list]] = {}
_cache_lock = threading.Lock()


def clear_override_cache() -> None:
    with _cache_lock:
        _cache.clear()


def _cached_rows(db: Session, kind: str, org_id: str) -> list:
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get((kind, org_id))
    if hit is not None and hit[0] > now:
        return hit[1]
    if kind == "feature":
        rows = [
            _FeatureRow(ov.entitlement_key, ov.effect, ov.is_allowed, ov.expires_at)
            for ov in db.query(OrganizationFeatureOverride)
            .filter(OrganizationFeatureOverride.organization_id == org_id)
            .all()
        ]
    else:
        rows = [
            _LimitRow(ov.limit_key, ov.value, ov.expires_at)
            for ov in db.query(OrganizationLimitOverride)
            .filter(OrganizationLimitOverride.organization_id == org_id)
            .all()
        ]
    with _cache_lock:
        _cache[(kind, org_id)] = (now + CACHE_SECONDS, rows)
    return rows


_OVERRIDE_TYPES = (OrganizationFeatureOverride, OrganizationLimitOverride)


@event.listens_for(Session, "after_flush")
def _clear_on_override_write(session: Session, _flush_context) -> None:  # noqa: ANN001
    if any(isinstance(obj, _OVERRIDE_TYPES) for obj in (*session.new, *session.dirty, *session.deleted)):
        session.info["crm_overrides_changed"] = True
        clear_override_cache()


@event.listens_for(Session, "do_orm_execute")
def _clear_on_override_bulk_write(state) -> None:  # noqa: ANN001
    # Bulk query.delete() / query.update() (e.g. resetting all overrides) never puts rows
    # in session.new/dirty/deleted, so after_flush above does not see them.
    if (state.is_delete or state.is_update) and state.bind_mapper is not None \
            and state.bind_mapper.class_ in _OVERRIDE_TYPES:
        state.session.info["crm_overrides_changed"] = True
        clear_override_cache()


@event.listens_for(Session, "after_commit")
def _clear_after_override_commit(session: Session) -> None:
    # Cleared again once committed: another request may have re-read the old rows
    # between this session's flush and its commit.
    if session.info.pop("crm_overrides_changed", False):
        clear_override_cache()


def _resolve_feature(plan: Optional[Plan], rows: list, key: str) -> bool:
    """Plan + override rows -> the feature decision, by the rules has_entitlement has
    always applied: the first override row for the key (or its legacy alias), if active,
    wins; then the plan; then the canonical plan defaults; then the safe fallback."""
    candidates = [key, "report.profit_and_loss"] if key == "report.profit_loss" else [key]
    override = next((r for r in rows if r.entitlement_key in candidates), None)
    if override and _is_override_active(override.expires_at):
        if override.effect == "ALLOW" or override.is_allowed is True:
            return True
        if override.effect == "BLOCK" or override.is_allowed is False:
            return False

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

    return DEFAULT_FALLBACK_ENTITLEMENTS.get(key, False)


def _resolve_limit(plan: Optional[Plan], rows: list, limit_key: str) -> tuple[Optional[Union[int, float]], str]:
    override = next((r for r in rows if r.limit_key == limit_key), None)
    if override and _is_override_active(override.expires_at):
        return override.value, "organization_override"

    if plan:
        if limit_key == "max_users":
            if plan.max_users is not None:
                return plan.max_users, "plan"
            return default_limits_for_plan(plan.name).get("max_users"), "plan"
        elif limit_key == "max_warehouses":
            if plan.max_warehouses is not None:
                return plan.max_warehouses, "plan"
            return default_limits_for_plan(plan.name).get("max_warehouses"), "plan"
        elif limit_key == "max_orders":
            if plan.max_orders is not None:
                return plan.max_orders, "plan"
            return default_limits_for_plan(plan.name).get("max_orders"), "plan"
        elif limit_key == "max_storage_gb":
            # If max_storage_gb is set on plan, return it. If explicitly None, it means unlimited.
            if plan.max_storage_gb is not None:
                return plan.max_storage_gb, "plan"
            return None, "plan"

    return DEFAULT_FALLBACK_LIMITS.get(limit_key, None), "fallback"


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

    return _resolve_feature(plan, _cached_rows(db, "feature", org_id), key)


def get_effective_limit(
    db_or_org: Any,
    org_or_key: Any,
    key_or_db: Any = None,
) -> Optional[Union[int, float]]:
    """Evaluate the effective numeric limit for an organization.

    Resolution precedence:
    1. Active unexpired organization override (organization_limit_overrides).
    2. Current Plan limit (plan.max_users, plan.max_warehouses, plan.max_orders, plan.max_storage_gb).
    3. Safe default fallback (DEFAULT_FALLBACK_LIMITS).

    Returns:
        float or int: Numeric limit ceiling.
        None: Unlimited.
    """
    if isinstance(db_or_org, Session):
        db = db_or_org
        organization = org_or_key
        limit_key = key_or_db
    else:
        organization = db_or_org
        limit_key = org_or_key
        db = key_or_db

    if not organization or not limit_key or db is None:
        return None

    limit, _ = get_effective_limit_with_source(db, organization, limit_key)
    return limit


def get_effective_limit_with_source(
    db: Session,
    organization: Union[Organization, str, None],
    limit_key: str,
) -> tuple[Optional[Union[int, float]], str]:
    """Evaluate the effective numeric limit and return (effective_limit, source).

    source is one of 'organization_override', 'plan', or 'fallback'.
    """
    if not organization or not limit_key:
        return None, "fallback"

    org_id: str
    plan: Optional[Plan] = None
    if isinstance(organization, str):
        org = db.query(Organization).filter(Organization.id == organization).first()
        if not org:
            return None, "fallback"
        org_id = org.id
        plan = org.plan
    else:
        org_id = organization.id
        plan = organization.plan

    if plan is None and getattr(organization, "plan_id", None):
        plan = db.query(Plan).filter(Plan.id == organization.plan_id).first()

    return _resolve_limit(plan, _cached_rows(db, "limit", org_id), limit_key)


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

    # Each override table is read once (and briefly cached); every key is then resolved in
    # memory by the same rules as has_entitlement / get_effective_limit.
    feature_rows = _cached_rows(db, "feature", organization.id)
    limit_rows = _cached_rows(db, "limit", organization.id)
    features: Dict[str, bool] = {}
    for key in ALL_ENTITLEMENT_KEYS:
        norm = normalize_entitlement_key(key)
        features[key] = _resolve_feature(plan, feature_rows, norm) if norm else False

    limits: Dict[str, Optional[int]] = {key: _resolve_limit(plan, limit_rows, key)[0] for key in ALL_LIMIT_KEYS}

    # Active overrides (for non-sensitive metadata representation)
    feature_ovs = {normalize_entitlement_key(r.entitlement_key) for r in feature_rows if _is_override_active(r.expires_at)}
    limit_ovs = {r.limit_key for r in limit_rows if _is_override_active(r.expires_at)}

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
