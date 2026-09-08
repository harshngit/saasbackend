from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import require_permission
from app.models import User
from app.schemas.accounts_payable import (
    APAgeingBucketOut,
    APListResponse,
    APSummaryOut,
    SupplierAPStatementOut,
)
from app.services import accounts_payable_service

router = APIRouter(prefix="/accounts-payable", tags=["accounts-payable"])

_view = require_permission("accounts_payable", "view")


def _org_id(user: User) -> str:
    if not user.organization_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No organization on this account",
        )
    return user.organization_id


@router.get("", response_model=APListResponse)
def list_accounts_payable(
    user: User = Depends(_view),
    supplier_id: str | None = Query(default=None),
    payment_status: str | None = Query(default=None),
    verification_status: str | None = Query(default=None),
    overdue_only: bool = Query(default=False),
    search: str | None = Query(default=None, description="matches supplier_invoice_number or supplier name"),
    page: int | None = Query(default=None, ge=1),
    page_size: int | None = Query(default=None, ge=1, le=100),
    db: Session = Depends(get_db),
) -> APListResponse:
    """GET /accounts-payable: List open payable invoices derived from recorded SupplierInvoices."""
    org_id = _org_id(user)
    return accounts_payable_service.get_accounts_payable_list(
        db,
        org_id,
        supplier_id=supplier_id,
        payment_status=payment_status,
        verification_status=verification_status,
        overdue_only=overdue_only,
        search=search,
        page=page,
        page_size=page_size,
    )


@router.get("/summary", response_model=dict)
def get_accounts_payable_summary(
    user: User = Depends(_view),
    db: Session = Depends(get_db),
) -> dict:
    """GET /accounts-payable/summary: Return total payable, overdue, due today, open count, and aging breakdown."""
    org_id = _org_id(user)
    ap_data = accounts_payable_service.get_accounts_payable_list(db, org_id)
    return {
        "summary": ap_data.summary,
        "ageing": ap_data.ageing,
    }


@router.get("/supplier/{supplier_id}", response_model=SupplierAPStatementOut)
def get_supplier_ap_statement(
    supplier_id: str,
    user: User = Depends(_view),
    db: Session = Depends(get_db),
) -> SupplierAPStatementOut:
    """GET /accounts-payable/supplier/{supplier_id}: Return supplier-specific AP statement."""
    org_id = _org_id(user)
    return accounts_payable_service.get_supplier_ap_statement(db, org_id, supplier_id)
