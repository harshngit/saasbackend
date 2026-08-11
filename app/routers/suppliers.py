from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import require_permission, require_unlocked_org
from app.models import Supplier, SupplierPayment, User
from app.schemas.supplier import (
    PaymentCreate,
    PaymentOut,
    SupplierCreate,
    SupplierOut,
    SupplierStatusUpdate,
    SupplierUpdate,
)

router = APIRouter(prefix="/suppliers", tags=["suppliers"])

# Supplier records are gated by the `suppliers` module; payments by the `payments` module.
_view = require_permission("suppliers", "view")
_create = require_permission("suppliers", "create")
_edit = require_permission("suppliers", "edit")
_delete = require_permission("suppliers", "delete")
_pay_view = require_permission("payments", "view")
_pay_create = require_permission("payments", "create")
_pay_delete = require_permission("payments", "delete")


def _org_id(user: User) -> str:
    if not user.organization_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No organization on this account")
    return user.organization_id


def _owned(db: Session, supplier_id: str, org_id: str) -> Supplier:
    s = db.get(Supplier, supplier_id)
    if s is None or s.organization_id != org_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Supplier not found")
    return s


# --------------------------------- Suppliers CRUD ---------------------------------


@router.post("", response_model=SupplierOut, status_code=status.HTTP_201_CREATED)
def create_supplier(
    payload: SupplierCreate,
    user: User = Depends(_create),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> Supplier:
    supplier = Supplier(organization_id=_org_id(user), **payload.model_dump())
    db.add(supplier)
    db.commit()
    db.refresh(supplier)
    return supplier


@router.get("", response_model=list[SupplierOut])
def list_suppliers(
    user: User = Depends(_view),
    search: str | None = Query(default=None, description="matches name / contact / phone / email"),
    category: str | None = Query(default=None),
    is_active: bool | None = Query(default=None),
    db: Session = Depends(get_db),
) -> list[Supplier]:
    org_id = _org_id(user)
    query = db.query(Supplier).filter(Supplier.organization_id == org_id)
    if search:
        like = f"%{search}%"
        query = query.filter(
            or_(Supplier.name.ilike(like), Supplier.contact_person.ilike(like),
                Supplier.phone.ilike(like), Supplier.email.ilike(like))
        )
    if category is not None:
        query = query.filter(Supplier.category == category)
    if is_active is not None:
        query = query.filter(Supplier.is_active == is_active)
    return query.order_by(Supplier.name).all()


@router.get("/{supplier_id}", response_model=SupplierOut)
def get_supplier(supplier_id: str, user: User = Depends(_view), db: Session = Depends(get_db)) -> Supplier:
    return _owned(db, supplier_id, _org_id(user))


@router.put("/{supplier_id}", response_model=SupplierOut)
def update_supplier(
    supplier_id: str,
    payload: SupplierUpdate,
    user: User = Depends(_edit),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> Supplier:
    supplier = _owned(db, supplier_id, _org_id(user))
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(supplier, field, value)
    db.commit()
    db.refresh(supplier)
    return supplier


@router.patch("/{supplier_id}/status", response_model=SupplierOut)
def set_supplier_status(
    supplier_id: str,
    payload: SupplierStatusUpdate,
    user: User = Depends(_edit),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> Supplier:
    supplier = _owned(db, supplier_id, _org_id(user))
    supplier.is_active = payload.is_active
    db.commit()
    db.refresh(supplier)
    return supplier


@router.delete("/{supplier_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_supplier(
    supplier_id: str,
    user: User = Depends(_delete),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> None:
    supplier = _owned(db, supplier_id, _org_id(user))
    db.delete(supplier)  # payments cascade
    db.commit()


# ------------------------------- Supplier payments -------------------------------


@router.post("/{supplier_id}/payments", response_model=SupplierOut, status_code=status.HTTP_201_CREATED)
def record_payment(
    supplier_id: str,
    payload: PaymentCreate,
    user: User = Depends(_pay_create),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> Supplier:
    """Record a payment to the supplier. Returns the supplier with updated balances."""
    org_id = _org_id(user)
    supplier = _owned(db, supplier_id, org_id)
    payment = SupplierPayment(
        supplier_id=supplier.id,
        organization_id=org_id,
        amount=payload.amount,
        payment_mode=payload.payment_mode,
        reference=payload.reference,
        note=payload.note,
        paid_on=payload.paid_on or datetime.now(timezone.utc),
    )
    db.add(payment)
    supplier.total_paid = round((supplier.total_paid or 0) + payload.amount, 2)  # keep balance in sync
    db.commit()
    db.refresh(supplier)
    return supplier


@router.get("/{supplier_id}/payments", response_model=list[PaymentOut])
def list_payments(
    supplier_id: str,
    user: User = Depends(_pay_view),
    db: Session = Depends(get_db),
) -> list[SupplierPayment]:
    org_id = _org_id(user)
    _owned(db, supplier_id, org_id)
    return (
        db.query(SupplierPayment)
        .filter(SupplierPayment.supplier_id == supplier_id)
        .order_by(SupplierPayment.paid_on.desc(), SupplierPayment.id.desc())
        .all()
    )


@router.delete("/{supplier_id}/payments/{payment_id}", response_model=SupplierOut)
def void_payment(
    supplier_id: str,
    payment_id: str,
    user: User = Depends(_pay_delete),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> Supplier:
    """Void (delete) a payment and restore the supplier's outstanding balance."""
    org_id = _org_id(user)
    supplier = _owned(db, supplier_id, org_id)
    payment = db.get(SupplierPayment, payment_id)
    if payment is None or payment.supplier_id != supplier_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Payment not found")
    supplier.total_paid = round((supplier.total_paid or 0) - payment.amount, 2)  # reverse the payment
    db.delete(payment)
    db.commit()
    db.refresh(supplier)
    return supplier
