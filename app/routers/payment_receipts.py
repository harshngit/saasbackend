from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import require_permission, require_unlocked_org
from app.models import Customer, Invoice, PaymentReceipt, User
from app.schemas.payment_receipt import PaymentReceiptCreate, PaymentReceiptOut

router = APIRouter(prefix="/payment-receipts", tags=["payment_receipts"])

_view = require_permission("payment_receipts", "view")
_create = require_permission("payment_receipts", "create")


def _org_id(user: User) -> str:
    if not user.organization_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No organization on this account")
    return user.organization_id


@router.post("", response_model=PaymentReceiptOut, status_code=status.HTTP_201_CREATED)
def create_receipt(
    payload: PaymentReceiptCreate,
    user: User = Depends(_create),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> PaymentReceipt:
    org_id = _org_id(user)

    # Validate customer
    customer = db.get(Customer, payload.customer_id)
    if customer is None or customer.organization_id != org_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid customer_id")

    # Validate invoice reference if provided
    if payload.invoice_reference_id:
        invoice = db.get(Invoice, payload.invoice_reference_id)
        if invoice is None or invoice.organization_id != org_id:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid invoice_reference_id")

    receipt = PaymentReceipt(
        organization_id=org_id,
        receipt_number=payload.receipt_number,
        receipt_date=payload.receipt_date or datetime.now(timezone.utc),
        customer_id=payload.customer_id,
        invoice_reference_id=payload.invoice_reference_id,
        amount_received=payload.amount_received,
        payment_method=payload.payment_method,
        payment_status=payload.payment_status or "completed",
    )

    db.add(receipt)
    db.commit()
    db.refresh(receipt)
    return receipt


@router.get("", response_model=list[PaymentReceiptOut])
def list_receipts(
    user: User = Depends(_view),
    db: Session = Depends(get_db),
) -> list[PaymentReceipt]:
    return (
        db.query(PaymentReceipt)
        .filter(PaymentReceipt.organization_id == _org_id(user))
        .order_by(PaymentReceipt.receipt_date.desc())
        .all()
    )


@router.get("/{id}", response_model=PaymentReceiptOut)
def get_receipt_detail(
    id: str,
    user: User = Depends(_view),
    db: Session = Depends(get_db),
) -> PaymentReceipt:
    receipt = db.get(PaymentReceipt, id)
    if receipt is None or receipt.organization_id != _org_id(user):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Payment receipt not found")
    return receipt
