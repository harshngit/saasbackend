from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import require_permission, require_unlocked_org
from app.models import (
    PAYMENT_STATUSES,
    Product,
    ProductVariant,
    PurchaseInvoice,
    PurchaseInvoiceItem,
    StockMovement,
    Supplier,
    User,
)
from app.schemas.purchase import (
    CancelBody,
    PaymentStatusUpdate,
    PurchaseCreate,
    PurchaseItemIn,
    PurchaseOut,
    PurchaseUpdate,
)

router = APIRouter(prefix="/purchases", tags=["purchases"])

_view = require_permission("purchases", "view")
_create = require_permission("purchases", "create")
_edit = require_permission("purchases", "edit")
_approve = require_permission("purchases", "approve")


def _org_id(user: User) -> str:
    if not user.organization_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No organization on this account")
    return user.organization_id


def _owned(db: Session, invoice_id: str, org_id: str) -> PurchaseInvoice:
    inv = db.get(PurchaseInvoice, invoice_id)
    if inv is None or inv.organization_id != org_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Purchase invoice not found")
    return inv


def _build_items(db: Session, org_id: str, items: list[PurchaseItemIn]) -> tuple[list[PurchaseInvoiceItem], float]:
    built, subtotal = [], 0.0
    for it in items:
        product = db.get(Product, it.product_id)
        if product is None or product.organization_id != org_id:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="An item's product is not in your firm")
        variant = None
        if it.variant_id:
            variant = db.get(ProductVariant, it.variant_id)
            if variant is None or variant.product_id != product.id:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="An item's variant is invalid")
        line_total = round(it.purchase_price * it.quantity - it.discount + it.tax, 2)
        subtotal += round(it.purchase_price * it.quantity - it.discount, 2)
        built.append(PurchaseInvoiceItem(
            product_id=product.id, variant_id=it.variant_id,
            product_name=product.name if not variant else f"{product.name} ({variant.name})",
            quantity=it.quantity, purchase_price=it.purchase_price, discount=it.discount, tax=it.tax,
            line_total=line_total,
        ))
    return built, round(subtotal, 2)


def _recompute_totals(inv: PurchaseInvoice) -> None:
    inv.subtotal = round(sum(round(i.purchase_price * i.quantity - i.discount, 2) for i in inv.items), 2)
    inv.total = round(inv.subtotal - inv.discount + inv.tax, 2)


@router.post("", response_model=PurchaseOut, status_code=status.HTTP_201_CREATED)
def create_purchase(
    payload: PurchaseCreate,
    user: User = Depends(_create),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> PurchaseInvoice:
    org_id = _org_id(user)
    supplier = db.get(Supplier, payload.supplier_id)
    if supplier is None or supplier.organization_id != org_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="supplier_id is not a supplier in your firm")
    inv = PurchaseInvoice(
        organization_id=org_id, invoice_number=payload.invoice_number, supplier_id=supplier.id,
        invoice_date=payload.invoice_date or datetime.now(timezone.utc),
        status="pending", discount=payload.discount, tax=payload.tax,
        notes=payload.notes, attachment_url=payload.attachment_url, created_by=user.id,
    )
    inv.items, _ = _build_items(db, org_id, payload.items)
    _recompute_totals(inv)
    db.add(inv)
    db.commit()
    db.refresh(inv)
    return inv


@router.get("", response_model=list[PurchaseOut])
def list_purchases(
    user: User = Depends(_view),
    supplier_id: str | None = Query(default=None),
    status_filter: str | None = Query(default=None, alias="status"),
    payment_status: str | None = Query(default=None),
    search: str | None = Query(default=None, description="matches invoice_number"),
    db: Session = Depends(get_db),
) -> list[PurchaseInvoice]:
    org_id = _org_id(user)
    q = db.query(PurchaseInvoice).filter(PurchaseInvoice.organization_id == org_id)
    if supplier_id:
        q = q.filter(PurchaseInvoice.supplier_id == supplier_id)
    if status_filter:
        q = q.filter(PurchaseInvoice.status == status_filter)
    if payment_status:
        q = q.filter(PurchaseInvoice.payment_status == payment_status)
    if search:
        q = q.filter(PurchaseInvoice.invoice_number.ilike(f"%{search}%"))
    return q.order_by(PurchaseInvoice.created_at.desc()).all()


@router.get("/{invoice_id}", response_model=PurchaseOut)
def get_purchase(invoice_id: str, user: User = Depends(_view), db: Session = Depends(get_db)) -> PurchaseInvoice:
    return _owned(db, invoice_id, _org_id(user))


@router.patch("/{invoice_id}", response_model=PurchaseOut)
def update_purchase(
    invoice_id: str,
    payload: PurchaseUpdate,
    user: User = Depends(_edit),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> PurchaseInvoice:
    org_id = _org_id(user)
    inv = _owned(db, invoice_id, org_id)
    if inv.status != "pending":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Only pending invoices can be edited")
    data = payload.model_dump(exclude_unset=True)
    items = data.pop("items", None)
    for field, value in data.items():
        setattr(inv, field, value)
    if items is not None:
        inv.items, _ = _build_items(db, org_id, [PurchaseItemIn(**i) for i in items])
    _recompute_totals(inv)
    db.commit()
    db.refresh(inv)
    return inv


@router.patch("/{invoice_id}/approve", response_model=PurchaseOut)
def approve_purchase(
    invoice_id: str,
    user: User = Depends(_approve),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> PurchaseInvoice:
    """Approve → add stock (purchase_in) and increase the supplier's total_purchases."""
    org_id = _org_id(user)
    inv = _owned(db, invoice_id, org_id)
    if inv.status != "pending":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Only pending invoices can be approved (this is '{inv.status}')")
    for item in inv.items:
        if item.variant_id:
            variant = db.get(ProductVariant, item.variant_id)
            if variant:
                variant.inventory = (variant.inventory or 0) + item.quantity
                bal = variant.inventory
            else:
                continue
        else:
            product = db.get(Product, item.product_id) if item.product_id else None
            if product is None:
                continue
            product.total_inventory = (product.total_inventory or 0) + item.quantity
            bal = product.total_inventory
        db.add(StockMovement(
            organization_id=org_id, product_id=item.product_id, variant_id=item.variant_id,
            movement_type="purchase_in", quantity=item.quantity, balance_after=bal,
            note=f"Purchase {inv.invoice_number}", created_by=user.id,
        ))
    if inv.supplier_id:
        supplier = db.get(Supplier, inv.supplier_id)
        if supplier:
            supplier.total_purchases = round((supplier.total_purchases or 0) + inv.total, 2)
    inv.status = "approved"
    inv.stock_added = True
    db.commit()
    db.refresh(inv)
    return inv


@router.patch("/{invoice_id}/payment-status", response_model=PurchaseOut)
def set_payment_status(
    invoice_id: str,
    payload: PaymentStatusUpdate,
    user: User = Depends(_edit),
    db: Session = Depends(get_db),
) -> PurchaseInvoice:
    inv = _owned(db, invoice_id, _org_id(user))
    if payload.payment_status not in PAYMENT_STATUSES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"payment_status must be one of {sorted(PAYMENT_STATUSES)}")
    inv.payment_status = payload.payment_status
    if payload.amount_paid is not None:
        inv.amount_paid = payload.amount_paid
    db.commit()
    db.refresh(inv)
    return inv


@router.patch("/{invoice_id}/cancel", response_model=PurchaseOut)
def cancel_purchase(
    invoice_id: str,
    payload: CancelBody,
    user: User = Depends(_approve),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> PurchaseInvoice:
    """Cancel. If approved, reverse the stock-in and the supplier's total_purchases."""
    org_id = _org_id(user)
    inv = _owned(db, invoice_id, org_id)
    if inv.status == "cancelled":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Already cancelled")
    if inv.stock_added:
        for item in inv.items:
            if item.variant_id:
                variant = db.get(ProductVariant, item.variant_id)
                if variant is None:
                    continue
                new_bal = (variant.inventory or 0) - item.quantity
                if new_bal < 0:
                    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Cannot reverse: stock already consumed for {item.product_name}")
                variant.inventory = new_bal
                bal = new_bal
            else:
                product = db.get(Product, item.product_id) if item.product_id else None
                if product is None:
                    continue
                new_bal = (product.total_inventory or 0) - item.quantity
                if new_bal < 0:
                    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Cannot reverse: stock already consumed for {item.product_name}")
                product.total_inventory = new_bal
                bal = new_bal
            db.add(StockMovement(
                organization_id=org_id, product_id=item.product_id, variant_id=item.variant_id,
                movement_type="purchase_return", quantity=-item.quantity, balance_after=bal,
                note=f"Cancel purchase {inv.invoice_number}", created_by=user.id,
            ))
        if inv.supplier_id:
            supplier = db.get(Supplier, inv.supplier_id)
            if supplier:
                supplier.total_purchases = round((supplier.total_purchases or 0) - inv.total, 2)
        inv.stock_added = False
    inv.status = "cancelled"
    db.commit()
    db.refresh(inv)
    return inv


@router.delete("/{invoice_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_purchase(
    invoice_id: str,
    user: User = Depends(require_permission("purchases", "delete")),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> None:
    inv = _owned(db, invoice_id, _org_id(user))
    if inv.status == "approved":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Cancel an approved invoice before deleting")
    db.delete(inv)
    db.commit()
