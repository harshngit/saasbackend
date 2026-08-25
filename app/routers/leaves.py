from datetime import date
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.core import scoping
from app.core.database import get_db
from app.core.deps import require_permission, require_unlocked_org
from app.models import Leave, SystemRole, User
from app.schemas.leave import LeaveCreate, LeaveOut, LeaveRejectBody, LeaveUpdate
from app.services import leave_service

router = APIRouter(prefix="/leaves", tags=["leaves"])

_view = require_permission("leaves", "view")
_create = require_permission("leaves", "create")
_edit = require_permission("leaves", "edit")
_approve = require_permission("leaves", "approve")
_delete = require_permission("leaves", "delete")


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


@router.get("/me", response_model=list[LeaveOut])
def my_leaves(
    user: User = Depends(_view),
    status_filter: str | None = Query(default=None, alias="status"),
    db: Session = Depends(get_db),
) -> list[Leave]:
    org_id = _org_id(user)
    query = db.query(Leave).filter(Leave.organization_id == org_id, Leave.user_id == user.id)
    if status_filter:
        query = query.filter(Leave.status == status_filter)
    return query.order_by(Leave.created_at.desc()).all()


@router.get("", response_model=list[LeaveOut])
def list_leaves(
    user_id: str | None = Query(default=None),
    status_filter: str | None = Query(default=None, alias="status"),
    leave_type: str | None = Query(default=None),
    date_from: date | None = Query(default=None),
    date_to: date | None = Query(default=None),
    user: User = Depends(_view),
    db: Session = Depends(get_db),
) -> list[Leave]:
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

    return query.order_by(Leave.created_at.desc()).all()


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
