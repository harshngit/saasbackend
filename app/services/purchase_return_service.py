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


def received_quantity(purchase: PurchaseInvoice, item: PurchaseInvoiceItem) -> int:
    """What was actually received on this purchase line: the accepted quantity of its
    confirmed GRNs (`received_qty`, rolled up by grn_service). Never the ordered
    quantity -- goods that never arrived cannot be sent back.

    Legacy exception: a purchase that put stock in before GRN lines tracked receipts
    (`stock_added` with no received quantity on any line) counts as fully received."""
    rec = int(item.received_qty or 0)
    if rec == 0 and purchase.stock_added and not any((i.received_qty or 0) for i in purchase.items):
        return int(item.quantity or 0)
    return rec


def attach_line_figures(db: Session, record: PurchaseReturn) -> PurchaseReturn:
    """Give each line its ordered / received / previously-returned / returnable figures
    (from the source purchase line) for the single-return API responses."""
    purchase = record.purchase
    lines_by_id = {i.id: i for i in purchase.items} if purchase is not None else {}
    for ri in record.items:
        p_item = lines_by_id.get(ri.purchase_item_id)
        if p_item is None:
            ri.__dict__["_line_figures"] = {}
            continue
        received = received_quantity(purchase, p_item)
        prev = get_previously_returned_quantity(db, p_item.id, exclude_return_id=record.id)
        ri.__dict__["_line_figures"] = {
            "ordered_qty": int(p_item.quantity or 0),
            "received_qty": received,
            "previously_returned_qty": prev,
            "returnable_qty": max(received - prev, 0),
        }
    return record


def return_warehouse_id(db: Session, org_id: str, warehouse_id: str | None, purchase: PurchaseInvoice | None) -> str:
    """The warehouse the goods leave from: the return's, else the purchase's, else the firm default."""
    from app.services import stock_service

    wh = stock_service.owned_warehouse(db, warehouse_id or (purchase.warehouse_id if purchase else None), org_id)
    if wh is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Warehouse not found in your organization")
    return wh.id


def check_return_stock(db: Session, org_id: str, warehouse_id: str, items: list) -> None:  # noqa: ANN001
    """Every product's total on this return must be available in the warehouse
    (on hand minus what is reserved for orders). 400 naming the product otherwise."""
    from app.models import Warehouse as _Warehouse
    from app.services import stock_service

    wanted: dict[tuple[str, str | None], list] = {}
    for it in items:
        if not it.product_id:
            continue
        entry = wanted.setdefault((it.product_id, it.variant_id), [0, it.product_name])
        entry[0] += int(it.quantity or 0)
    wh = db.get(_Warehouse, warehouse_id)
    wh_name = wh.name if wh is not None else warehouse_id
    for (pid, vid), (qty, name) in wanted.items():
        avail = max(int(stock_service.available(db, warehouse_id, pid, vid)), 0)
        if qty > avail:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"Not enough stock of '{name}' in warehouse '{wh_name}' to return {qty}: "
                    f"only {avail} can be returned (available stock)."
                ),
            )


def validate_and_build_return_items(
    db: Session,
    org_id: str,
    purchase: PurchaseInvoice,
    items_in: list[PurchaseReturnItemCreate],
    exclude_return_id: str | None = None,
) -> list[PurchaseReturnItem]:
    """Validate return quantities against RECEIVED purchase line quantities minus
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
        received_qty = received_quantity(purchase, match_item)
        prev_returned = get_previously_returned_quantity(
            db, match_item.id, exclude_return_id=exclude_return_id
        )
        eligible_qty = max(received_qty - prev_returned, 0)

        if item_in.quantity > eligible_qty:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"Return quantity ({item_in.quantity}) for '{match_item.product_name}' exceeds eligible "
                    f"return quantity: only {eligible_qty} allowed (received {received_qty}, already returned "
                    f"{prev_returned}, ordered {match_item.quantity})"
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
    warehouse_id = return_warehouse_id(db, org_id, warehouse_id, purchase)
    check_return_stock(db, org_id, warehouse_id, built_items)

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

    if payload.items is not None or payload.warehouse_id is not None:
        # Re-check the stock rule whenever the lines or the source warehouse change.
        wh_id = return_warehouse_id(db, org_id, purchase_return.warehouse_id, purchase_return.purchase)
        check_return_stock(db, org_id, wh_id, purchase_return.items)

    db.commit()
    db.refresh(purchase_return)
    return purchase_return


def _move_return_stock(
    db: Session,
    org_id: str,
    return_record: PurchaseReturn,
    sign: int,
    user_id: str | None,
) -> float:
    """Move every line's stock through stock_service (warehouse row, catalog counter and
    stock_movements ledger together, under row locks). Returns the value moved."""
    from app.services import stock_service

    wh_id = return_warehouse_id(db, org_id, return_record.warehouse_id, return_record.purchase)
    return_record.warehouse_id = wh_id
    purchase_no = return_record.purchase.invoice_number if return_record.purchase else return_record.return_number
    lines = [ri for ri in return_record.items if ri.product_id]
    stock_service.lock_stock_items(db, org_id, wh_id, lines)
    if sign < 0:
        check_return_stock(db, org_id, wh_id, lines)  # re-checked under the lock
    moved_value = 0.0
    for ri in lines:
        if sign < 0:
            reason = ri.reason or return_record.reason
            note = f"Return {return_record.return_number} on {purchase_no}" + (f" — {reason}" if reason else "")
        else:
            note = f"Cancelled Return {return_record.return_number} on {purchase_no}"
        stock_service.adjust_on_hand(
            db,
            org_id,
            wh_id,
            ri.product_id,
            ri.variant_id,
            float(sign * ri.quantity),
            movement_type="purchase_return",
            note=note,
            created_by=user_id,
            batch={"batch_number": ri.batch_number, "expiry_date": ri.expiry_date} if ri.batch_number else None,
            serial_numbers=ri.serial_numbers,
        )
        moved_value += ri.line_total or 0.0
    return moved_value


def deduct_inventory_for_return(
    db: Session,
    org_id: str,
    return_record: PurchaseReturn,
    user_id: str | None = None,
) -> None:
    """Deduct inventory (exactly once) and reduce the supplier's purchase total."""
    if return_record.stock_deducted:
        return
    reversed_value = _move_return_stock(db, org_id, return_record, -1, user_id)
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
    """Put the stock back and restore the supplier total when a deducted return is cancelled."""
    if not return_record.stock_deducted:
        return
    reversed_value = _move_return_stock(db, org_id, return_record, +1, user_id)
    if return_record.supplier_id and reversed_value:
        supplier = db.get(Supplier, return_record.supplier_id)
        if supplier:
            supplier.total_purchases = round((supplier.total_purchases or 0) + reversed_value, 2)
    return_record.stock_deducted = False


def revalidate_draft(db: Session, org_id: str, purchase_return: PurchaseReturn) -> None:
    """Re-apply the quantity rule to a saved draft before its stock moves: the draft may
    be old, and receipts or other returns may have changed since it was saved."""
    purchase = purchase_return.purchase
    if purchase is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Source purchase invoice not found")
    validate_and_build_return_items(
        db,
        org_id,
        purchase,
        [
            PurchaseReturnItemCreate(
                purchase_item_id=ri.purchase_item_id,
                product_id=ri.product_id,
                variant_id=ri.variant_id,
                quantity=ri.quantity,
                unit_price=ri.unit_price,
                tax_rate=ri.tax_rate,
            )
            for ri in purchase_return.items
        ],
        exclude_return_id=purchase_return.id,
    )


def _locked_return(db: Session, org_id: str, return_id: str) -> PurchaseReturn:
    """The return row under SELECT ... FOR UPDATE, so two concurrent confirms (a retry,
    a double click) run one after the other and the second sees the first's result."""
    from sqlalchemy.orm import lazyload

    record = (
        db.query(PurchaseReturn)
        .filter(PurchaseReturn.id == return_id, PurchaseReturn.organization_id == org_id)
        .options(lazyload("*"))
        .with_for_update()
        .first()
    )
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Purchase return not found")
    return record


def confirm_purchase_return(
    db: Session,
    org_id: str,
    return_id: str,
    user_id: str | None = None,
) -> PurchaseReturn:
    """Transition: draft -> confirmed. One transaction: the quantity rule and the stock
    rule are re-checked, stock leaves the warehouse, supplier and purchase balances are
    recalculated and the status changes -- all of it or none of it. A repeated confirm
    (retry / double click) is refused with 400 and changes nothing."""
    try:
        purchase_return = _locked_return(db, org_id, return_id)
        if purchase_return.status != "draft":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Cannot confirm purchase return in status '{purchase_return.status}'. Only 'draft' returns can be confirmed.",
            )
        revalidate_draft(db, org_id, purchase_return)
        deduct_inventory_for_return(db, org_id, purchase_return, user_id=user_id)
        purchase_return.status = "confirmed"
        purchase_return.confirmed_at = _now()
        purchase_return.confirmed_by = user_id
        db.flush()
        recalculate_purchase_and_supplier_invoice_returns(db, purchase_return.purchase_id)
        db.commit()
    except Exception:
        db.rollback()
        raise

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

    if purchase_return.status == "draft":
        revalidate_draft(db, org_id, purchase_return)
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


from app.core.sorting import apply_sorting
from sqlalchemy import func, select

_purchase_return_num_subquery = select(
    func.coalesce(PurchaseInvoice.purchase_number, PurchaseInvoice.invoice_number)
).where(PurchaseInvoice.id == PurchaseReturn.purchase_id).scalar_subquery()

PURCHASE_RETURN_SORTABLE_FIELDS = {
    "return_number": PurchaseReturn.return_number,
    "supplier": select(Supplier.name).where(Supplier.id == PurchaseReturn.supplier_id).scalar_subquery(),
    "supplier_name": select(Supplier.name).where(Supplier.id == PurchaseReturn.supplier_id).scalar_subquery(),
    "purchase_number": _purchase_return_num_subquery,
    "purchase": _purchase_return_num_subquery,
    "related_purchase": _purchase_return_num_subquery,
    "related_purchase_number": _purchase_return_num_subquery,
    "purchase_invoice": _purchase_return_num_subquery,
    "purchase_invoice_number": _purchase_return_num_subquery,
    "return_date": PurchaseReturn.return_date,
    "date": PurchaseReturn.return_date,
    "amount": PurchaseReturn.total_amount,
    "total_amount": PurchaseReturn.total_amount,
    "status": PurchaseReturn.status,
    "created_at": PurchaseReturn.created_at,
}


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
    sort_by: str | None = None,
    sort_order: str | None = None,
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
    q = apply_sorting(
        q,
        sort_by=sort_by,
        sort_order=sort_order,
        allowed_fields=PURCHASE_RETURN_SORTABLE_FIELDS,
        default_order=[PurchaseReturn.created_at.desc(), PurchaseReturn.id.desc()],
        secondary_key=PurchaseReturn.id.desc(),
    )

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
