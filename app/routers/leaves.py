from datetime import date
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core import notification_types as nt
from app.core import scoping
from app.core.database import get_db
from app.core.deps import require_entitlement, require_permission, require_unlocked_org
from app.core.pagination import paginate
from app.core.sorting import apply_sorting
from app.models import Leave, SystemRole, User
from app.schemas.bulk import BulkDelete, BulkDeleteResult
from app.schemas.leave import LeaveCreate, LeaveOut, LeaveRejectBody, LeaveUpdate
from app.schemas.pagination import PaginatedResponse
from app.services import leave_service, notification_service

router = APIRouter(
    prefix="/leaves",
    tags=["leaves"],
    dependencies=[Depends(require_entitlement("employee.leaves"))],
)

_view = require_permission("leaves", "view")
_create = require_permission("leaves", "create")
_edit = require_permission("leaves", "edit")
_approve = require_permission("leaves", "approve")
_delete = require_permission("leaves", "delete")

LEAVE_SORTABLE_FIELDS = {
    "leave_type": Leave.leave_type,
    "type": Leave.leave_type,
    "start_date": Leave.start_date,
    "date": Leave.start_date,
    "end_date": Leave.end_date,
    "days_count": Leave.days_count,
    "days": Leave.days_count,
    "number_of_days": Leave.days_count,
    "status": Leave.status,
    "employee": select(User.name).where(User.id == Leave.user_id).scalar_subquery(),
    "employee_name": select(User.name).where(User.id == Leave.user_id).scalar_subquery(),
    "user": select(User.name).where(User.id == Leave.user_id).scalar_subquery(),
    "name": select(User.name).where(User.id == Leave.user_id).scalar_subquery(),
    "created_at": Leave.created_at,
}


def _org_id(user: User) -> str:
    if not user.organization_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No organization on this account")
    return user.organization_id


def _can_manage_all(user: User) -> bool:
    if user.system_role in (SystemRole.ADMIN.value, SystemRole.SUPER_ADMIN.value):
        return True
    if user.role_detail and user.role_detail.permissions:
        perms = user.role_detail.permissions.get("leaves", {})
        if perms.get("approve", False):
            return True
    return False


def _owned(db: Session, leave_id: str, org_id: str, user: User | None = None) -> Leave:
    record = (
        db.query(Leave)
        .filter(Leave.id == leave_id, Leave.organization_id == org_id)
        .first()
    )
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Leave not found")
    if user and not _can_manage_all(user):
        if not scoping.owns_record(db, user, record, "user_id"):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Leave not found")
    return record


@router.post("", response_model=LeaveOut, status_code=status.HTTP_201_CREATED)
def create_leave(
    payload: LeaveCreate,
    user: User = Depends(_create),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> Leave:
    org_id = _org_id(user)
    days_count = leave_service.calculate_days_count(payload.start_date, payload.end_date)

    leave = Leave(
        organization_id=org_id,
        user_id=user.id,
        leave_type=payload.leave_type,
        start_date=payload.start_date,
        end_date=payload.end_date,
        days_count=days_count,
        reason=payload.reason,
        status="pending",
    )
    db.add(leave)
    db.commit()
    db.refresh(leave)
    return leave


@router.get("/me", response_model=PaginatedResponse[LeaveOut])
def my_leaves(
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1, le=100),
    sort_by: str | None = Query(default=None, description="Field to sort by"),
    sort_order: str = Query(default="asc", description="Sort order: asc or desc"),
    user: User = Depends(_view),
    status_filter: str | None = Query(default=None, alias="status"),
    db: Session = Depends(get_db),
) -> PaginatedResponse[LeaveOut]:
    org_id = _org_id(user)
    query = db.query(Leave).filter(Leave.organization_id == org_id, Leave.user_id == user.id)
    if status_filter:
        query = query.filter(Leave.status == status_filter)
    query = apply_sorting(
        query,
        sort_by=sort_by,
        sort_order=sort_order,
        sortable_fields=LEAVE_SORTABLE_FIELDS,
        default_order=[Leave.created_at.desc(), Leave.id.desc()],
        secondary_key=Leave.id.desc(),
    )
    return paginate(query, page=page, page_size=page_size)


@router.get("", response_model=PaginatedResponse[LeaveOut])
def list_leaves(
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1, le=100),
    sort_by: str | None = Query(default=None, description="Field to sort by"),
    sort_order: str = Query(default="asc", description="Sort order: asc or desc"),
    search: str | None = Query(default=None),
    user_id: str | None = Query(default=None),
    status_filter: str | None = Query(default=None, alias="status"),
    leave_type: str | None = Query(default=None),
    date_from: date | None = Query(default=None),
    date_to: date | None = Query(default=None),
    user: User = Depends(_view),
    db: Session = Depends(get_db),
) -> PaginatedResponse[LeaveOut]:
    org_id = _org_id(user)
    query = db.query(Leave).filter(Leave.organization_id == org_id)

    if not _can_manage_all(user):
        query = query.filter(Leave.user_id == user.id)
    elif user_id:
        query = query.filter(Leave.user_id == user_id)

    if status_filter:
        query = query.filter(Leave.status == status_filter)
    if leave_type:
        query = query.filter(Leave.leave_type == leave_type)
    if date_from:
        query = query.filter(Leave.start_date >= date_from)
    if date_to:
        query = query.filter(Leave.end_date <= date_to)
    if search:
        s = f"%{search.strip()}%"
        query = query.filter(
            or_(
                Leave.reason.ilike(s),
                Leave.leave_type.ilike(s),
            )
        )

    query = apply_sorting(
        query,
        sort_by=sort_by,
        sort_order=sort_order,
        sortable_fields=LEAVE_SORTABLE_FIELDS,
        default_order=[Leave.created_at.desc(), Leave.id.desc()],
        secondary_key=Leave.id.desc(),
    )
    return paginate(query, page=page, page_size=page_size)


@router.post("/bulk-delete", response_model=BulkDeleteResult)
@router.delete("/bulk-delete", response_model=BulkDeleteResult)
def bulk_delete_leaves(
    payload: BulkDelete,
    user: User = Depends(_delete),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> BulkDeleteResult:
    """Bulk delete pending leave requests.

    Preserves approved and rejected leave records. Only pending leaves can be deleted.
    Atomic (all-or-nothing).
    """
    org_id = _org_id(user)
    unique_ids = list(dict.fromkeys(payload.ids))
    leaves = []
    for lid in unique_ids:
        leave = _owned(db, lid, org_id, user)
        if leave.status != "pending":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Leave request '{lid}' is '{leave.status}'. Only pending leaves can be cancelled/deleted",
            )
        leaves.append(leave)

    try:
        for leave in leaves:
            db.delete(leave)
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Cannot delete leaves due to database integrity constraints: {exc.orig}",
        ) from exc
    return BulkDeleteResult(deleted=len(unique_ids))


@router.get("/{id}", response_model=LeaveOut)
def get_leave(
    id: str,
    user: User = Depends(_view),
    db: Session = Depends(get_db),
) -> Leave:
    org_id = _org_id(user)
    return _owned(db, id, org_id, user)


@router.patch("/{id}", response_model=LeaveOut)
def update_leave(
    id: str,
    payload: LeaveUpdate,
    user: User = Depends(_edit),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> Leave:
    org_id = _org_id(user)
    leave = _owned(db, id, org_id, user)

    if leave.status != "pending":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Only pending leaves can be edited",
        )

    update_data = payload.model_dump(exclude_unset=True)

    new_start = update_data.get("start_date", leave.start_date)
    new_end = update_data.get("end_date", leave.end_date)
    if new_start > new_end:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="start_date cannot be after end_date",
        )

    leave.days_count = leave_service.calculate_days_count(new_start, new_end)

    for field, value in update_data.items():
        setattr(leave, field, value)

    db.commit()
    db.refresh(leave)
    return leave


def _notify_leave_decision(db: Session, leave: Leave, actor: User) -> None:
    """Tell the applicant their leave was approved or rejected (not when they decided it themselves)."""
    if leave.user_id == actor.id:
        return
    notification_service.notify(
        db, leave.user_id, f"Leave {leave.status}", f"{leave.start_date} to {leave.end_date}",
        organization_id=leave.organization_id,
        data=nt.build_data(nt.LEAVE_STATUS_CHANGED, leave_id=leave.id, status=leave.status),
    )


@router.patch("/{id}/approve", response_model=LeaveOut)
def approve_leave(
    id: str,
    user: User = Depends(_approve),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> Leave:
    org_id = _org_id(user)
    leave = _owned(db, id, org_id)

    if leave.status != "pending":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Only pending leaves can be approved",
        )

    leave.status = "approved"
    leave.approved_by = user.id
    _notify_leave_decision(db, leave, user)
    db.commit()
    db.refresh(leave)
    return leave


@router.patch("/{id}/reject", response_model=LeaveOut)
def reject_leave(
    id: str,
    payload: LeaveRejectBody,
    user: User = Depends(_approve),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> Leave:
    org_id = _org_id(user)
    leave = _owned(db, id, org_id)

    if leave.status != "pending":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Only pending leaves can be rejected",
        )

    leave.status = "rejected"
    leave.approved_by = user.id
    leave.reject_reason = payload.reject_reason
    _notify_leave_decision(db, leave, user)
    db.commit()
    db.refresh(leave)
    return leave


@router.delete("/{id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_leave(
    id: str,
    user: User = Depends(_delete),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> None:
    org_id = _org_id(user)
    leave = _owned(db, id, org_id, user)

    if leave.status != "pending":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Only pending leaves can be cancelled/deleted",
        )

    db.delete(leave)
    db.commit()
