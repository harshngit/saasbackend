from datetime import datetime, timezone
from fastapi import HTTPException, status
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.grn import GoodsReceiptNote, GoodsReceiptNoteItem
from app.models.product import Product, ProductVariant
from app.models.purchase_invoice import PurchaseInvoice, PurchaseInvoiceItem
from app.models.supplier_invoice import SupplierInvoice, SupplierInvoiceItem
from app.schemas.supplier_invoice import SupplierInvoiceItemIn


def build_and_calculate_invoice_items(
    db: Session,
    org_id: str,
    purchase: PurchaseInvoice,
    items_in: list[SupplierInvoiceItemIn],
) -> tuple[list[SupplierInvoiceItem], float, float, float, float]:
    """Build SupplierInvoiceItem instances and compute financial totals server-side."""
    if not items_in:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Supplier invoice must contain at least one line item",
        )

    built_items: list[SupplierInvoiceItem] = []
    subtotal = 0.0
    item_taxes = 0.0
    item_discounts = 0.0

    for it in items_in:
        if it.billed_qty <= 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Billed quantity must be greater than zero",
            )
        if it.unit_price < 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Unit price cannot be negative",
            )

        p_item = db.get(PurchaseInvoiceItem, it.purchase_item_id)
        if p_item is None or p_item.invoice_id != purchase.id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid purchase_item_id '{it.purchase_item_id}' for this purchase order",
            )

        prod_id = it.product_id or p_item.product_id
        if prod_id:
            product = db.get(Product, prod_id)
            if product is None or product.organization_id != org_id:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Item product does not belong to your organization",
                )

        var_id = it.variant_id or p_item.variant_id
        if var_id:
            variant = db.get(ProductVariant, var_id)
            if variant is None:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Item variant is invalid",
                )

        line_subtotal = round(it.billed_qty * it.unit_price, 2)
        tax_amt = round(it.tax_amount, 2)
        disc_amt = round(it.discount_amount, 2)
        line_tot = round(line_subtotal + tax_amt - disc_amt, 2)

        subtotal += line_subtotal
        item_taxes += tax_amt
        item_discounts += disc_amt

        built_items.append(
            SupplierInvoiceItem(
                purchase_item_id=p_item.id,
                product_id=prod_id,
                variant_id=var_id,
                description=it.description or p_item.product_name,
                billed_qty=it.billed_qty,
                unit_price=round(it.unit_price, 2),
                tax_rate=it.tax_rate,
                tax_amount=tax_amt,
                discount_amount=disc_amt,
                line_total=line_tot,
            )
        )

    subtotal = round(subtotal, 2)
    grand_total = round(subtotal + item_taxes - item_discounts, 2)
    return built_items, subtotal, item_taxes, item_discounts, grand_total


def record_supplier_invoice(
    db: Session,
    invoice: SupplierInvoice,
    org_id: str,
    user_id: str,
) -> SupplierInvoice:
    """Record & commit Supplier Invoice with 3-way matching and concurrency locking. ZERO stock movement."""
    if invoice.status == "recorded":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Supplier invoice is already recorded",
        )
    if invoice.status == "cancelled":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot record a cancelled supplier invoice",
        )

    # Lock PurchaseInvoice header row via .with_for_update() to serialize concurrent invoice recording
    purchase = (
        db.query(PurchaseInvoice)
        .filter(
            PurchaseInvoice.id == invoice.purchase_id,
            PurchaseInvoice.organization_id == org_id,
        )
        .with_for_update()
        .first()
    )
    if purchase is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Linked purchase order not found",
        )

    # Purchase eligibility checks: must be confirmed or closed
    if purchase.status in ("draft", "pending"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot record invoice against purchase in '{purchase.status}' status. Purchase must be confirmed.",
        )
    if purchase.status == "cancelled":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot record invoice against a cancelled purchase order",
        )

    # Supplier mismatch guard
    if invoice.supplier_id != purchase.supplier_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invoice supplier does not match the purchase order supplier",
        )

    # 3-Way Matching & Over-invoicing Protection Loop
    all_matched = True

    for item in invoice.items:
        p_item = db.get(PurchaseInvoiceItem, item.purchase_item_id)
        if p_item is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid purchase item link on invoice item",
            )

        # 1. Query total confirmed accepted quantity from GRNs for this purchase item
        confirmed_accepted = (
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

        # 2. Query previously recorded billed quantity from recorded/disputed invoices for this purchase item
        previously_invoiced = (
            db.query(func.coalesce(func.sum(SupplierInvoiceItem.billed_qty), 0))
            .join(SupplierInvoice, SupplierInvoice.id == SupplierInvoiceItem.supplier_invoice_id)
            .filter(
                SupplierInvoice.organization_id == org_id,
                SupplierInvoice.purchase_id == purchase.id,
                SupplierInvoice.status.in_(["recorded", "disputed"]),
                SupplierInvoice.id != invoice.id,
                SupplierInvoiceItem.purchase_item_id == p_item.id,
            )
            .scalar()
            or 0
        )

        total_requested = previously_invoiced + item.billed_qty
        if total_requested > confirmed_accepted:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"Cumulative billed quantity ({total_requested}) exceeds confirmed GRN accepted quantity "
                    f"({confirmed_accepted}) for product '{p_item.product_name}'"
                ),
            )

        # 3. Evaluate 3-way match (Quantity & Price)
        available_accepted = max(confirmed_accepted - previously_invoiced, 0)
        qty_match = (item.billed_qty <= available_accepted)
        price_match = (abs(item.unit_price - p_item.purchase_price) < 0.01)

        if not (qty_match and price_match):
            all_matched = False

    # Set statuses
    invoice.status = "recorded"
    invoice.verification_status = "matched" if all_matched else "mismatched"
    invoice.recorded_at = datetime.now(timezone.utc)
    invoice.recorded_by = user_id

    # CRITICAL INVENTORY RULE: ZERO Stock Movements
    db.commit()
    db.refresh(invoice)
    return invoice
