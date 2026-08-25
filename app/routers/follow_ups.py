from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import require_permission, require_unlocked_org
from app.models import FollowUp, User
from app.schemas.follow_up import FollowUpCreate, FollowUpOut, FollowUpUpdate
from app.services import follow_up_service

router = APIRouter(prefix="/follow-ups", tags=["follow_ups"])

_view = require_permission("follow_ups", "view")
_create = require_permission("follow_ups", "create")
_edit = require_permission("follow_ups", "edit")
_delete = require_permission("follow_ups", "delete")


def _org_id(user: User) -> str:
    if not user.organization_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No organization on this account")
    return user.organization_id


@router.post("", response_model=FollowUpOut, status_code=status.HTTP_201_CREATED)
def create_follow_up(
    payload: FollowUpCreate,
    user: User = Depends(_create),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> FollowUp:
    org_id = _org_id(user)
    return follow_up_service.create_follow_up(db, org_id, user, payload)


@router.get("", response_model=list[FollowUpOut])
def list_follow_ups(
    customer_id: str | None = Query(default=None),
    visit_id: str | None = Query(default=None),
    assigned_to_id: str | None = Query(default=None),
    status: str | None = Query(default=None),
    priority: str | None = Query(default=None),
    due_before: datetime | None = Query(default=None),
    due_after: datetime | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    user: User = Depends(_view),
    db: Session = Depends(get_db),
) -> list[FollowUp]:
    org_id = _org_id(user)
    return follow_up_service.list_follow_ups(
        db,
        org_id,
        user,
        customer_id=customer_id,
        visit_id=visit_id,
        assigned_to_id=assigned_to_id,
        status_filter=status,
        priority=priority,
        due_before=due_before,
        due_after=due_after,
        limit=limit,
        offset=offset,
    )


@router.get("/{id}", response_model=FollowUpOut)
def get_follow_up(
    id: str,
    user: User = Depends(_view),
    db: Session = Depends(get_db),
) -> FollowUp:
    org_id = _org_id(user)
    return follow_up_service.get_follow_up(db, org_id, id, user)


@router.patch("/{id}", response_model=FollowUpOut)
def update_follow_up(
    id: str,
    payload: FollowUpUpdate,
    user: User = Depends(_edit),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> FollowUp:
    org_id = _org_id(user)
    return follow_up_service.update_follow_up(db, org_id, id, user, payload)


@router.post("/{id}/complete", response_model=FollowUpOut)
def complete_follow_up(
    id: str,
    user: User = Depends(_edit),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> FollowUp:
    org_id = _org_id(user)
    return follow_up_service.complete_follow_up(db, org_id, id, user)


@router.delete("/{id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_follow_up(
    id: str,
    user: User = Depends(_delete),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> None:
    org_id = _org_id(user)
    follow_up_service.delete_follow_up(db, org_id, id, user)
