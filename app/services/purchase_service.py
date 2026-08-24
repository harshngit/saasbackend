from datetime import datetime, timezone
from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.models.product import Product, ProductVariant
from app.models.purchase_invoice import PurchaseInvoice, PurchaseInvoiceItem
from app.models.supplier import Supplier
from app.models.warehouse import Warehouse
from app.schemas.purchase import PurchaseItemIn


def validate_supplier(db: Session, org_id: str, supplier_id: str | None) -> Supplier | None:
    if not supplier_id:
        return None
    supplier = db.get(Supplier, supplier_id)
    if supplier is None or supplier.organization_id != org_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="supplier_id is not a supplier in your firm",
        )
    return supplier


def validate_warehouse(db: Session, org_id: str, warehouse_id: str | None) -> Warehouse | None:
    if not warehouse_id:
        return None
    warehouse = db.get(Warehouse, warehouse_id)
    if warehouse is None or warehouse.organization_id != org_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="warehouse_id is not a warehouse in your firm",
        )
    return warehouse


def build_and_calculate_items(
    db: Session, org_id: str, items_in: list[PurchaseItemIn]
) -> tuple[list[PurchaseInvoiceItem], float, float, float]:
    """Validate each item line, query products/variants, compute line totals, and return:

    (built_items, subtotal, total_item_discounts, total_item_taxes).
    """
    if not items_in:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="At least one purchase item is required",
        )

    built_items: list[PurchaseInvoiceItem] = []
    subtotal = 0.0
    total_item_discounts = 0.0
    total_item_taxes = 0.0

    for it in items_in:
        if it.quantity <= 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Item quantity must be greater than 0",
            )
        if it.purchase_price < 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Item unit cost cannot be negative",
            )
        if it.discount_percent < 0 or it.discount_percent > 100:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Item discount percentage must be between 0 and 100",
            )
        if it.tax_rate < 0 or it.tax_rate > 100:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Item tax rate must be between 0 and 100",
            )

        product = db.get(Product, it.product_id)
        if product is None or product.organization_id != org_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="An item's product is not in your firm",
            )

        variant = None
        if it.variant_id:
            variant = db.get(ProductVariant, it.variant_id)
            if variant is None or variant.product_id != product.id:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="An item's variant is invalid",
                )

        if it.warehouse_id:
            wh = db.get(Warehouse, it.warehouse_id)
            if wh is None or wh.organization_id != org_id:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="An item's warehouse is not in your firm",
                )

        line_subtotal = round(it.purchase_price * it.quantity, 2)

        # Discount calculation (prefer discount_percent if provided, else explicit discount)
        if it.discount_percent > 0:
            discount_amount = round(line_subtotal * (it.discount_percent / 100.0), 2)
        else:
            discount_amount = round(it.discount or 0.0, 2)

        taxable_amount = round(max(line_subtotal - discount_amount, 0.0), 2)

        # Tax calculation (prefer tax_rate if provided, else explicit tax)
        if it.tax_rate > 0:
            tax_amount = round(taxable_amount * (it.tax_rate / 100.0), 2)
        else:
            tax_amount = round(it.tax or 0.0, 2)

        line_total = round(taxable_amount + tax_amount, 2)

        subtotal += line_subtotal
        total_item_discounts += discount_amount
        total_item_taxes += tax_amount

        item_name = it.description or (
            product.name if not variant else f"{product.name} ({variant.name})"
        )
        item_code = it.product_code or (variant.sku if variant and variant.sku else product.sku)
        item_barcode = it.barcode or (
            variant.barcode if variant and variant.barcode else product.barcode
        )
        item_uom = it.unit_of_measure_uom or product.uom

        built_items.append(
            PurchaseInvoiceItem(
                product_id=product.id,
                variant_id=it.variant_id,
                product_code=item_code,
                barcode=item_barcode,
                description=it.description or item_name,
                warehouse_id=it.warehouse_id,
                product_name=item_name,
                quantity=it.quantity,
                purchase_price=it.purchase_price,
                discount_percent=it.discount_percent,
                discount=discount_amount,
                tax_rate=it.tax_rate,
                tax=tax_amount,
                line_total=line_total,
                unit_of_measure_uom=item_uom,
                batch_number=it.batch_number,
                serial_numbers=list(it.serial_numbers or []),
                expiry_date=it.expiry_date,
            )
        )

    return (
        built_items,
        round(subtotal, 2),
        round(total_item_discounts, 2),
        round(total_item_taxes, 2),
    )


def calculate_header_totals(
    subtotal: float,
    item_discounts: float,
    item_taxes: float,
    overall_discount: float = 0.0,
    header_tax: float = 0.0,
    freight_charges: float = 0.0,
    packing_charges: float = 0.0,
    insurance_charges: float = 0.0,
    other_charges: float = 0.0,
    round_off: float = 0.0,
) -> tuple[float, float, float, float]:
    """Calculate (net_subtotal, effective_discount, effective_tax, grand_total)."""
    # Negative charges check
    if any(c < 0 for c in (freight_charges, packing_charges, insurance_charges, other_charges)):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Additional charges cannot be negative",
        )
    if overall_discount < 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Overall discount cannot be negative",
        )

    effective_discount = round(overall_discount if overall_discount > 0 else item_discounts, 2)
    effective_tax = round(header_tax if header_tax > 0 else item_taxes, 2)

    grand_total = round(
        subtotal
        - effective_discount
        + effective_tax
        + freight_charges
        + packing_charges
        + insurance_charges
        + other_charges
        + round_off,
        2,
    )
    return round(subtotal, 2), effective_discount, effective_tax, grand_total
