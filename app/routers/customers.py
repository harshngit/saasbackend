from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import require_permission, require_unlocked_org
from app.core.pdf_docs import payment_receipt_pdf
from app.models import Customer, CustomerPayment, Invoice, User
from app.services import numbering_service, lookup_service
from app.schemas.customer import (
    CustomerCreate,
    CustomerOut,
    CustomerPaymentCreate,
    CustomerPaymentOut,
    CustomerUpdate,
)

router = APIRouter(prefix="/customers", tags=["customers"])

# Permission gates (admin/super_admin bypass; staff checked against their role matrix).
_view = require_permission("customers", "view")
_create = require_permission("customers", "create")
_edit = require_permission("customers", "edit")
_delete = require_permission("customers", "delete")
_pay_view = require_permission("payments", "view")
_pay_create = require_permission("payments", "create")
_pay_delete = require_permission("payments", "delete")


def _org_id(user: User) -> str:
    if not user.organization_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No organization on this account")
    return user.organization_id


def _owned_customer(db: Session, customer_id: str, org_id: str) -> Customer:
    """Accepts the UUID or the human-facing code (customer_id)."""
    record = lookup_service.by_id_or_code(
        db, Customer, customer_id, org_id, Customer.customer_id
    )
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Customer not found")
    return record


def _owned_invoice(
    db: Session, invoice_id: str | None, customer: Customer, org_id: str
) -> Invoice | None:
    """The invoice a payment settles — it must belong to this firm and this customer."""
    if not invoice_id:
        return None
    invoice = db.get(Invoice, invoice_id)
    if invoice is None or invoice.organization_id != org_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Invoice not found")
    if invoice.customer_id != customer.id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="That invoice belongs to a different customer",
        )
    return invoice


def _apply_to_invoice(invoice: Invoice, amount: float) -> None:
    """Move an invoice's paid figure by `amount` (negative to void) and restate its
    status. Clamped at zero so voiding can never push it negative."""
    invoice.amount_paid = round(max((invoice.amount_paid or 0) + amount, 0), 2)
    if invoice.amount_paid <= 0:
        invoice.status = "unpaid"
    elif invoice.amount_paid + 0.01 >= (invoice.total or 0):
        invoice.status = "paid"
    else:
        invoice.status = "partial"


def _validate_assignee(db: Session, org_id: str, sales_officer_id: str | None) -> None:
    if sales_officer_id is None:
        return
    officer = db.get(User, sales_officer_id)
    if officer is None or officer.organization_id != org_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="assigned_sales_officer_id is not a user in your firm",
        )


@router.post("", response_model=CustomerOut, status_code=status.HTTP_201_CREATED)
def create_customer(
    payload: CustomerCreate,
    user: User = Depends(_create),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> Customer:
    org_id = _org_id(user)
    _validate_assignee(db, org_id, payload.assigned_sales_officer_id)
    data = payload.model_dump()
    # Sheet: Customer ID is an Auto Number, so it is issued here, not sent in.
    data["customer_id"] = numbering_service.next_number(
        db, org_id, Customer.customer_id, "CUST"
    )
    customer = Customer(organization_id=org_id, **data)
    customer.recompute_outstanding()  # opening_balance -> outstanding
    db.add(customer)
    db.commit()
    db.refresh(customer)
    return customer


@router.get("", response_model=list[CustomerOut])
def list_customers(
    user: User = Depends(_view),
    search: str | None = Query(default=None, description="matches customer code / name / business / phone / email"),
    category: str | None = Query(default=None),
    is_active: bool | None = Query(default=None),
    assigned_sales_officer_id: str | None = Query(default=None),
    db: Session = Depends(get_db),
) -> list[Customer]:
    org_id = _org_id(user)
    query = db.query(Customer).filter(Customer.organization_id == org_id)
    if search:
        like = f"%{search}%"
        query = query.filter(
            or_(
                Customer.name.ilike(like),
                Customer.business_name.ilike(like),
                Customer.phone.ilike(like),
                Customer.email.ilike(like),
                Customer.customer_id.ilike(like),
            )
        )
    if category is not None:
        query = query.filter(Customer.category == category)
    if is_active is not None:
        query = query.filter(Customer.is_active == is_active)
    if assigned_sales_officer_id is not None:
        query = query.filter(Customer.assigned_sales_officer_id == assigned_sales_officer_id)
    return query.order_by(Customer.created_at.desc()).all()


@router.get("/{customer_id}", response_model=CustomerOut)
def get_customer(customer_id: str, user: User = Depends(_view), db: Session = Depends(get_db)) -> Customer:
    return _owned_customer(db, customer_id, _org_id(user))


@router.patch("/{customer_id}", response_model=CustomerOut)
def update_customer(
    customer_id: str,
    payload: CustomerUpdate,
    user: User = Depends(_edit),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> Customer:
    org_id = _org_id(user)
    customer = _owned_customer(db, customer_id, org_id)
    data = payload.model_dump(exclude_unset=True)
    if "assigned_sales_officer_id" in data:
        _validate_assignee(db, org_id, data["assigned_sales_officer_id"])
    for field, value in data.items():
        setattr(customer, field, value)
    db.commit()
    db.refresh(customer)
    return customer


@router.post("/{customer_id}/payments", response_model=CustomerOut, status_code=status.HTTP_201_CREATED)
def record_customer_payment(
    customer_id: str,
    payload: CustomerPaymentCreate,
    user: User = Depends(_pay_create),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> Customer:
    """Record a payment received from a customer. Returns the customer with updated balances.

    Pass `invoice_id` to settle a specific invoice — that invoice's `amount_paid`
    and `status` move with it. Omit it and the payment is an advance that only
    reduces the customer's outstanding balance."""
    org_id = _org_id(user)
    customer = _owned_customer(db, customer_id, org_id)
    invoice = _owned_invoice(db, payload.invoice_id, customer, org_id)
    db.add(CustomerPayment(
        customer_id=customer.id, organization_id=org_id, order_id=payload.order_id,
        invoice_id=invoice.id if invoice else None,
        amount=payload.amount, payment_mode=payload.payment_mode, reference=payload.reference,
        note=payload.note, received_on=payload.received_on or datetime.now(timezone.utc),
    ))
    if invoice is not None:
        _apply_to_invoice(invoice, payload.amount)
    customer.total_received = round((customer.total_received or 0) + payload.amount, 2)
    customer.recompute_outstanding()
    db.commit()
    db.refresh(customer)
    return customer


@router.get("/{customer_id}/payments", response_model=list[CustomerPaymentOut])
def list_customer_payments(
    customer_id: str, user: User = Depends(_pay_view), db: Session = Depends(get_db)
) -> list[CustomerPayment]:
    _owned_customer(db, customer_id, _org_id(user))
    return (
        db.query(CustomerPayment)
        .filter(CustomerPayment.customer_id == customer_id)
        .order_by(CustomerPayment.received_on.desc())
        .all()
    )


@router.get("/{customer_id}/payments/receipt/{payment_id}")
def payment_receipt(
    customer_id: str,
    payment_id: str,
    user: User = Depends(_pay_view),
    db: Session = Depends(get_db),
) -> Response:
    """Download a PDF receipt for a customer payment."""
    org_id = _org_id(user)
    customer = _owned_customer(db, customer_id, org_id)
    payment = db.get(CustomerPayment, payment_id)
    if payment is None or payment.customer_id != customer_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Payment not found")
    pdf = payment_receipt_pdf(user.organization, customer, payment)
    return Response(
        content=pdf, media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="receipt-{payment_id[:8]}.pdf"'},
    )


@router.delete("/{customer_id}/payments/{payment_id}", response_model=CustomerOut)
def void_customer_payment(
    customer_id: str,
    payment_id: str,
    user: User = Depends(_pay_delete),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> Customer:
    org_id = _org_id(user)
    customer = _owned_customer(db, customer_id, org_id)
    payment = db.get(CustomerPayment, payment_id)
    if payment is None or payment.customer_id != customer_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Payment not found")
    # Undo whatever this payment did: the invoice it settled, or the advance balance.
    if payment.invoice_id:
        invoice = db.get(Invoice, payment.invoice_id)
        if invoice is not None:
            _apply_to_invoice(invoice, -payment.amount)
    customer.total_received = round((customer.total_received or 0) - payment.amount, 2)
    customer.recompute_outstanding()
    db.delete(payment)
    db.commit()
    db.refresh(customer)
    return customer


@router.delete("/{customer_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_customer(
    customer_id: str,
    user: User = Depends(_delete),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> None:
    customer = _owned_customer(db, customer_id, _org_id(user))
    db.delete(customer)
    db.commit()
