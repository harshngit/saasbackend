from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import require_permission, require_unlocked_org
from app.models import (
    PurchaseInvoice,
    Supplier,
    SupplierInvoice,
    SupplierInvoiceItem,
    User,
)
from app.schemas.supplier_invoice import (
    SupplierInvoiceCreate,
    SupplierInvoiceItemIn,
    SupplierInvoiceOut,
    SupplierInvoiceUpdate,
)
from app.services import lookup_service, purchase_service, supplier_invoice_service

router = APIRouter(prefix="/supplier-invoices", tags=["supplier-invoices"])

_view = require_permission("supplier_invoices", "view")
_create = require_permission("supplier_invoices", "create")
_edit = require_permission("supplier_invoices", "edit")
_approve = require_permission("supplier_invoices", "approve")
_delete = require_permission("supplier_invoices", "delete")


def _org_id(user: User) -> str:
    if not user.organization_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No organization on this account")
    return user.organization_id


def _owned(db: Session, id: str, org_id: str) -> SupplierInvoice:
    record = lookup_service.by_id_or_code(
        db,
        SupplierInvoice,
        id,
        org_id,
        SupplierInvoice.supplier_invoice_number,
    )
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Supplier invoice not found")
    return record


@router.post("", response_model=SupplierInvoiceOut, status_code=status.HTTP_201_CREATED)
def create_supplier_invoice(
    payload: SupplierInvoiceCreate,
    user: User = Depends(_create),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> SupplierInvoice:
    org_id = _org_id(user)

    # 1. Validate Purchase Order
    purchase = db.get(PurchaseInvoice, payload.purchase_id)
    if purchase is None or purchase.organization_id != org_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Purchase order not found in your firm")

    if purchase.status in ("draft", "pending"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Purchase order must be confirmed before creating a supplier invoice",
        )
    if purchase.status == "cancelled":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot create supplier invoice for a cancelled purchase order",
        )

    # 2. Validate Supplier
    supplier = purchase_service.validate_supplier(db, org_id, payload.supplier_id)
    if supplier.id != purchase.supplier_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invoice supplier does not match the purchase order supplier",
        )

    # 3. Check duplicate invoice number constraint per supplier + org
    dup = (
        db.query(SupplierInvoice)
        .filter(
            SupplierInvoice.organization_id == org_id,
            SupplierInvoice.supplier_id == supplier.id,
            SupplierInvoice.supplier_invoice_number == payload.supplier_invoice_number,
        )
        .first()
    )
    if dup is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Supplier invoice number '{payload.supplier_invoice_number}' already exists for this supplier",
        )

    # 4. Build line items and calculate server-side line totals
    built_items, subtotal, tax_amt, disc_amt, grand_total = (
        supplier_invoice_service.build_and_calculate_invoice_items(
            db, org_id, purchase, payload.items
        )
    )

    inv = SupplierInvoice(
        organization_id=org_id,
        supplier_id=supplier.id,
        purchase_id=purchase.id,
        supplier_invoice_number=payload.supplier_invoice_number,
        supplier_invoice_date=payload.supplier_invoice_date or datetime.now(timezone.utc),
        due_date=payload.due_date,
        status="draft",
        verification_status="pending",
        payment_status="unpaid",
        subtotal=subtotal,
        tax_amount=tax_amt + payload.tax_amount,
        discount_amount=disc_amt + payload.discount_amount,
        grand_total=round(grand_total + payload.tax_amount - payload.discount_amount, 2),
        amount_paid=0.0,
        notes=payload.notes,
        attachment_url=payload.attachment_url,
        created_by=user.id,
    )
    inv.items = built_items
    db.add(inv)
    db.commit()
    db.refresh(inv)
    return inv


@router.get("", response_model=list[SupplierInvoiceOut])
def list_supplier_invoices(
    user: User = Depends(_view),
    supplier_id: str | None = Query(default=None),
    purchase_id: str | None = Query(default=None),
    status_filter: str | None = Query(default=None, alias="status"),
    verification_status: str | None = Query(default=None),
    payment_status: str | None = Query(default=None),
    search: str | None = Query(default=None, description="matches supplier_invoice_number"),
    db: Session = Depends(get_db),
) -> list[SupplierInvoice]:
    org_id = _org_id(user)
    q = db.query(SupplierInvoice).filter(SupplierInvoice.organization_id == org_id)

    if supplier_id:
        q = q.filter(SupplierInvoice.supplier_id == supplier_id)
    if purchase_id:
        q = q.filter(SupplierInvoice.purchase_id == purchase_id)
    if status_filter:
        q = q.filter(SupplierInvoice.status == status_filter.lower())
    if verification_status:
        q = q.filter(SupplierInvoice.verification_status == verification_status.lower())
    if payment_status:
        q = q.filter(SupplierInvoice.payment_status == payment_status.lower())
    if search:
        s = f"%{search}%"
        q = q.filter(SupplierInvoice.supplier_invoice_number.ilike(s))

    return q.order_by(SupplierInvoice.created_at.desc()).all()


@router.get("/{id}", response_model=SupplierInvoiceOut)
def get_supplier_invoice(
    id: str, user: User = Depends(_view), db: Session = Depends(get_db)
) -> SupplierInvoice:
    return _owned(db, id, _org_id(user))


@router.put("/{id}", response_model=SupplierInvoiceOut)
def update_supplier_invoice(
    id: str,
    payload: SupplierInvoiceUpdate,
    user: User = Depends(_edit),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> SupplierInvoice:
    org_id = _org_id(user)
    inv = _owned(db, id, org_id)

    if inv.status != "draft":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Only draft supplier invoices can be edited (current status: '{inv.status}')",
        )

    data = payload.model_dump(exclude_unset=True)
    items_raw = data.pop("items", None)

    if "supplier_invoice_number" in data and data["supplier_invoice_number"] != inv.supplier_invoice_number:
        dup = (
            db.query(SupplierInvoice)
            .filter(
                SupplierInvoice.organization_id == org_id,
                SupplierInvoice.supplier_id == inv.supplier_id,
                SupplierInvoice.supplier_invoice_number == data["supplier_invoice_number"],
                SupplierInvoice.id != inv.id,
            )
            .first()
        )
        if dup is not None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Supplier invoice number '{data['supplier_invoice_number']}' already exists for this supplier",
            )

    for field, value in data.items():
        if value is not None:
            setattr(inv, field, value)

    if items_raw is not None:
        purchase = db.get(PurchaseInvoice, inv.purchase_id)
        built_items, subtotal, tax_amt, disc_amt, grand_total = (
            supplier_invoice_service.build_and_calculate_invoice_items(
                db, org_id, purchase, [SupplierInvoiceItemIn(**i) for i in items_raw]
            )
        )
        inv.items = built_items
        inv.subtotal = subtotal
        inv.tax_amount = tax_amt + (inv.tax_amount or 0.0)
        inv.discount_amount = disc_amt + (inv.discount_amount or 0.0)
        inv.grand_total = grand_total

    db.commit()
    db.refresh(inv)
    return inv


@router.post("/{id}/record", response_model=SupplierInvoiceOut)
def record_supplier_invoice_endpoint(
    id: str,
    user: User = Depends(_approve),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> SupplierInvoice:
    """Record Supplier Invoice: Atomically 3-way match, enforce over-invoicing protection, & lock invoice. ZERO stock movement."""
    org_id = _org_id(user)
    inv = _owned(db, id, org_id)
    return supplier_invoice_service.record_supplier_invoice(db, inv, org_id, user.id)


@router.post("/{id}/cancel", response_model=SupplierInvoiceOut)
def cancel_supplier_invoice(
    id: str,
    user: User = Depends(_approve),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> SupplierInvoice:
    """Cancel Supplier Invoice (frees recorded quantity). NO stock movement."""
    org_id = _org_id(user)
    inv = _owned(db, id, org_id)
    inv.status = "cancelled"
    db.commit()
    db.refresh(inv)
    return inv


@router.delete("/{id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_supplier_invoice(
    id: str,
    user: User = Depends(_delete),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> None:
    org_id = _org_id(user)
    inv = _owned(db, id, org_id)

    if inv.status == "recorded":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot delete a recorded supplier invoice. Recorded invoices are preserved for financial audit trails.",
        )
    if inv.status == "cancelled":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot delete a cancelled supplier invoice. Cancelled invoices are preserved for audit trails.",
        )

    db.delete(inv)
    db.commit()
