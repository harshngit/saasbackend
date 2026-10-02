import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.core import scoping
from app.models import (
    GoodsReceiptNote,
    Product,
    ProductVariant,
    PurchaseInvoice,
    PurchaseInvoiceItem,
    PurchaseReturn,
    PurchaseReturnItem,
    StockMovement,
    Supplier,
    SupplierInvoice,
    User,
    Warehouse,
    WarehouseStock,
)
from app.schemas.purchase_return import (
    PurchaseReturnCreate,
    PurchaseReturnItemCreate,
    PurchaseReturnListResponse,
    PurchaseReturnOut,
    PurchaseReturnUpdate,
)
from app.services import numbering_service

logger = logging.getLogger("crm.purchase_returns")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def get_previously_returned_quantity(
    db: Session,
    purchase_item_id: str,
    exclude_return_id: str | None = None,
) -> int:
    """Calculate the cumulative returned quantity for a purchase invoice line item
    across all active (non-cancelled) Purchase Returns."""
    q = (
        db.query(func.coalesce(func.sum(PurchaseReturnItem.quantity), 0))
        .join(PurchaseReturn, PurchaseReturn.id == PurchaseReturnItem.purchase_return_id)
        .filter(
            PurchaseReturnItem.purchase_item_id == purchase_item_id,
            PurchaseReturn.status != "cancelled",
        )
    )
    if exclude_return_id:
        q = q.filter(PurchaseReturn.id != exclude_return_id)
    return int(q.scalar() or 0)


def get_previously_returned_amount(
    db: Session,
    purchase_id: str,
    exclude_return_id: str | None = None,
) -> float:
    """Calculate the cumulative return value across all active (non-cancelled) Purchase Returns for a purchase."""
    q = (
        db.query(func.coalesce(func.sum(PurchaseReturnItem.line_total), 0.0))
        .join(PurchaseReturn, PurchaseReturn.id == PurchaseReturnItem.purchase_return_id)
        .filter(
            PurchaseReturn.purchase_id == purchase_id,
            PurchaseReturn.status != "cancelled",
        )
    )
    if exclude_return_id:
        q = q.filter(PurchaseReturn.id != exclude_return_id)
    return round(float(q.scalar() or 0.0), 2)


def recalculate_purchase_and_supplier_invoice_returns(
    db: Session,
    purchase_id: str | None,
) -> None:
    """Recalculate and synchronize return_amount and payment_status on both PurchaseInvoice
    and SupplierInvoice for all active (non-cancelled, non-draft) purchase returns."""
    if not purchase_id:
        return

    total_return_val = (
        db.query(func.coalesce(func.sum(PurchaseReturnItem.line_total), 0.0))
        .join(PurchaseReturn, PurchaseReturn.id == PurchaseReturnItem.purchase_return_id)
        .filter(
            PurchaseReturn.purchase_id == purchase_id,
            PurchaseReturn.status.notin_(["draft", "cancelled"]),
        )
        .scalar()
    ) or 0.0
    total_return_val = round(float(total_return_val), 2)

    # 1. Update PurchaseInvoice
    purchase = db.get(PurchaseInvoice, purchase_id)
    if purchase:
        purchase.return_amount = total_return_val
        total_settled = round((purchase.amount_paid or 0.0) + purchase.return_amount, 2)
        if total_settled >= (purchase.total or 0.0) and (purchase.total or 0.0) > 0:
            purchase.payment_status = "paid"
        elif total_settled > 0:
            purchase.payment_status = "partially_paid"
        else:
            purchase.payment_status = "unpaid"

    # 2. Update linked SupplierInvoices
    supplier_invoices = (
        db.query(SupplierInvoice)
        .filter(SupplierInvoice.purchase_id == purchase_id)
        .all()
    )
    from app.services import supplier_payment_service
    for sinv in supplier_invoices:
        sinv.return_amount = total_return_val
        supplier_payment_service.recalculate_supplier_invoice(db, sinv)


def validate_and_build_return_items(
    db: Session,
    org_id: str,
    purchase: PurchaseInvoice,
    items_in: list[PurchaseReturnItemCreate],
    exclude_return_id: str | None = None,
) -> list[PurchaseReturnItem]:
    """Validate return quantities against received purchase line quantities minus
    previously returned quantities to strictly prevent over-returns."""
    if not items_in:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="At least one return item must be provided",
        )

    # Map purchase line items by ID and by (product_id, variant_id)
    purchase_items_by_id = {item.id: item for item in purchase.items}
    built_items: list[PurchaseReturnItem] = []

    for item_in in items_in:
        if item_in.quantity <= 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Return quantity must be greater than zero",
            )

        match_item: PurchaseInvoiceItem | None = None
        if item_in.purchase_item_id:
            match_item = purchase_items_by_id.get(item_in.purchase_item_id)
            if match_item is None:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Purchase item ID '{item_in.purchase_item_id}' does not belong to purchase invoice '{purchase.invoice_number}'",
                )
        else:
            # Match by product_id and variant_id
            for p_item in purchase.items:
                if p_item.product_id == item_in.product_id and (
                    p_item.variant_id == item_in.variant_id or (not p_item.variant_id and not item_in.variant_id)
                ):
                    match_item = p_item
                    break

        if match_item is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Product '{item_in.product_id}' (variant: '{item_in.variant_id}') does not belong to purchase invoice '{purchase.invoice_number}'",
            )

        # Cumulative return validation: received_qty - already_returned_qty
        received_qty = match_item.quantity
        prev_returned = get_previously_returned_quantity(
            db, match_item.id, exclude_return_id=exclude_return_id
        )
        eligible_qty = max(received_qty - prev_returned, 0)

        if item_in.quantity > eligible_qty:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"Return quantity ({item_in.quantity}) exceeds eligible return quantity "
                    f"({eligible_qty}) for item '{match_item.product_name}' (Total ordered: {received_qty}, "
                    f"Already returned: {prev_returned})"
                ),
            )

        unit_price = item_in.unit_price if item_in.unit_price is not None else match_item.purchase_price
        tax_rate = item_in.tax_rate if item_in.tax_rate is not None else (match_item.tax_rate or 0.0)
        tax_amount = round(unit_price * item_in.quantity * (tax_rate / 100.0), 2)
        line_total = round(unit_price * item_in.quantity + tax_amount, 2)

        return_item = PurchaseReturnItem(
            purchase_item_id=match_item.id,
            product_id=match_item.product_id,
            variant_id=match_item.variant_id,
            product_code=match_item.product_code,
            barcode=match_item.barcode,
            product_name=match_item.product_name,
            unit_of_measure_uom=match_item.unit_of_measure_uom,
            quantity=item_in.quantity,
            unit_price=unit_price,
            tax_rate=tax_rate,
            tax_amount=tax_amount,
            line_total=line_total,
            reason=item_in.reason,
            batch_number=item_in.batch_number or match_item.batch_number,
            serial_numbers=item_in.serial_numbers or match_item.serial_numbers,
            expiry_date=item_in.expiry_date or match_item.expiry_date,
        )
        built_items.append(return_item)

    proposed_return_total = sum(item.line_total for item in built_items)
    prev_returned_amount = get_previously_returned_amount(
        db, purchase.id, exclude_return_id=exclude_return_id
    )
    sinv_paid = (
        db.query(func.coalesce(func.sum(SupplierInvoice.amount_paid), 0.0))
        .filter(
            SupplierInvoice.purchase_id == purchase.id,
            SupplierInvoice.status != "cancelled",
        )
        .scalar()
    ) or 0.0
    effective_paid = max(purchase.amount_paid or 0.0, float(sinv_paid))
    max_return_allowed = max(
        round((purchase.total or 0.0) - prev_returned_amount - effective_paid, 2),
        0.0,
    )
    if proposed_return_total > max_return_allowed:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Total return value ({proposed_return_total:.2f}) exceeds remaining eligible unpaid balance "
                f"({max_return_allowed:.2f}) for purchase '{purchase.invoice_number}'. "
                f"Return cannot exceed remaining unpaid balance."
            ),
        )

    return built_items


def create_purchase_return(
    db: Session,
    org_id: str,
    payload: PurchaseReturnCreate,
    user_id: str | None = None,
) -> PurchaseReturn:
    """Create a new Purchase Return record in 'draft' status without immediate inventory deduction."""
    purchase = db.get(PurchaseInvoice, payload.purchase_id)
    if purchase is None or purchase.organization_id != org_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Purchase invoice not found in your organization",
        )

    if purchase.status not in ("approved", "confirmed", "closed", "received", "submitted", "draft"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot create a return for purchase invoice in state '{purchase.status}'",
        )

    supplier_id = payload.supplier_id or purchase.supplier_id
    if supplier_id:
        supplier = db.get(Supplier, supplier_id)
        if supplier is None or supplier.organization_id != org_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Supplier not found in your organization",
            )

    grn_id = payload.grn_id
    if grn_id:
        grn = db.get(GoodsReceiptNote, grn_id)
        if grn is None or grn.organization_id != org_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Goods receipt note not found in your organization",
            )

    warehouse_id = payload.warehouse_id or purchase.warehouse_id
    if warehouse_id:
        warehouse = db.get(Warehouse, warehouse_id)
        if warehouse is None or warehouse.organization_id != org_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Warehouse not found in your organization",
            )

    return_number = numbering_service.next_number(
        db, org_id, PurchaseReturn.return_number, "PR"
    )

    built_items = validate_and_build_return_items(db, org_id, purchase, payload.items)

    purchase_return = PurchaseReturn(
        organization_id=org_id,
        return_number=return_number,
        purchase_id=purchase.id,
        supplier_id=supplier_id,
        grn_id=grn_id,
        warehouse_id=warehouse_id,
        status="draft",
        return_date=payload.return_date or _now(),
        reason=payload.reason,
        notes=payload.notes,
        created_by=user_id,
    )
    purchase_return.items.extend(built_items)

    db.add(purchase_return)
    db.commit()
    db.refresh(purchase_return)
    return purchase_return


def update_purchase_return(
    db: Session,
    org_id: str,
    return_id: str,
    payload: PurchaseReturnUpdate,
    user_id: str | None = None,
) -> PurchaseReturn:
    """Update a draft purchase return."""
    purchase_return = db.get(PurchaseReturn, return_id)
    if purchase_return is None or purchase_return.organization_id != org_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Purchase return not found",
        )

    if purchase_return.status != "draft":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot edit purchase return in status '{purchase_return.status}'. Only 'draft' returns can be edited.",
        )

    if payload.return_date is not None:
        purchase_return.return_date = payload.return_date
    if payload.reason is not None:
        purchase_return.reason = payload.reason
    if payload.notes is not None:
        purchase_return.notes = payload.notes
    if payload.warehouse_id is not None:
        if payload.warehouse_id:
            wh = db.get(Warehouse, payload.warehouse_id)
            if wh is None or wh.organization_id != org_id:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Warehouse not found in your organization",
                )
            purchase_return.warehouse_id = payload.warehouse_id
        else:
            purchase_return.warehouse_id = None

    if payload.items is not None:
        purchase = purchase_return.purchase
        if purchase is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Source purchase invoice not found",
            )
        built_items = validate_and_build_return_items(
            db, org_id, purchase, payload.items, exclude_return_id=purchase_return.id
        )
        purchase_return.items.clear()
        purchase_return.items.extend(built_items)

    db.commit()
    db.refresh(purchase_return)
    return purchase_return


def deduct_inventory_for_return(
    db: Session,
    org_id: str,
    return_record: PurchaseReturn,
    user_id: str | None = None,
) -> None:
    """Atomically deduct inventory and log StockMovement upon return confirmation / dispatch."""
    if return_record.stock_deducted:
        return  # Deduct exactly once

    reversed_value = 0.0
    purchase_no = return_record.purchase.invoice_number if return_record.purchase else return_record.return_number

    for ri in return_record.items:
        if ri.variant_id:
            variant = db.get(ProductVariant, ri.variant_id)
            if variant is None or variant.product.organization_id != org_id:
                continue
            new_bal = (variant.inventory or 0) - ri.quantity
            if new_bal < 0:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Cannot return more than available stock for variant '{variant.name or ri.product_name}' (Current: {variant.inventory}, Requested: {ri.quantity})",
                )
            variant.inventory = new_bal
            balance_after = new_bal
        else:
            product = db.get(Product, ri.product_id) if ri.product_id else None
            if product is None or product.organization_id != org_id:
                continue
            new_bal = (product.total_inventory or 0) - ri.quantity
            if new_bal < 0:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Cannot return more than available stock for product '{product.name}' (Current: {product.total_inventory}, Requested: {ri.quantity})",
                )
            product.total_inventory = new_bal
            balance_after = new_bal

        # Also decrement WarehouseStock if warehouse is set
        if return_record.warehouse_id:
            wh_stock = (
                db.query(WarehouseStock)
                .filter(
                    WarehouseStock.warehouse_id == return_record.warehouse_id,
                    WarehouseStock.product_id == ri.product_id,
                    WarehouseStock.variant_id == ri.variant_id,
                )
                .first()
            )
            if wh_stock:
                wh_stock.quantity = max((wh_stock.quantity or 0) - ri.quantity, 0)

        db.add(
            StockMovement(
                organization_id=org_id,
                warehouse_id=return_record.warehouse_id,
                product_id=ri.product_id,
                variant_id=ri.variant_id,
                movement_type="purchase_return",
                quantity=-ri.quantity,
                balance_after=balance_after,
                note=f"Return {return_record.return_number} on {purchase_no}" + (f" — {ri.reason or return_record.reason}" if (ri.reason or return_record.reason) else ""),
                created_by=user_id,
            )
        )
        reversed_value += ri.line_total

    # Supplier financial adjustment: reduce supplier's total_purchases
    if return_record.supplier_id and reversed_value:
        supplier = db.get(Supplier, return_record.supplier_id)
        if supplier:
            supplier.total_purchases = round(max((supplier.total_purchases or 0) - reversed_value, 0.0), 2)

    return_record.stock_deducted = True


def restore_inventory_for_return(
    db: Session,
    org_id: str,
    return_record: PurchaseReturn,
    user_id: str | None = None,
) -> None:
    """Restore inventory and adjust supplier totals if a return was cancelled after stock deduction."""
    if not return_record.stock_deducted:
        return

    reversed_value = 0.0
    purchase_no = return_record.purchase.invoice_number if return_record.purchase else return_record.return_number

    for ri in return_record.items:
        if ri.variant_id:
            variant = db.get(ProductVariant, ri.variant_id)
            if variant is None or variant.product.organization_id != org_id:
                continue
            new_bal = (variant.inventory or 0) + ri.quantity
            variant.inventory = new_bal
            balance_after = new_bal
        else:
            product = db.get(Product, ri.product_id) if ri.product_id else None
            if product is None or product.organization_id != org_id:
                continue
            new_bal = (product.total_inventory or 0) + ri.quantity
            product.total_inventory = new_bal
            balance_after = new_bal

        if return_record.warehouse_id:
            wh_stock = (
                db.query(WarehouseStock)
                .filter(
                    WarehouseStock.warehouse_id == return_record.warehouse_id,
                    WarehouseStock.product_id == ri.product_id,
                    WarehouseStock.variant_id == ri.variant_id,
                )
                .first()
            )
            if wh_stock:
                wh_stock.quantity = (wh_stock.quantity or 0) + ri.quantity

        db.add(
            StockMovement(
                organization_id=org_id,
                warehouse_id=return_record.warehouse_id,
                product_id=ri.product_id,
                variant_id=ri.variant_id,
                movement_type="purchase_return",
                quantity=ri.quantity,
                balance_after=balance_after,
                note=f"Cancelled Return {return_record.return_number} on {purchase_no}",
                created_by=user_id,
            )
        )
        reversed_value += ri.line_total

    if return_record.supplier_id and reversed_value:
        supplier = db.get(Supplier, return_record.supplier_id)
        if supplier:
            supplier.total_purchases = round((supplier.total_purchases or 0) + reversed_value, 2)

    return_record.stock_deducted = False


def confirm_purchase_return(
    db: Session,
    org_id: str,
    return_id: str,
    user_id: str | None = None,
) -> PurchaseReturn:
    """Transition: draft -> confirmed (deducts stock and records lifecycle timestamp)."""
    purchase_return = db.get(PurchaseReturn, return_id)
    if purchase_return is None or purchase_return.organization_id != org_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Purchase return not found",
        )

    if purchase_return.status != "draft":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot confirm purchase return in status '{purchase_return.status}'. Only 'draft' returns can be confirmed.",
        )

    deduct_inventory_for_return(db, org_id, purchase_return, user_id=user_id)
    purchase_return.status = "confirmed"
    purchase_return.confirmed_at = _now()
    purchase_return.confirmed_by = user_id
    db.flush()
    recalculate_purchase_and_supplier_invoice_returns(db, purchase_return.purchase_id)

    db.commit()
    db.refresh(purchase_return)
    return purchase_return


def dispatch_purchase_return(
    db: Session,
    org_id: str,
    return_id: str,
    user_id: str | None = None,
) -> PurchaseReturn:
    """Transition: confirmed / draft -> dispatched."""
    purchase_return = db.get(PurchaseReturn, return_id)
    if purchase_return is None or purchase_return.organization_id != org_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Purchase return not found",
        )

    if purchase_return.status not in ("draft", "confirmed"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot dispatch purchase return in status '{purchase_return.status}'. Only 'draft' or 'confirmed' returns can be dispatched.",
        )

    if not purchase_return.stock_deducted:
        deduct_inventory_for_return(db, org_id, purchase_return, user_id=user_id)
    if not purchase_return.confirmed_at:
        purchase_return.confirmed_at = _now()
        purchase_return.confirmed_by = user_id

    purchase_return.status = "dispatched"
    purchase_return.dispatched_at = _now()
    purchase_return.dispatched_by = user_id
    db.flush()
    recalculate_purchase_and_supplier_invoice_returns(db, purchase_return.purchase_id)

    db.commit()
    db.refresh(purchase_return)
    return purchase_return


def complete_purchase_return(
    db: Session,
    org_id: str,
    return_id: str,
    user_id: str | None = None,
) -> PurchaseReturn:
    """Transition: confirmed / dispatched -> completed (terminal state)."""
    purchase_return = db.get(PurchaseReturn, return_id)
    if purchase_return is None or purchase_return.organization_id != org_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Purchase return not found",
        )

    if purchase_return.status not in ("confirmed", "dispatched"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot complete purchase return in status '{purchase_return.status}'. Only 'confirmed' or 'dispatched' returns can be completed.",
        )

    if not purchase_return.stock_deducted:
        deduct_inventory_for_return(db, org_id, purchase_return, user_id=user_id)

    purchase_return.status = "completed"
    purchase_return.completed_at = _now()
    purchase_return.completed_by = user_id
    db.flush()
    recalculate_purchase_and_supplier_invoice_returns(db, purchase_return.purchase_id)

    db.commit()
    db.refresh(purchase_return)
    return purchase_return


def cancel_purchase_return(
    db: Session,
    org_id: str,
    return_id: str,
    cancel_reason: str | None = None,
    user_id: str | None = None,
) -> PurchaseReturn:
    """Transition: draft / confirmed / dispatched -> cancelled (reverses stock if deducted)."""
    purchase_return = db.get(PurchaseReturn, return_id)
    if purchase_return is None or purchase_return.organization_id != org_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Purchase return not found",
        )

    if purchase_return.status in ("completed", "cancelled"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot cancel a '{purchase_return.status}' purchase return.",
        )

    if purchase_return.stock_deducted:
        restore_inventory_for_return(db, org_id, purchase_return, user_id=user_id)

    purchase_return.status = "cancelled"
    purchase_return.cancel_reason = cancel_reason
    purchase_return.cancelled_at = _now()
    purchase_return.cancelled_by = user_id
    db.flush()
    recalculate_purchase_and_supplier_invoice_returns(db, purchase_return.purchase_id)

    db.commit()
    db.refresh(purchase_return)
    return purchase_return


def get_purchase_return_detail(
    db: Session,
    org_id: str,
    return_id: str,
) -> PurchaseReturn:
    """Get single Purchase Return by UUID or return_number with tenant isolation."""
    record = (
        db.query(PurchaseReturn)
        .filter(
            PurchaseReturn.organization_id == org_id,
            or_(PurchaseReturn.id == return_id, PurchaseReturn.return_number == return_id),
        )
        .first()
    )
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Purchase return not found",
        )
    return record


def list_purchase_returns(
    db: Session,
    org_id: str,
    status_filter: str | None = None,
    supplier_id: str | None = None,
    purchase_id: str | None = None,
    search: str | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    page: int | None = None,
    page_size: int | None = None,
) -> PurchaseReturnListResponse:
    """List purchase returns with tenant scoping, filtering, search, and pagination."""
    q = db.query(PurchaseReturn).filter(PurchaseReturn.organization_id == org_id)

    if status_filter:
        q = q.filter(PurchaseReturn.status == status_filter)
    if supplier_id:
        q = q.filter(PurchaseReturn.supplier_id == supplier_id)
    if purchase_id:
        q = q.filter(PurchaseReturn.purchase_id == purchase_id)
    if date_from:
        q = q.filter(PurchaseReturn.return_date >= date_from)
    if date_to:
        q = q.filter(PurchaseReturn.return_date <= date_to)

    if search:
        search_term = f"%{search.strip()}%"
        q = q.outerjoin(Supplier, PurchaseReturn.supplier_id == Supplier.id).outerjoin(
            PurchaseInvoice, PurchaseReturn.purchase_id == PurchaseInvoice.id
        ).filter(
            or_(
                PurchaseReturn.return_number.ilike(search_term),
                PurchaseReturn.reason.ilike(search_term),
                PurchaseReturn.notes.ilike(search_term),
                Supplier.name.ilike(search_term),
                PurchaseInvoice.invoice_number.ilike(search_term),
                PurchaseInvoice.purchase_number.ilike(search_term),
            )
        )

    total = q.count()
    q = q.order_by(PurchaseReturn.created_at.desc())

    if page is not None and page_size is not None:
        offset = (page - 1) * page_size
        items = q.offset(offset).limit(page_size).all()
    else:
        items = q.all()

    return PurchaseReturnListResponse(
        items=[PurchaseReturnOut.model_validate(item) for item in items],
        total=total,
        page=page,
        page_size=page_size,
    )


def process_legacy_purchase_return(
    db: Session,
    org_id: str,
    purchase: PurchaseInvoice,
    items_data: list[dict[str, Any]],
    reason: str | None,
    user: User,
) -> PurchaseReturn:
    """Bridges legacy POST /purchases/{id}/returns into the canonical PurchaseReturn system."""
    items_in = [
        PurchaseReturnItemCreate(
            product_id=item.get("product_id"),
            variant_id=item.get("variant_id"),
            quantity=item.get("quantity", 0),
            reason=reason,
        )
        for item in items_data
    ]
    payload = PurchaseReturnCreate(
        purchase_id=purchase.id,
        supplier_id=purchase.supplier_id,
        warehouse_id=purchase.warehouse_id,
        reason=reason,
        items=items_in,
    )
    ret = create_purchase_return(db, org_id, payload, user_id=user.id)
    ret = confirm_purchase_return(db, org_id, ret.id, user_id=user.id)
    ret = complete_purchase_return(db, org_id, ret.id, user_id=user.id)
    return ret
