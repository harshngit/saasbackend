from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import require_permission, require_unlocked_org
from app.core.pdf_docs import payment_receipt_pdf
from app.models import Customer, CustomerPayment, User
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
    customer = db.get(Customer, customer_id)
    if customer is None or customer.organization_id != org_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Customer not found")
    return customer


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
    customer = Customer(organization_id=org_id, **payload.model_dump())
    customer.recompute_outstanding()  # opening_balance -> outstanding
    db.add(customer)
    db.commit()
    db.refresh(customer)
    return customer


@router.get("", response_model=list[CustomerOut])
def list_customers(
    user: User = Depends(_view),
    search: str | None = Query(default=None, description="matches name / business / phone / email"),
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
    """Record a payment received from a customer. Returns the customer with updated balances."""
    org_id = _org_id(user)
    customer = _owned_customer(db, customer_id, org_id)
    db.add(CustomerPayment(
        customer_id=customer.id, organization_id=org_id, order_id=payload.order_id,
        amount=payload.amount, payment_mode=payload.payment_mode, reference=payload.reference,
        note=payload.note, received_on=payload.received_on or datetime.now(timezone.utc),
    ))
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
