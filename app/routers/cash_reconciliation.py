from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import require_permission, require_unlocked_org
from app.core.pagination import paginate
from app.models import User
from app.models.cash_reconciliation import CashReconciliation
from app.schemas.cash_reconciliation import (
    CashReconciliationCloseIn,
    CashReconciliationCreate,
    CashReconciliationOut,
    CashReconciliationUpdate,
    ExpectedCollectionsPreview,
    MethodDetailOut,
)
from app.schemas.pagination import PaginatedResponse
from app.services import cash_reconciliation_service

router = APIRouter(prefix="/cash-reconciliation", tags=["cash_reconciliation"])

_view = require_permission("payments", "view")
_create = require_permission("payments", "create")
_edit = require_permission("payments", "edit")
_delete = require_permission("payments", "delete")


def _org_id(user: User) -> str:
    if not user.organization_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="No organization on this account"
        )
    return user.organization_id


def _owned(db: Session, rec_id: str, org_id: str) -> CashReconciliation:
    rec = db.get(CashReconciliation, rec_id)
    if rec is None or rec.organization_id != org_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Cash reconciliation not found"
        )
    return rec


@router.get("/expected", response_model=ExpectedCollectionsPreview)
@router.get("/preview", response_model=ExpectedCollectionsPreview)
def preview_expected(
    reconciliation_date: Optional[date] = Query(default=None),
    user: User = Depends(_view),
    db: Session = Depends(get_db),
) -> ExpectedCollectionsPreview:
    """Preview expected collections and cash movements for a business date."""
    org_id = _org_id(user)
    target_date = reconciliation_date or date.today()
    figures = cash_reconciliation_service.calculate_expected_figures(db, org_id, target_date)

    details = [
        MethodDetailOut(
            payment_method=m,
            expected_amount=amt,
            actual_amount=amt,
            variance_amount=amt - amt,
        )
        for m, amt in figures["method_totals"].items()
    ]

    expected_closing = (
        figures["cash_sales"]
        + figures["cash_received"]
        - figures["cash_paid"]
        - figures["expenses_paid"]
    )

    return ExpectedCollectionsPreview(
        reconciliation_date=target_date,
        total_expected_collections=figures["total_expected_collections"],
        details=details,
        opening_balance=0.0,
        cash_sales=figures["cash_sales"],
        cash_received=figures["cash_received"],
        cash_paid=figures["cash_paid"],
        expenses_paid=figures["expenses_paid"],
        expected_closing_cash=expected_closing,
    )


@router.get("", response_model=PaginatedResponse[CashReconciliationOut])
def list_reconciliations(
    date_from: Optional[date] = Query(default=None),
    date_to: Optional[date] = Query(default=None),
    status_filter: Optional[str] = Query(default=None, alias="status"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=10, ge=1, le=100),
    user: User = Depends(_view),
    db: Session = Depends(get_db),
) -> PaginatedResponse[CashReconciliationOut]:
    """List cash reconciliation history with date range and status filters."""
    org_id = _org_id(user)
    query = db.query(CashReconciliation).filter(CashReconciliation.organization_id == org_id)

    if date_from:
        query = query.filter(CashReconciliation.reconciliation_date >= date_from)
    if date_to:
        query = query.filter(CashReconciliation.reconciliation_date <= date_to)
    if status_filter:
        query = query.filter(CashReconciliation.status == status_filter.strip().lower())

    query = query.order_by(
        CashReconciliation.reconciliation_date.desc(), CashReconciliation.created_at.desc()
    )

    items, total, page, page_size, total_pages = paginate(query, page=page, page_size=page_size)

    return PaginatedResponse(
        items=items,
        total=total,
        page=page,
        page_size=page_size,
        total_pages=total_pages,
    )


@router.get("/for-date/{business_date}", response_model=CashReconciliationOut)
def get_reconciliation_for_date(
    business_date: date,
    refresh: bool = Query(default=True, description="Refresh draft expected figures from live data"),
    user: User = Depends(_view),
    db: Session = Depends(get_db),
) -> CashReconciliationOut:
    """Retrieve the cash reconciliation session for a specific business date."""
    org_id = _org_id(user)
    rec = (
        db.query(CashReconciliation)
        .filter(
            CashReconciliation.organization_id == org_id,
            CashReconciliation.reconciliation_date == business_date,
        )
        .first()
    )
    if rec is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No cash reconciliation found for date {business_date.isoformat()}",
        )

    if rec.status == "draft" and refresh:
        cash_reconciliation_service.refresh_draft_if_needed(db, rec)

    return rec


@router.get("/{id}", response_model=CashReconciliationOut)
def get_reconciliation_detail(
    id: str,
    refresh: bool = Query(default=True, description="Refresh draft expected figures from live data"),
    user: User = Depends(_view),
    db: Session = Depends(get_db),
) -> CashReconciliationOut:
    """Retrieve detailed record of a cash reconciliation session."""
    org_id = _org_id(user)
    rec = _owned(db, id, org_id)

    if rec.status == "draft" and refresh:
        cash_reconciliation_service.refresh_draft_if_needed(db, rec)

    return rec


@router.post("", response_model=CashReconciliationOut, status_code=status.HTTP_201_CREATED)
def create_reconciliation(
    payload: CashReconciliationCreate,
    user: User = Depends(_create),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> CashReconciliationOut:
    """Create a new cash reconciliation draft for a business date."""
    org_id = _org_id(user)
    return cash_reconciliation_service.create_draft(db, org_id, user, payload)


@router.patch("/{id}", response_model=CashReconciliationOut)
def update_reconciliation(
    id: str,
    payload: CashReconciliationUpdate,
    user: User = Depends(_edit),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> CashReconciliationOut:
    """Update editable fields of a draft reconciliation session."""
    org_id = _org_id(user)
    rec = _owned(db, id, org_id)
    return cash_reconciliation_service.update_draft(db, rec, payload, user)


@router.post("/{id}/close", response_model=CashReconciliationOut)
def close_reconciliation(
    id: str,
    payload: Optional[CashReconciliationCloseIn] = None,
    user: User = Depends(_edit),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> CashReconciliationOut:
    """Close and permanently freeze a cash reconciliation session."""
    org_id = _org_id(user)
    return cash_reconciliation_service.close_reconciliation(db, id, org_id, user, payload)


@router.delete("/{id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_reconciliation(
    id: str,
    user: User = Depends(_delete),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> None:
    """Delete a draft reconciliation session."""
    org_id = _org_id(user)
    rec = _owned(db, id, org_id)
    cash_reconciliation_service.delete_draft(db, rec, user)
