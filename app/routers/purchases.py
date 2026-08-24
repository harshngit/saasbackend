from datetime import datetime, timezone

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile, status
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import require_permission, require_unlocked_org
from app.core.files import save_upload
from app.models import (
    PAYMENT_STATUSES,
    Product,
    ProductVariant,
    PurchaseInvoice,
    PurchaseInvoiceItem,
    StockMovement,
    Supplier,
    User,
    Warehouse,
)
from app.services import numbering_service, lookup_service, purchase_service
from app.schemas.purchase import (
    CancelBody,
    PaymentStatusUpdate,
    PurchaseCreate,
    PurchaseItemIn,
    PurchaseOut,
    PurchaseReturnBody,
    PurchaseUpdate,
)

router = APIRouter(prefix="", tags=["purchases"])

_view = require_permission("purchases", "view")
_create = require_permission("purchases", "create")
_edit = require_permission("purchases", "edit")
_approve = require_permission("purchases", "approve")


def _org_id(user: User) -> str:
    if not user.organization_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No organization on this account")
    return user.organization_id


def _owned(db: Session, id: str, org_id: str) -> PurchaseInvoice:
    """Accepts UUID or human-facing codes (purchase_number, purchase_id, invoice_number, grn_number)."""
    record = lookup_service.by_id_or_code(
        db,
        PurchaseInvoice,
        id,
        org_id,
        PurchaseInvoice.purchase_number,
        PurchaseInvoice.purchase_id,
        PurchaseInvoice.invoice_number,
        PurchaseInvoice.grn_number,
    )
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Purchase invoice not found")
    return record


@router.post("", response_model=PurchaseOut, status_code=status.HTTP_201_CREATED)
def create_purchase(
    payload: PurchaseCreate,
    user: User = Depends(_create),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> PurchaseInvoice:
    org_id = _org_id(user)
    supplier = purchase_service.validate_supplier(db, org_id, payload.supplier_id)
    if payload.warehouse_id:
        purchase_service.validate_warehouse(db, org_id, payload.warehouse_id)

    # Build items and calculate line math server-side
    built_items, subtotal, item_discounts, item_taxes = purchase_service.build_and_calculate_items(
        db, org_id, payload.items
    )

    # Calculate overall totals with additional charges and round off
    net_subtotal, effective_discount, effective_tax, grand_total = purchase_service.calculate_header_totals(
        subtotal=subtotal,
        item_discounts=item_discounts,
        item_taxes=item_taxes,
        overall_discount=payload.discount,
        header_tax=payload.tax,
        freight_charges=payload.freight_charges,
        packing_charges=payload.packing_charges,
        insurance_charges=payload.insurance_charges,
        other_charges=payload.other_charges,
        round_off=payload.round_off,
    )

    # Auto-populate supplier contact fields if not manually provided
    contact_person = payload.contact_person or (supplier.contact_person if supplier else None)
    mobile_number = payload.mobile_number or (supplier.phone if supplier else None)
    email_address = payload.email_address or (supplier.email if supplier else None)
    payee_gstin = payload.payee_gstin or (supplier.gst_number if supplier else None)
    billing_address = payload.billing_address or (supplier.address if supplier else None)

    # Determine payment status if amount_paid passed
    amt_paid = round(payload.amount_paid or 0.0, 2)
    if amt_paid >= grand_total and grand_total > 0:
        pay_status = "paid"
    elif amt_paid > 0:
        pay_status = "partial"
    else:
        pay_status = "unpaid"

    inv = PurchaseInvoice(
        organization_id=org_id,
        invoice_number=payload.invoice_number,
        supplier_id=supplier.id if supplier else None,
        invoice_date=payload.invoice_date or datetime.now(timezone.utc),
        status="pending",
        payment_status=pay_status,
        subtotal=net_subtotal,
        discount=effective_discount,
        tax=effective_tax,
        total=grand_total,
        amount_paid=amt_paid,
        notes=payload.notes,
        attachment_url=payload.attachment_url,
        created_by=user.id,
        # 1. Basic Information
        purchase_id=numbering_service.next_number(db, org_id, PurchaseInvoice.purchase_id, "PURID"),
        purchase_number=numbering_service.next_number(db, org_id, PurchaseInvoice.purchase_number, "PUR"),
        purchase_type=payload.purchase_type or "Direct Purchase",
        purchase_date=payload.purchase_date or payload.invoice_date or datetime.now(timezone.utc),
        financial_year=payload.financial_year,
        purchase_status=payload.purchase_status or "Draft",
        reference_number=payload.reference_number or payload.invoice_number,
        # 2. Supplier Details
        contact_person=contact_person,
        mobile_number=mobile_number,
        email_address=email_address,
        payee_gstin=payee_gstin,
        billing_address=billing_address,
        shipping_address=payload.shipping_address,
        # 4. Totals (Additional charges)
        freight_charges=payload.freight_charges,
        packing_charges=payload.packing_charges,
        insurance_charges=payload.insurance_charges,
        other_charges=payload.other_charges,
        round_off=payload.round_off,
        # 5. Goods Receipt
        grn_number=payload.grn_number or numbering_service.next_number(db, org_id, PurchaseInvoice.grn_number, "GRN"),
        received_date=payload.received_date,
        warehouse_id=payload.warehouse_id,
        received_by=payload.received_by,
        receiving_status=payload.receiving_status or "Pending",
        # 6. Payment Details
        payment_method=payload.payment_method,
        payment_terms=payload.payment_terms,
        due_date=payload.due_date,
        payment_reference=payload.payment_reference,
        # 7. Accounting
        purchase_account_id=payload.purchase_account_id,
        tax_category=payload.tax_category,
        cost_center_id=payload.cost_center_id,
        project_id=payload.project_id,
        # 8. Approval
        requested_by=payload.requested_by or user.id,
        approval_status=payload.approval_status or "Pending",
        # 9. Documents
        supplier_quotation_url=payload.supplier_quotation_url,
        purchase_order_url=payload.purchase_order_url,
        supplier_invoice_url=payload.supplier_invoice_url or payload.attachment_url,
        delivery_challan_url=payload.delivery_challan_url,
        supporting_documents=list(payload.supporting_documents or []),
        # 10. Additional Info
        terms_and_conditions=payload.terms_and_conditions,
        internal_remarks=payload.internal_remarks or payload.notes,
        tags=list(payload.tags or []),
    )
    inv.items = built_items
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
    purchase_type: str | None = Query(default=None),
    receiving_status: str | None = Query(default=None),
    warehouse_id: str | None = Query(default=None),
    cost_center_id: str | None = Query(default=None),
    project_id: str | None = Query(default=None),
    tag: str | None = Query(default=None),
    search: str | None = Query(default=None, description="matches invoice_number, purchase_number, or reference_number"),
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
    if purchase_type:
        q = q.filter(PurchaseInvoice.purchase_type == purchase_type)
    if receiving_status:
        q = q.filter(PurchaseInvoice.receiving_status == receiving_status)
    if warehouse_id:
        q = q.filter(PurchaseInvoice.warehouse_id == warehouse_id)
    if cost_center_id:
        q = q.filter(PurchaseInvoice.cost_center_id == cost_center_id)
    if project_id:
        q = q.filter(PurchaseInvoice.project_id == project_id)
    if tag:
        # Filter JSON tags
        q = q.filter(PurchaseInvoice.tags.contains(tag))
    if search:
        s = f"%{search}%"
        q = q.filter(
            or_(
                PurchaseInvoice.invoice_number.ilike(s),
                PurchaseInvoice.purchase_number.ilike(s),
                PurchaseInvoice.purchase_id.ilike(s),
                PurchaseInvoice.reference_number.ilike(s),
            )
        )
    return q.order_by(PurchaseInvoice.created_at.desc()).all()


@router.get("/{id}", response_model=PurchaseOut)
def get_purchase(id: str, user: User = Depends(_view), db: Session = Depends(get_db)) -> PurchaseInvoice:
    return _owned(db, id, _org_id(user))


@router.put("/{id}", response_model=PurchaseOut)
@router.patch("/{id}", response_model=PurchaseOut)
def update_purchase(
    id: str,
    payload: PurchaseUpdate,
    user: User = Depends(_edit),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> PurchaseInvoice:
    org_id = _org_id(user)
    inv = _owned(db, id, org_id)
    if inv.status != "pending":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Only pending invoices can be edited")

    data = payload.model_dump(exclude_unset=True)
    items_raw = data.pop("items", None)

    for field, value in data.items():
        setattr(inv, field, value)

    if items_raw is not None:
        built_items, subtotal, item_discounts, item_taxes = purchase_service.build_and_calculate_items(
            db, org_id, [PurchaseItemIn(**i) for i in items_raw]
        )
        inv.items = built_items
    else:
        subtotal = sum(round(i.purchase_price * i.quantity, 2) for i in inv.items)
        item_discounts = sum(round(i.discount or 0.0, 2) for i in inv.items)
        item_taxes = sum(round(i.tax or 0.0, 2) for i in inv.items)

    net_subtotal, effective_discount, effective_tax, grand_total = purchase_service.calculate_header_totals(
        subtotal=subtotal,
        item_discounts=item_discounts,
        item_taxes=item_taxes,
        overall_discount=inv.discount,
        header_tax=inv.tax,
        freight_charges=inv.freight_charges,
        packing_charges=inv.packing_charges,
        insurance_charges=inv.insurance_charges,
        other_charges=inv.other_charges,
        round_off=inv.round_off,
    )
    inv.subtotal = net_subtotal
    inv.discount = effective_discount
    inv.tax = effective_tax
    inv.total = grand_total

    db.commit()
    db.refresh(inv)
    return inv


@router.patch("/{id}/approve", response_model=PurchaseOut)
def approve_purchase(
    id: str,
    user: User = Depends(_approve),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> PurchaseInvoice:
    """Approve -> add stock (purchase_in) and increase the supplier's total_purchases."""
    org_id = _org_id(user)
    inv = _owned(db, id, org_id)
    if inv.status != "pending":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Only pending invoices can be approved (this is '{inv.status}')",
        )

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

        db.add(
            StockMovement(
                organization_id=org_id,
                product_id=item.product_id,
                variant_id=item.variant_id,
                movement_type="purchase_in",
                quantity=item.quantity,
                balance_after=bal,
                note=f"Purchase {inv.invoice_number}",
                created_by=user.id,
            )
        )

    if inv.supplier_id:
        supplier = db.get(Supplier, inv.supplier_id)
        if supplier:
            supplier.total_purchases = round((supplier.total_purchases or 0) + inv.total, 2)

    now = datetime.now(timezone.utc)
    inv.status = "approved"
    inv.approval_status = "Approved"
    inv.approved_by = user.id
    inv.approved_at = now
    inv.stock_added = True
    inv.receiving_status = "Completed"
    inv.received_date = inv.received_date or now
    inv.received_by = inv.received_by or user.id

    db.commit()
    db.refresh(inv)
    return inv


@router.patch("/{id}/payment-status", response_model=PurchaseOut)
def set_payment_status(
    id: str,
    payload: PaymentStatusUpdate,
    user: User = Depends(_edit),
    db: Session = Depends(get_db),
) -> PurchaseInvoice:
    inv = _owned(db, id, _org_id(user))
    if payload.payment_status not in PAYMENT_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"payment_status must be one of {sorted(PAYMENT_STATUSES)}",
        )
    inv.payment_status = payload.payment_status
    if payload.amount_paid is not None:
        inv.amount_paid = payload.amount_paid
    if payload.payment_method:
        inv.payment_method = payload.payment_method
    if payload.payment_reference:
        inv.payment_reference = payload.payment_reference

    db.commit()
    db.refresh(inv)
    return inv


@router.patch("/{id}/cancel", response_model=PurchaseOut)
def cancel_purchase(
    id: str,
    payload: CancelBody,
    user: User = Depends(_approve),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> PurchaseInvoice:
    """Cancel. If approved, reverse the stock-in and the supplier's total_purchases."""
    org_id = _org_id(user)
    inv = _owned(db, id, org_id)
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
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=f"Cannot reverse: stock already consumed for {item.product_name}",
                    )
                variant.inventory = new_bal
                bal = new_bal
            else:
                product = db.get(Product, item.product_id) if item.product_id else None
                if product is None:
                    continue
                new_bal = (product.total_inventory or 0) - item.quantity
                if new_bal < 0:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=f"Cannot reverse: stock already consumed for {item.product_name}",
                    )
                product.total_inventory = new_bal
                bal = new_bal

            db.add(
                StockMovement(
                    organization_id=org_id,
                    product_id=item.product_id,
                    variant_id=item.variant_id,
                    movement_type="purchase_return",
                    quantity=-item.quantity,
                    balance_after=bal,
                    note=f"Cancel purchase {inv.invoice_number}",
                    created_by=user.id,
                )
            )

        if inv.supplier_id:
            supplier = db.get(Supplier, inv.supplier_id)
            if supplier:
                supplier.total_purchases = round((supplier.total_purchases or 0) - inv.total, 2)
        inv.stock_added = False

    inv.status = "cancelled"
    inv.approval_status = "Rejected"
    inv.approval_remarks = payload.reason
    db.commit()
    db.refresh(inv)
    return inv


@router.patch("/{id}/reject", response_model=PurchaseOut)
def reject_purchase(
    id: str,
    payload: CancelBody,
    user: User = Depends(_approve),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> PurchaseInvoice:
    """Reject purchase invoice with manager remarks."""
    return cancel_purchase(id, payload, user, _unlocked, db)


@router.post("/{id}/documents", response_model=PurchaseOut)
def upload_document(
    request: Request,
    id: str,
    file: UploadFile = File(...),
    user: User = Depends(_edit),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> PurchaseInvoice:
    """Attach a supporting document (invoice scan/photo — image or PDF, max 10 MB)."""
    inv = _owned(db, id, _org_id(user))
    doc_url, _ = save_upload(db, inv.organization_id, file, request)
    inv.attachment_url = doc_url
    inv.supplier_invoice_url = doc_url
    docs = list(inv.supporting_documents or [])
    docs.append({"name": file.filename or "Document", "url": doc_url})
    inv.supporting_documents = docs
    db.commit()
    db.refresh(inv)
    return inv


@router.post("/{id}/returns", response_model=PurchaseOut)
def purchase_return(
    id: str,
    payload: PurchaseReturnBody,
    user: User = Depends(_approve),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> PurchaseInvoice:
    """Return items to the supplier: removes stock and reduces the supplier's payable."""
    org_id = _org_id(user)
    inv = _owned(db, id, org_id)
    if inv.status != "approved":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Only approved invoices can be returned")

    reversed_value = 0.0
    for ri in payload.items:
        match = next((i for i in inv.items if i.product_id == ri.product_id and i.variant_id == ri.variant_id), None)
        price = match.purchase_price if match else 0
        if ri.variant_id:
            variant = db.get(ProductVariant, ri.variant_id)
            if variant is None:
                continue
            new_bal = (variant.inventory or 0) - ri.quantity
            if new_bal < 0:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Cannot return more than in stock")
            variant.inventory = new_bal
        else:
            product = db.get(Product, ri.product_id) if ri.product_id else None
            if product is None:
                continue
            new_bal = (product.total_inventory or 0) - ri.quantity
            if new_bal < 0:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Cannot return more than in stock")
            product.total_inventory = new_bal

        db.add(
            StockMovement(
                organization_id=org_id,
                product_id=ri.product_id,
                variant_id=ri.variant_id,
                movement_type="purchase_return",
                quantity=-ri.quantity,
                balance_after=new_bal,
                note=f"Return on {inv.invoice_number}" + (f" — {payload.reason}" if payload.reason else ""),
                created_by=user.id,
            )
        )
        reversed_value += price * ri.quantity

    if inv.supplier_id and reversed_value:
        supplier = db.get(Supplier, inv.supplier_id)
        if supplier:
            supplier.total_purchases = round((supplier.total_purchases or 0) - reversed_value, 2)

    db.commit()
    db.refresh(inv)
    return inv


@router.delete("/{id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_purchase(
    id: str,
    user: User = Depends(require_permission("purchases", "delete")),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> None:
    inv = _owned(db, id, _org_id(user))
    if inv.status == "approved":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Cancel an approved invoice before deleting")
    db.delete(inv)
    db.commit()
