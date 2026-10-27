"""Platform Operations: platform staff (Super Admin + Sales Manager) work across organizations.

- Super Admin and Sales Manager: read organizations (detail, plan, features, billing),
  raise plan-upgrade and feature-change requests, read requests.
- Super Admin only: approve / reject requests, manage platform staff.

A Sales Manager never changes an organization directly: plan and feature changes only
happen when a Super Admin approves a request. Every rule is enforced here; the
frontend hiding a button is never the protection.
"""
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import require_system_role
from app.core.entitlements import CANONICAL_ENTITLEMENTS, ENTITLEMENT_CATEGORIES
from app.core.pagination import paginate
from app.core.security import hash_password
from app.models import (
    Organization,
    OrganizationStatus,
    Plan,
    PLATFORM_STAFF_ROLES,
    PlatformFeatureRequest,
    PlatformUpgradeRequest,
    SystemRole,
    UpgradeStatus,
    User,
)
from app.routers import superadmin
from app.schemas.organization import OrganizationOut
from app.schemas.organization_override import OrganizationAdminEntitlementsOut
from app.schemas.pagination import PaginatedResponse
from app.schemas.plan import PlanOut
from app.schemas.platform import (
    FeatureCatalogItem,
    FeatureRequestCreate,
    FeatureRequestOut,
    PlatformStaffCreate,
    PlatformStaffOut,
    PlatformStaffUpdate,
    PlatformSummary,
    RequestDecision,
    UpgradeRequestCreate,
    UpgradeRequestOut,
)
from app.schemas.razorpay import SubscriptionPaymentOut
from app.services import billing_service, password_service, platform_request_service

_platform_guard = require_system_role(SystemRole.SUPER_ADMIN, SystemRole.SALES_MANAGER)
_super_admin_guard = require_system_role(SystemRole.SUPER_ADMIN)

router = APIRouter(prefix="/platform", tags=["platform"], dependencies=[Depends(_platform_guard)])

# ------------------------------------------------------------------ overview


@router.get("/summary", response_model=PlatformSummary)
def summary(db: Session = Depends(get_db)) -> PlatformSummary:
    """Landing-page counts."""
    return PlatformSummary(
        organizations=db.query(Organization).count(),
        pending_upgrade_requests=db.query(PlatformUpgradeRequest)
        .filter(PlatformUpgradeRequest.status == "pending").count(),
        pending_feature_requests=db.query(PlatformFeatureRequest)
        .filter(PlatformFeatureRequest.status == "pending").count(),
    )


@router.get("/plans", response_model=list[PlanOut])
def list_plans(db: Session = Depends(get_db)) -> list[Plan]:
    """Active plans: the choices for "Requested Plan"."""
    return db.query(Plan).filter(Plan.is_active.is_(True)).order_by(Plan.price_monthly).all()


@router.get("/features", response_model=list[FeatureCatalogItem])
def list_features() -> list[FeatureCatalogItem]:
    """Every feature key: the choices for "Feature" in a feature-change request."""
    return [
        FeatureCatalogItem(
            key=key,
            name=meta.get("name", key),
            category=meta.get("category", ""),
            category_name=ENTITLEMENT_CATEGORIES.get(meta.get("category", ""), ""),
            description=meta.get("description", ""),
        )
        for key, meta in sorted(CANONICAL_ENTITLEMENTS.items(), key=lambda kv: (kv[1].get("category", ""), kv[0]))
    ]


# ------------------------------------------------------------------ organizations (read only)


@router.get("/organizations", response_model=PaginatedResponse[OrganizationOut])
def list_organizations(
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1, le=100),
    search: str | None = Query(default=None),
    status_filter: OrganizationStatus | None = Query(default=None, alias="status"),
    upgrade_status: UpgradeStatus | None = Query(default=None),
    sort_by: str | None = Query(default=None),
    sort_order: str | None = Query(default="desc"),
    db: Session = Depends(get_db),
) -> PaginatedResponse[OrganizationOut]:
    """Same list, filters and sorting as GET /superadmin/organizations."""
    return superadmin.list_organizations(
        page=page, page_size=page_size, search=search, status_filter=status_filter,
        upgrade_status=upgrade_status, sort_by=sort_by, sort_order=sort_order, db=db,
    )


@router.get("/organizations/{org_id}", response_model=OrganizationOut)
def get_organization(org_id: str, db: Session = Depends(get_db)) -> Organization:
    return superadmin._get_org(db, org_id)


@router.get("/organizations/{org_id}/entitlements", response_model=OrganizationAdminEntitlementsOut)
def get_organization_entitlements(org_id: str, db: Session = Depends(get_db)) -> dict:
    """Plan, effective features (enabled / restricted), limits and overrides."""
    return superadmin.get_organization_entitlements_admin(org_id=org_id, db=db)


@router.get("/organizations/{org_id}/subscription-payments", response_model=PaginatedResponse[SubscriptionPaymentOut])
def list_organization_payments(
    org_id: str,
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1, le=100),
    db: Session = Depends(get_db),
) -> PaginatedResponse[SubscriptionPaymentOut]:
    """The organization's subscription payments (basic billing history)."""
    superadmin._get_org(db, org_id)
    return billing_service.list_payments(
        db, organization_id=org_id, page=page, page_size=page_size,
        search=None, status=None, sort_by=None, sort_order="desc",
    )


# ------------------------------------------------------------------ upgrade requests


@router.post(
    "/organizations/{org_id}/upgrade-requests",
    response_model=UpgradeRequestOut,
    status_code=status.HTTP_201_CREATED,
)
def create_upgrade_request(
    org_id: str,
    payload: UpgradeRequestCreate,
    caller: User = Depends(_platform_guard),
    db: Session = Depends(get_db),
) -> PlatformUpgradeRequest:
    return platform_request_service.create_upgrade_request(
        db, org_id, caller, payload.requested_plan_id,
        payload.billing_cycle.value if payload.billing_cycle else None, payload.reason, payload.notes,
    )


@router.get("/upgrade-requests", response_model=PaginatedResponse[UpgradeRequestOut])
def list_upgrade_requests(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    status_filter: str | None = Query(default=None, alias="status", pattern="^(pending|approved|rejected)$"),
    organization_id: str | None = Query(default=None),
    mine: bool = Query(default=False),
    caller: User = Depends(_platform_guard),
    db: Session = Depends(get_db),
) -> PaginatedResponse[UpgradeRequestOut]:
    """Newest first. ?status=pending|approved|rejected, ?organization_id=, ?mine=true."""
    query = db.query(PlatformUpgradeRequest)
    if status_filter:
        query = query.filter(PlatformUpgradeRequest.status == status_filter)
    if organization_id:
        query = query.filter(PlatformUpgradeRequest.organization_id == organization_id)
    if mine:
        query = query.filter(PlatformUpgradeRequest.requested_by_user_id == caller.id)
    query = query.order_by(PlatformUpgradeRequest.created_at.desc(), PlatformUpgradeRequest.id.desc())
    return paginate(query, page=page, page_size=page_size)


@router.get("/upgrade-requests/{request_id}", response_model=UpgradeRequestOut)
def get_upgrade_request(request_id: str, db: Session = Depends(get_db)) -> PlatformUpgradeRequest:
    request = db.get(PlatformUpgradeRequest, request_id)
    if request is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Request not found")
    return request


@router.post("/upgrade-requests/{request_id}/approve", response_model=UpgradeRequestOut)
def approve_upgrade_request(
    request_id: str,
    payload: RequestDecision | None = None,
    caller: User = Depends(_super_admin_guard),
    db: Session = Depends(get_db),
) -> PlatformUpgradeRequest:
    """Super Admin: move the organization onto the requested plan."""
    return platform_request_service.approve_upgrade_request(db, request_id, caller, payload.note if payload else None)


@router.post("/upgrade-requests/{request_id}/reject", response_model=UpgradeRequestOut)
def reject_upgrade_request(
    request_id: str,
    payload: RequestDecision | None = None,
    caller: User = Depends(_super_admin_guard),
    db: Session = Depends(get_db),
) -> PlatformUpgradeRequest:
    """Super Admin: close the request. The organization is not changed."""
    return platform_request_service.reject_upgrade_request(db, request_id, caller, payload.note if payload else None)


# ------------------------------------------------------------------ feature requests


@router.post(
    "/organizations/{org_id}/feature-requests",
    response_model=FeatureRequestOut,
    status_code=status.HTTP_201_CREATED,
)
def create_feature_request(
    org_id: str,
    payload: FeatureRequestCreate,
    caller: User = Depends(_platform_guard),
    db: Session = Depends(get_db),
) -> PlatformFeatureRequest:
    return platform_request_service.create_feature_request(
        db, org_id, caller, payload.entitlement_key, payload.action, payload.reason,
    )


@router.get("/feature-requests", response_model=PaginatedResponse[FeatureRequestOut])
def list_feature_requests(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    status_filter: str | None = Query(default=None, alias="status", pattern="^(pending|approved|rejected)$"),
    organization_id: str | None = Query(default=None),
    mine: bool = Query(default=False),
    caller: User = Depends(_platform_guard),
    db: Session = Depends(get_db),
) -> PaginatedResponse[FeatureRequestOut]:
    """Newest first. ?status=pending|approved|rejected, ?organization_id=, ?mine=true."""
    query = db.query(PlatformFeatureRequest)
    if status_filter:
        query = query.filter(PlatformFeatureRequest.status == status_filter)
    if organization_id:
        query = query.filter(PlatformFeatureRequest.organization_id == organization_id)
    if mine:
        query = query.filter(PlatformFeatureRequest.requested_by_user_id == caller.id)
    query = query.order_by(PlatformFeatureRequest.created_at.desc(), PlatformFeatureRequest.id.desc())
    return paginate(query, page=page, page_size=page_size)


@router.get("/feature-requests/{request_id}", response_model=FeatureRequestOut)
def get_feature_request(request_id: str, db: Session = Depends(get_db)) -> PlatformFeatureRequest:
    request = db.get(PlatformFeatureRequest, request_id)
    if request is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Request not found")
    return request


@router.post("/feature-requests/{request_id}/approve", response_model=FeatureRequestOut)
def approve_feature_request(
    request_id: str,
    payload: RequestDecision | None = None,
    caller: User = Depends(_super_admin_guard),
    db: Session = Depends(get_db),
) -> PlatformFeatureRequest:
    """Super Admin: apply the feature override (unlock = ALLOW, restrict = BLOCK)."""
    return platform_request_service.approve_feature_request(db, request_id, caller, payload.note if payload else None)


@router.post("/feature-requests/{request_id}/reject", response_model=FeatureRequestOut)
def reject_feature_request(
    request_id: str,
    payload: RequestDecision | None = None,
    caller: User = Depends(_super_admin_guard),
    db: Session = Depends(get_db),
) -> PlatformFeatureRequest:
    """Super Admin: close the request. Feature access is not changed."""
    return platform_request_service.reject_feature_request(db, request_id, caller, payload.note if payload else None)


# ------------------------------------------------------------------ platform staff (Super Admin)


def _get_staff(db: Session, staff_id: str) -> User:
    staff = db.get(User, staff_id)
    if staff is None or staff.organization_id is not None or staff.system_role not in PLATFORM_STAFF_ROLES:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Staff member not found")
    return staff


@router.get("/staff", response_model=list[PlatformStaffOut], dependencies=[Depends(_super_admin_guard)])
def list_staff(
    search: str | None = Query(default=None),
    is_active: bool | None = Query(default=None),
    db: Session = Depends(get_db),
) -> list[User]:
    """Platform staff (Sales Managers). Super Admins are managed at /superadmin/admins."""
    query = db.query(User).filter(User.organization_id.is_(None), User.system_role.in_(PLATFORM_STAFF_ROLES))
    if is_active is not None:
        query = query.filter(User.is_active.is_(is_active))
    if search:
        s = f"%{search.strip()}%"
        query = query.filter(or_(User.name.ilike(s), User.email.ilike(s)))
    return query.order_by(User.created_at.desc()).all()


@router.post(
    "/staff",
    response_model=PlatformStaffOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(_super_admin_guard)],
)
def create_staff(payload: PlatformStaffCreate, db: Session = Depends(get_db)) -> User:
    """A platform staff account with a password set by the Super Admin (no invite flow)."""
    email = str(payload.email)
    if superadmin._email_taken(db, email):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Email already registered")
    staff = User(
        organization_id=None,
        name=payload.name.strip(),
        email=email,
        phone=payload.phone,
        password_hash=hash_password(payload.password),
        role=None,
        system_role=payload.role,
        is_active=True,
    )
    db.add(staff)
    db.commit()
    db.refresh(staff)
    return staff


@router.get("/staff/{staff_id}", response_model=PlatformStaffOut, dependencies=[Depends(_super_admin_guard)])
def get_staff(staff_id: str, db: Session = Depends(get_db)) -> User:
    return _get_staff(db, staff_id)


@router.patch("/staff/{staff_id}", response_model=PlatformStaffOut, dependencies=[Depends(_super_admin_guard)])
def update_staff(staff_id: str, payload: PlatformStaffUpdate, db: Session = Depends(get_db)) -> User:
    """Edit details, reset the password, or activate / deactivate. Deactivating (or a
    new password) ends the member's sessions at once: refresh tokens are revoked and
    every request with an existing access token is refused."""
    staff = _get_staff(db, staff_id)
    data = payload.model_dump(exclude_unset=True)
    if data.get("email"):
        data["email"] = str(data["email"])
        if data["email"] != staff.email and superadmin._email_taken(db, data["email"], exclude_id=staff.id):
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Email already registered")
    password = data.pop("password", None)
    role = data.pop("role", None)
    for field in ("name", "email", "phone", "is_active"):
        if field in data and (data[field] is not None or field == "phone"):
            setattr(staff, field, data[field])
    if role:
        staff.system_role = role
    if password:
        staff.password_hash = hash_password(password)
    db.commit()
    if password or staff.is_active is False:
        password_service.revoke_all_refresh_tokens(db, staff.id)
    db.refresh(staff)
    return staff
