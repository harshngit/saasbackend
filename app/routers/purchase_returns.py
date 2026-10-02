from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import require_permission, require_unlocked_org
from app.models import User
from app.schemas.purchase_return import (
    PurchaseReturnCancel,
    PurchaseReturnCreate,
    PurchaseReturnListResponse,
    PurchaseReturnOut,
    PurchaseReturnUpdate,
)
from app.services import purchase_return_service

router = APIRouter(prefix="/purchase-returns", tags=["purchase_returns"])

_view = require_permission("purchases", "view")
_create = require_permission("purchases", "create")
_edit = require_permission("purchases", "edit")
_approve = require_permission("purchases", "approve")
_delete = require_permission("purchases", "delete")


def _org_id(user: User) -> str:
    if not user.organization_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No organization on this account",
        )
    return user.organization_id


@router.post("", response_model=PurchaseReturnOut, status_code=status.HTTP_201_CREATED)
def create_purchase_return(
    payload: PurchaseReturnCreate,
    user: User = Depends(_create),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> PurchaseReturnOut:
    """Create a new Purchase Return record in 'draft' status."""
    org_id = _org_id(user)
    ret = purchase_return_service.create_purchase_return(
        db, org_id, payload, user_id=user.id
    )
    return PurchaseReturnOut.model_validate(ret)


@router.get("", response_model=PurchaseReturnListResponse)
def list_purchase_returns(
    user: User = Depends(_view),
    status_filter: str | None = Query(default=None, alias="status", description="Filter by status (draft, confirmed, dispatched, completed, cancelled)"),
    supplier_id: str | None = Query(default=None, description="Filter by supplier ID"),
    purchase_id: str | None = Query(default=None, description="Filter by purchase invoice ID"),
    search: str | None = Query(default=None, description="Search by return number, supplier, purchase number, or reason"),
    date_from: datetime | None = Query(default=None, description="Filter from return date"),
    date_to: datetime | None = Query(default=None, description="Filter to return date"),
    page: int | None = Query(default=None, ge=1),
    page_size: int | None = Query(default=None, ge=1, le=100),
    db: Session = Depends(get_db),
) -> PurchaseReturnListResponse:
    """List purchase returns with filtering, search, and pagination."""
    org_id = _org_id(user)
    return purchase_return_service.list_purchase_returns(
        db,
        org_id,
        status_filter=status_filter,
        supplier_id=supplier_id,
        purchase_id=purchase_id,
        search=search,
        date_from=date_from,
        date_to=date_to,
        page=page,
        page_size=page_size,
    )


@router.get("/{id}", response_model=PurchaseReturnOut)
def get_purchase_return(
    id: str,
    user: User = Depends(_view),
    db: Session = Depends(get_db),
) -> PurchaseReturnOut:
    """Retrieve detailed purchase return record by UUID or return_number."""
    org_id = _org_id(user)
    ret = purchase_return_service.get_purchase_return_detail(db, org_id, id)
    return PurchaseReturnOut.model_validate(ret)


@router.patch("/{id}", response_model=PurchaseReturnOut)
def update_purchase_return(
    id: str,
    payload: PurchaseReturnUpdate,
    user: User = Depends(_edit),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> PurchaseReturnOut:
    """Update editable fields on a draft purchase return."""
    org_id = _org_id(user)
    ret = purchase_return_service.update_purchase_return(
        db, org_id, id, payload, user_id=user.id
    )
    return PurchaseReturnOut.model_validate(ret)


@router.post("/{id}/confirm", response_model=PurchaseReturnOut)
def confirm_purchase_return(
    id: str,
    user: User = Depends(_approve),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> PurchaseReturnOut:
    """Confirm a draft purchase return (deducts stock and records timestamp)."""
    org_id = _org_id(user)
    ret = purchase_return_service.confirm_purchase_return(
        db, org_id, id, user_id=user.id
    )
    return PurchaseReturnOut.model_validate(ret)


@router.post("/{id}/dispatch", response_model=PurchaseReturnOut)
def dispatch_purchase_return(
    id: str,
    user: User = Depends(_approve),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> PurchaseReturnOut:
    """Dispatch a purchase return."""
    org_id = _org_id(user)
    ret = purchase_return_service.dispatch_purchase_return(
        db, org_id, id, user_id=user.id
    )
    return PurchaseReturnOut.model_validate(ret)


@router.post("/{id}/complete", response_model=PurchaseReturnOut)
def complete_purchase_return(
    id: str,
    user: User = Depends(_approve),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> PurchaseReturnOut:
    """Mark a purchase return as completed (terminal state)."""
    org_id = _org_id(user)
    ret = purchase_return_service.complete_purchase_return(
        db, org_id, id, user_id=user.id
    )
    return PurchaseReturnOut.model_validate(ret)


@router.post("/{id}/cancel", response_model=PurchaseReturnOut)
def cancel_purchase_return(
    id: str,
    payload: PurchaseReturnCancel | None = None,
    user: User = Depends(_approve),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> PurchaseReturnOut:
    """Cancel a purchase return (restores inventory if previously deducted)."""
    org_id = _org_id(user)
    cancel_reason = payload.cancel_reason if payload else None
    ret = purchase_return_service.cancel_purchase_return(
        db, org_id, id, cancel_reason=cancel_reason, user_id=user.id
    )
    return PurchaseReturnOut.model_validate(ret)
