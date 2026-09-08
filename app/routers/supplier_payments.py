from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import require_permission, require_unlocked_org
from app.models import User
from app.schemas.supplier_payment import (
    InvoicePaymentAllocationOut,
    SupplierPaymentCreate,
    SupplierPaymentListResponse,
    SupplierPaymentOut,
    SupplierPaymentVoidIn,
)
from app.services import supplier_payment_service

router = APIRouter(prefix="/supplier-payments", tags=["supplier-payments"])

_view = require_permission("supplier_payments", "view")
_create = require_permission("supplier_payments", "create")
_approve = require_permission("supplier_payments", "approve")


def _org_id(user: User) -> str:
    if not user.organization_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No organization on this account",
        )
    return user.organization_id


@router.post("", response_model=SupplierPaymentOut, status_code=status.HTTP_201_CREATED)
def record_supplier_payment(
    payload: SupplierPaymentCreate,
    user: User = Depends(_create),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> SupplierPaymentOut:
    """POST /supplier-payments: Record a new Supplier Payment with optional invoice allocations."""
    org_id = _org_id(user)
    return supplier_payment_service.record_supplier_payment(
        db, org_id, payload, user_id=user.id
    )


@router.get("", response_model=SupplierPaymentListResponse)
def list_supplier_payments(
    user: User = Depends(_view),
    supplier_id: str | None = Query(default=None),
    status: str | None = Query(default=None, description="recorded or voided"),
    payment_method: str | None = Query(default=None),
    date_from: datetime | None = Query(default=None),
    date_to: datetime | None = Query(default=None),
    search: str | None = Query(default=None, description="matches payment_number, reference, or supplier name"),
    page: int | None = Query(default=None, ge=1),
    page_size: int | None = Query(default=None, ge=1, le=100),
    db: Session = Depends(get_db),
) -> SupplierPaymentListResponse:
    """GET /supplier-payments: List supplier payments with filtering and pagination."""
    org_id = _org_id(user)
    return supplier_payment_service.list_supplier_payments(
        db,
        org_id,
        supplier_id=supplier_id,
        payment_status=status,
        payment_method=payment_method,
        date_from=date_from,
        date_to=date_to,
        search=search,
        page=page,
        page_size=page_size,
    )


@router.get("/{id}", response_model=SupplierPaymentOut)
def get_supplier_payment(
    id: str,
    user: User = Depends(_view),
    db: Session = Depends(get_db),
) -> SupplierPaymentOut:
    """GET /supplier-payments/{id}: Get detailed supplier payment record with allocations."""
    org_id = _org_id(user)
    return supplier_payment_service.get_supplier_payment_detail(db, org_id, id)


@router.post("/{id}/void", response_model=SupplierPaymentOut)
def void_supplier_payment(
    id: str,
    payload: SupplierPaymentVoidIn,
    user: User = Depends(_approve),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> SupplierPaymentOut:
    """POST /supplier-payments/{id}/void: Void a payment and reverse invoice allocations atomically."""
    org_id = _org_id(user)
    return supplier_payment_service.void_supplier_payment(
        db, org_id, id, reason=payload.reason, user_id=user.id
    )


@router.get("/invoices/{invoice_id}/allocations", response_model=list[InvoicePaymentAllocationOut])
def get_invoice_payment_allocations(
    invoice_id: str,
    user: User = Depends(_view),
    db: Session = Depends(get_db),
) -> list[InvoicePaymentAllocationOut]:
    """GET /supplier-payments/invoices/{invoice_id}/allocations: List payment allocations for a specific Supplier Invoice."""
    org_id = _org_id(user)
    return supplier_payment_service.get_invoice_payment_history(db, org_id, invoice_id)
