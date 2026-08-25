from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import require_permission, require_unlocked_org
from app.models import User, Visit
from app.schemas.follow_up import FollowUpCreate, FollowUpOut
from app.schemas.visit import VisitCreate, VisitOut, VisitUpdate
from app.services import follow_up_service, visit_service

router = APIRouter(prefix="/visits", tags=["visits"])

_view = require_permission("visits", "view")
_create = require_permission("visits", "create")
_edit = require_permission("visits", "edit")
_delete = require_permission("visits", "delete")
_create_fu = require_permission("follow_ups", "create")


def _org_id(user: User) -> str:
    if not user.organization_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No organization on this account")
    return user.organization_id


@router.post("", response_model=VisitOut, status_code=status.HTTP_201_CREATED)
def create_visit(
    payload: VisitCreate,
    user: User = Depends(_create),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> Visit:
    org_id = _org_id(user)
    return visit_service.create_visit(db, org_id, user, payload)


@router.get("", response_model=list[VisitOut])
def list_visits(
    customer_id: str | None = Query(default=None),
    lead_id: str | None = Query(default=None),
    salesperson_id: str | None = Query(default=None),
    status: str | None = Query(default=None),
    date_from: datetime | None = Query(default=None),
    date_to: datetime | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    user: User = Depends(_view),
    db: Session = Depends(get_db),
) -> list[Visit]:
    org_id = _org_id(user)
    return visit_service.list_visits(
        db,
        org_id,
        user,
        customer_id=customer_id,
        lead_id=lead_id,
        salesperson_id=salesperson_id,
        status_filter=status,
        date_from=date_from,
        date_to=date_to,
        limit=limit,
        offset=offset,
    )


@router.get("/{id}", response_model=VisitOut)
def get_visit(
    id: str,
    user: User = Depends(_view),
    db: Session = Depends(get_db),
) -> Visit:
    org_id = _org_id(user)
    return visit_service.get_visit(db, org_id, id, user)


@router.patch("/{id}", response_model=VisitOut)
def update_visit(
    id: str,
    payload: VisitUpdate,
    user: User = Depends(_edit),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> Visit:
    org_id = _org_id(user)
    return visit_service.update_visit(db, org_id, id, user, payload)


@router.delete("/{id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_visit(
    id: str,
    user: User = Depends(_delete),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> None:
    org_id = _org_id(user)
    visit_service.delete_visit(db, org_id, id, user)


@router.post("/{visit_id}/follow-ups", response_model=FollowUpOut, status_code=status.HTTP_201_CREATED)
def create_visit_follow_up(
    visit_id: str,
    payload: FollowUpCreate,
    user: User = Depends(_create_fu),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
):
    org_id = _org_id(user)
    return follow_up_service.create_follow_up(db, org_id, user, payload, default_visit_id=visit_id)
