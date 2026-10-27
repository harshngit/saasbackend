"""Upgrade / feature requests raised by platform staff and decided by a Super Admin.

A request changes nothing until it is approved. Every decision locks the request row
(and the organization) first, so two Super Admins clicking at once cannot both apply
it: the second sees it already decided and gets 409. Only approvals reach the
organization's activity feed -- pending requests and rejections are platform-internal.
"""
from datetime import datetime, timezone

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.models import (
    Organization,
    OrganizationFeatureOverride,
    OrganizationStatus,
    Plan,
    PlatformFeatureRequest,
    PlatformUpgradeRequest,
    UpgradeStatus,
    User,
)
from app.services import activity_service

PENDING, APPROVED, REJECTED = "pending", "approved", "rejected"
_EFFECT = {"unlock": "ALLOW", "restrict": "BLOCK"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _lock_org(db: Session, org_id: str) -> Organization:
    org = db.query(Organization).filter(Organization.id == org_id).with_for_update(of=Organization).first()
    if org is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Organization not found")
    return org


def _already_decided(request) -> HTTPException:  # noqa: ANN001
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"Request already {request.status}")


def _lock_request(db: Session, model, request_id: str):  # noqa: ANN001, ANN202
    request = db.query(model).filter(model.id == request_id).with_for_update(of=model).first()
    if request is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Request not found")
    if request.status != PENDING:
        raise _already_decided(request)
    return request


def _decide(request, caller: User, new_status: str, note: str | None) -> None:  # noqa: ANN001
    request.status = new_status
    request.decided_by_user_id = caller.id
    request.decided_at = _now()
    request.decision_note = note


# ------------------------------------------------------------------ upgrade requests


def create_upgrade_request(
    db: Session, org_id: str, caller: User, requested_plan_id: str, billing_cycle: str | None,
    reason: str, notes: str | None,
) -> PlatformUpgradeRequest:
    org = _lock_org(db, org_id)
    plan = db.get(Plan, requested_plan_id)
    if plan is None or not plan.is_active:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Requested plan is not available")
    if org.plan_id == plan.id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Organization is already on this plan")
    pending = (
        db.query(PlatformUpgradeRequest)
        .filter(PlatformUpgradeRequest.organization_id == org.id, PlatformUpgradeRequest.status == PENDING)
        .first()
    )
    if pending is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This organization already has a pending upgrade request",
        )
    request = PlatformUpgradeRequest(
        organization_id=org.id,
        current_plan_id=org.plan_id,
        requested_plan_id=plan.id,
        billing_cycle=billing_cycle,
        reason=reason.strip(),
        notes=notes,
        status=PENDING,
        requested_by_user_id=caller.id,
    )
    db.add(request)
    db.commit()
    db.refresh(request)
    return request


def approve_upgrade_request(db: Session, request_id: str, caller: User, note: str | None) -> PlatformUpgradeRequest:
    request = _lock_request(db, PlatformUpgradeRequest, request_id)
    org = _lock_org(db, request.organization_id)
    plan = db.get(Plan, request.requested_plan_id) if request.requested_plan_id else None
    if plan is None or not plan.is_active:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Requested plan is no longer available")

    org.plan_id = plan.id
    org.status = OrganizationStatus.ACTIVE
    if request.billing_cycle:
        org.billing_cycle = request.billing_cycle
    # The tenant's own pending request for this same plan is now satisfied too.
    if org.upgrade_status == UpgradeStatus.PENDING.value and org.requested_plan_id == plan.id:
        org.upgrade_status = UpgradeStatus.APPROVED.value
        org.requested_plan_id = None
        org.upgrade_reject_reason = None
    _decide(request, caller, APPROVED, note)
    activity_service.record(
        db, org.id, caller, "subscription", "Plan upgrade approved", f"Organization moved to {plan.name}",
    )
    db.commit()
    db.refresh(request)
    return request


def reject_upgrade_request(db: Session, request_id: str, caller: User, note: str | None) -> PlatformUpgradeRequest:
    request = _lock_request(db, PlatformUpgradeRequest, request_id)
    _decide(request, caller, REJECTED, note)
    db.commit()
    db.refresh(request)
    return request


# ------------------------------------------------------------------ feature requests


def create_feature_request(
    db: Session, org_id: str, caller: User, entitlement_key: str, action: str, reason: str,
) -> PlatformFeatureRequest:
    org = _lock_org(db, org_id)
    pending = (
        db.query(PlatformFeatureRequest)
        .filter(
            PlatformFeatureRequest.organization_id == org.id,
            PlatformFeatureRequest.entitlement_key == entitlement_key,
            PlatformFeatureRequest.status == PENDING,
        )
        .first()
    )
    if pending is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This feature already has a pending request for this organization",
        )
    request = PlatformFeatureRequest(
        organization_id=org.id,
        entitlement_key=entitlement_key,
        action=action,
        reason=reason.strip(),
        status=PENDING,
        requested_by_user_id=caller.id,
    )
    db.add(request)
    db.commit()
    db.refresh(request)
    return request


def approve_feature_request(db: Session, request_id: str, caller: User, note: str | None) -> PlatformFeatureRequest:
    """Writes the same organization feature override a Super Admin sets directly."""
    request = _lock_request(db, PlatformFeatureRequest, request_id)
    org = _lock_org(db, request.organization_id)
    effect = _EFFECT[request.action]
    override = (
        db.query(OrganizationFeatureOverride)
        .filter(
            OrganizationFeatureOverride.organization_id == org.id,
            OrganizationFeatureOverride.entitlement_key == request.entitlement_key,
        )
        .first()
    )
    if override is None:
        override = OrganizationFeatureOverride(organization_id=org.id, entitlement_key=request.entitlement_key)
        db.add(override)
    override.effect = effect
    override.is_allowed = effect == "ALLOW"
    override.expires_at = None
    override.reason = request.reason
    override.created_by_user_id = caller.id

    _decide(request, caller, APPROVED, note)
    activity_service.record(
        db, org.id, caller, "entitlements", "Feature override updated",
        f"Set feature override for '{request.entitlement_key}' to {effect} (approved request)",
    )
    db.commit()
    db.refresh(request)
    return request


def reject_feature_request(db: Session, request_id: str, caller: User, note: str | None) -> PlatformFeatureRequest:
    request = _lock_request(db, PlatformFeatureRequest, request_id)
    _decide(request, caller, REJECTED, note)
    db.commit()
    db.refresh(request)
    return request
