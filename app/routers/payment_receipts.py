"""Payment receipts — money received from a customer, and the receipt for it.

A receipt is not a second kind of payment: it is the document over the one
`customer_payments` row every payment surface writes. So a receipt taken here, a
payment recorded on the customer, and cash taken with a quick bill all move the same
invoice, the same balance and the same ledger.
"""

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import require_permission, require_unlocked_org
from app.models import Customer, CustomerPayment, Invoice, User
from app.schemas.payment_receipt import (
    PaymentReceiptCreate,
    PaymentReceiptOut,
    PaymentReceiptUpdate,
)
from app.services import payment_service

router = APIRouter(prefix="/payment-receipts", tags=["payment_receipts"])

_view = require_permission("payment_receipts", "view")
_create = require_permission("payment_receipts", "create")
_edit = require_permission("payment_receipts", "edit")
_delete = require_permission("payment_receipts", "delete")


def _org_id(user: User) -> str:
    if not user.organization_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="No organization on this account"
        )
    return user.organization_id


def _owned(db: Session, receipt_id: str, org_id: str) -> CustomerPayment:
    receipt = db.get(CustomerPayment, receipt_id)
    if receipt is None or receipt.organization_id != org_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Payment receipt not found")
    return receipt


def _out(db: Session, payment: CustomerPayment) -> PaymentReceiptOut:
    """One receipt, with where its invoice stands afterwards."""
    invoice = db.get(Invoice, payment.invoice_id) if payment.invoice_id else None
    customer = db.get(Customer, payment.customer_id) if payment.customer_id else None
    return PaymentReceiptOut(
        id=payment.id,
        organization_id=payment.organization_id,
        receipt_number=payment.receipt_number,
        receipt_date=payment.received_on,
        customer_id=payment.customer_id,
        invoice_reference_id=payment.invoice_id,
        invoice_id=payment.invoice_id,
        amount_received=payment.amount,
        amount_collected=payment.amount,
        payment_method=payment.payment_mode,
        transaction_reference=payment.reference,
        note=payment.note,
        upi_id=payment.upi_id,
        card_type=payment.card_type,
        card_last_four=payment.card_last_four,
        collection_instructions=payment.collection_instructions,
        invoice_total=invoice.total if invoice is not None else None,
        total_paid=invoice.amount_paid if invoice is not None else None,
        outstanding_amount=payment_service.outstanding(invoice) if invoice is not None else None,
        payment_status=payment_service.payment_status(invoice) if invoice is not None else None,
        order_amount=payment.order_amount,
        previous_pending=payment.previous_pending,
        remaining_receivable=payment.remaining_receivable,
        created_at=payment.created_at,
        customer=customer,
        invoice=invoice,
    )


@router.post("", response_model=PaymentReceiptOut, status_code=status.HTTP_201_CREATED)
def create_receipt(
    payload: PaymentReceiptCreate,
    user: User = Depends(_create),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> PaymentReceiptOut:
    """Record a payment and issue its receipt.

    Send `invoice_reference_id` and the customer comes from the invoice — the app never
    has to send who paid twice. In one transaction this moves the invoice's paid
    figure and status, the customer's received and outstanding totals, and the ledger.

    Paying more than the invoice still owes is refused. Money genuinely held on account
    is an advance: send `customer_id` with no invoice.
    """
    org_id = _org_id(user)

    invoice = None
    if payload.invoice_reference_id:
        invoice = db.get(Invoice, payload.invoice_reference_id)
        if invoice is None or invoice.organization_id != org_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="invoice_reference_id is not an invoice in your firm",
            )

    # The customer is whoever the invoice was raised for. An explicit customer_id is
    # only for an advance — and if both arrive they have to agree.
    customer = None
    if invoice is not None and invoice.customer_id:
        customer = db.get(Customer, invoice.customer_id)
        if payload.customer_id and payload.customer_id != customer.id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="That invoice was raised for a different customer",
            )
    elif payload.customer_id:
        customer = db.get(Customer, payload.customer_id)
        if customer is None or customer.organization_id != org_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="customer_id is not a customer in your firm",
            )

    if customer is None and invoice is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Send invoice_reference_id, or customer_id for an advance payment",
        )

    if payload.receipt_number:
        taken = (
            db.query(CustomerPayment)
            .filter(
                CustomerPayment.organization_id == org_id,
                CustomerPayment.receipt_number == payload.receipt_number,
            )
            .first()
        )
        if taken is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="A receipt with this number already exists",
            )

    try:
        payment = payment_service.record(
            db, org_id,
            customer=customer,
            invoice=invoice,
            amount=payload.amount_received,
            payment_mode=payload.payment_method,
            reference=payload.transaction_reference,
            note=payload.note,
            received_on=payload.receipt_date,
            receipt_number=payload.receipt_number,
            upi_id=payload.upi_id,
            card_type=payload.card_type,
            card_last_four=payload.card_last_four,
            collection_instructions=payload.collection_instructions,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    db.commit()
    db.refresh(payment)
    return _out(db, payment)


@router.get("", response_model=list[PaymentReceiptOut])
def list_receipts(
    user: User = Depends(_view),
    customer_id: str | None = Query(default=None),
    invoice_id: str | None = Query(default=None, description="Receipts against one invoice"),
    db: Session = Depends(get_db),
) -> list[PaymentReceiptOut]:
    """Every payment this firm has received, newest first."""
    org_id = _org_id(user)
    query = db.query(CustomerPayment).filter(CustomerPayment.organization_id == org_id)
    if customer_id:
        query = query.filter(CustomerPayment.customer_id == customer_id)
    if invoice_id:
        query = query.filter(CustomerPayment.invoice_id == invoice_id)
    rows = query.order_by(
        CustomerPayment.received_on.desc(), CustomerPayment.id.desc()
    ).all()
    return [_out(db, row) for row in rows]


@router.get("/{id}", response_model=PaymentReceiptOut)
def get_receipt_detail(
    id: str, user: User = Depends(_view), db: Session = Depends(get_db)
) -> PaymentReceiptOut:
    return _out(db, _owned(db, id, _org_id(user)))


@router.patch("/{id}", response_model=PaymentReceiptOut)
def update_receipt(
    id: str,
    payload: PaymentReceiptUpdate,
    user: User = Depends(_edit),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> PaymentReceiptOut:
    """Correct a receipt's details — its method, reference, date or number.

    The amount is deliberately not editable: void the receipt and record it again, so
    the invoice and the customer's balance are restated together rather than drifting
    from a silent edit.
    """
    org_id = _org_id(user)
    receipt = _owned(db, id, org_id)
    data = payload.model_dump(exclude_unset=True)

    if data.get("receipt_number") and data["receipt_number"] != receipt.receipt_number:
        taken = (
            db.query(CustomerPayment)
            .filter(
                CustomerPayment.organization_id == org_id,
                CustomerPayment.receipt_number == data["receipt_number"],
                CustomerPayment.id != receipt.id,
            )
            .first()
        )
        if taken is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="A receipt with this number already exists",
            )

    columns = {
        "payment_method": "payment_mode",
        "transaction_reference": "reference",
        "receipt_date": "received_on",
        "receipt_number": "receipt_number",
        "note": "note",
        "upi_id": "upi_id",
        "card_type": "card_type",
        "card_last_four": "card_last_four",
        "collection_instructions": "collection_instructions",
    }
    for field, column in columns.items():
        if field in data:
            value = data[field]
            if column == "received_on" and value is None:
                continue  # a receipt always has a date
            setattr(receipt, column, value)
    db.commit()
    db.refresh(receipt)
    return _out(db, receipt)


@router.delete("/{id}", status_code=status.HTTP_204_NO_CONTENT)
def void_receipt(
    id: str,
    user: User = Depends(_delete),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> None:
    """Void a receipt — the money is taken back off the invoice and the customer's
    balance, leaving both exactly as they were before it."""
    receipt = _owned(db, id, _org_id(user))
    payment_service.void(db, receipt)
    db.commit()
