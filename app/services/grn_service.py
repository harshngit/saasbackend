from datetime import datetime, timezone
from fastapi import HTTPException, status
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.grn import GoodsReceiptNote, GoodsReceiptNoteItem
from app.models.product import Product, ProductVariant
from app.models.purchase_invoice import PurchaseInvoice, PurchaseInvoiceItem
from app.models.supplier import Supplier
from app.models.warehouse import Warehouse
from app.schemas.grn import GRNItemIn
from app.services import numbering_service, purchase_service, stock_service


def validate_grn_item_quantities(it: GRNItemIn, item_name: str = "Item") -> int:
    """Validate quantity non-negativity and damaged/rejected constraints. Returns calculated accepted_qty."""
    if it.received_qty < 0 or it.damaged_qty < 0 or it.rejected_qty < 0 or it.ordered_qty < 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Quantities cannot be negative for {item_name}",
        )
    if (it.damaged_qty + it.rejected_qty) > it.received_qty:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Damaged plus rejected quantity ({it.damaged_qty + it.rejected_qty}) cannot exceed received quantity ({it.received_qty}) for {item_name}",
        )
    accepted_qty = it.received_qty - it.damaged_qty - it.rejected_qty
    if accepted_qty < 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Accepted quantity cannot be negative for {item_name}",
        )
    return accepted_qty


def build_and_calculate_grn_items(
    db: Session,
    org_id: str,
    purchase: PurchaseInvoice,
    items_in: list[GRNItemIn] | None,
) -> list[GoodsReceiptNoteItem]:
    """Build GoodsReceiptNoteItem objects from input or auto-populate from Purchase items."""
    built_items: list[GoodsReceiptNoteItem] = []

    if not items_in:
        # Auto-populate items from purchase order
        for p_item in purchase.items:
            remaining = max(p_item.quantity - (p_item.received_qty or 0), 0)
            rec_qty = remaining
            built_items.append(
                GoodsReceiptNoteItem(
                    purchase_item_id=p_item.id,
                    product_id=p_item.product_id,
                    variant_id=p_item.variant_id,
                    product_code=p_item.product_code,
                    barcode=p_item.barcode,
                    product_name=p_item.product_name,
                    unit_of_measure_uom=p_item.unit_of_measure_uom,
                    ordered_qty=p_item.quantity,
                    received_qty=rec_qty,
                    damaged_qty=0,
                    rejected_qty=0,
                    accepted_qty=rec_qty,
                    batch_number=p_item.batch_number,
                    serial_numbers=list(p_item.serial_numbers or []),
                    expiry_date=p_item.expiry_date,
                )
            )
        return built_items

    for it in items_in:
        p_item = None
        if it.purchase_item_id:
            p_item = db.get(PurchaseInvoiceItem, it.purchase_item_id)
            if p_item is None or p_item.invoice_id != purchase.id:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Invalid purchase_item_id for this purchase",
                )

        product = None
        prod_id = it.product_id or (p_item.product_id if p_item else None)
        if prod_id:
            product = db.get(Product, prod_id)
            if product is None or product.organization_id != org_id:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Item product is not in your organization",
                )

        variant = None
        var_id = it.variant_id or (p_item.variant_id if p_item else None)
        if var_id:
            variant = db.get(ProductVariant, var_id)
            if variant is None or (product and variant.product_id != product.id):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Item variant is invalid",
                )

        item_name = it.description or (
            p_item.product_name if p_item else (
                product.name if product and not variant else (
                    f"{product.name} ({variant.name})" if product and variant else "Item"
                )
            )
        )
        accepted_qty = validate_grn_item_quantities(it, item_name)

        built_items.append(
            GoodsReceiptNoteItem(
                purchase_item_id=p_item.id if p_item else None,
                product_id=product.id if product else None,
                variant_id=variant.id if variant else None,
                product_code=it.product_code or (p_item.product_code if p_item else (variant.sku if variant else (product.sku if product else None))),
                barcode=it.barcode or (p_item.barcode if p_item else (variant.barcode if variant else (product.barcode if product else None))),
                product_name=item_name,
                unit_of_measure_uom=it.unit_of_measure_uom or (p_item.unit_of_measure_uom if p_item else (product.uom if product else None)),
                ordered_qty=it.ordered_qty or (p_item.quantity if p_item else 0),
                received_qty=it.received_qty,
                damaged_qty=it.damaged_qty,
                rejected_qty=it.rejected_qty,
                accepted_qty=accepted_qty,
                batch_number=it.batch_number or (p_item.batch_number if p_item else None),
                serial_numbers=list(it.serial_numbers or (p_item.serial_numbers if p_item else [])),
                expiry_date=it.expiry_date or (p_item.expiry_date if p_item else None),
                notes=it.notes,
            )
        )

    return built_items


def confirm_grn(db: Session, grn: GoodsReceiptNote, org_id: str, user_id: str) -> GoodsReceiptNote:
    """Atomically confirm GRN: inward accepted_qty to warehouse, update purchase receiving progress."""
    if grn.status == "confirmed":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Goods Receipt Note is already confirmed",
        )
    if grn.status == "cancelled":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot confirm a cancelled GRN",
        )

    purchase = db.get(PurchaseInvoice, grn.purchase_id)
    if purchase is None or purchase.organization_id != org_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Purchase not found")

    if purchase.status in ("draft", "pending"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Purchase order must be confirmed before receiving goods",
        )
    if purchase.status == "cancelled":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot receive goods against a cancelled purchase order",
        )

    # Determine receiving warehouse
    wh_id = grn.warehouse_id or purchase.warehouse_id or stock_service.default_warehouse(db, org_id).id
    warehouse = stock_service.owned_warehouse(db, wh_id, org_id, require_active=True)
    if warehouse is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Receiving warehouse is invalid or inactive",
        )

    # Validate items and check over-receipt against Purchase
    for item in grn.items:
        acc_qty = item.received_qty - item.damaged_qty - item.rejected_qty
        if acc_qty < 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Accepted quantity cannot be negative for {item.product_name}",
            )
        item.accepted_qty = acc_qty

        # Over-receipt protection if linked to purchase item
        if item.purchase_item_id:
            p_item = db.get(PurchaseInvoiceItem, item.purchase_item_id)
            if p_item:
                # Query prior confirmed accepted_qty for this purchase item
                prior_accepted = (
                    db.query(func.coalesce(func.sum(GoodsReceiptNoteItem.accepted_qty), 0))
                    .join(GoodsReceiptNote, GoodsReceiptNote.id == GoodsReceiptNoteItem.grn_id)
                    .filter(
                        GoodsReceiptNote.organization_id == org_id,
                        GoodsReceiptNote.purchase_id == purchase.id,
                        GoodsReceiptNote.status == "confirmed",
                        GoodsReceiptNoteItem.purchase_item_id == p_item.id,
                    )
                    .scalar()
                    or 0
                )
                if (prior_accepted + acc_qty) > p_item.quantity:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=f"Total accepted quantity ({prior_accepted + acc_qty}) exceeds ordered quantity ({p_item.quantity}) for product '{item.product_name}'",
                    )

    # Execute physical stock inwarding ONLY for accepted_qty > 0
    for item in grn.items:
        if not item.product_id or item.accepted_qty <= 0:
            continue
        item_wh_id = item.grn.warehouse_id or warehouse.id
        stock_service.adjust_on_hand(
            db,
            org_id,
            item_wh_id,
            item.product_id,
            item.variant_id,
            float(item.accepted_qty),
            movement_type="purchase_in",
            note=f"GRN {grn.grn_number} (PO {purchase.purchase_number or purchase.invoice_number})",
            created_by=user_id,
            batch={"batch_number": item.batch_number, "expiry_date": item.expiry_date} if item.batch_number else None,
            serial_numbers=item.serial_numbers,
        )

    # Mark GRN Confirmed
    now = datetime.now(timezone.utc)
    grn.status = "confirmed"
    grn.confirmed_at = now
    grn.confirmed_by = user_id
    grn.warehouse_id = warehouse.id

    db.flush()

    # Roll up received quantities to Purchase Order
    for p_item in purchase.items:
        tot_accepted = (
            db.query(func.coalesce(func.sum(GoodsReceiptNoteItem.accepted_qty), 0))
            .join(GoodsReceiptNote, GoodsReceiptNote.id == GoodsReceiptNoteItem.grn_id)
            .filter(
                GoodsReceiptNote.organization_id == org_id,
                GoodsReceiptNote.purchase_id == purchase.id,
                GoodsReceiptNote.status == "confirmed",
                GoodsReceiptNoteItem.purchase_item_id == p_item.id,
            )
            .scalar()
            or 0
        )
        p_item.received_qty = tot_accepted

    total_ordered = sum(i.quantity for i in purchase.items)
    total_received = sum(i.received_qty for i in purchase.items)

    if total_received == 0:
        purchase.receiving_status = "not_received"
    elif total_received >= total_ordered:
        purchase.receiving_status = "fully_received"
    else:
        purchase.receiving_status = "partially_received"

    db.commit()
    db.refresh(grn)
    return grn
