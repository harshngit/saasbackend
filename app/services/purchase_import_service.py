from collections import defaultdict
from datetime import datetime, timezone
from typing import Any
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.core.excel_import import ImportSummaryOut, RowError, generate_xlsx_template, parse_spreadsheet_rows
from app.models import Product, ProductVariant, PurchaseInvoice, Supplier, User, Warehouse
from app.schemas.purchase import PurchaseItemIn
from app.services import lookup_service, numbering_service, purchase_service

PURCHASE_COLUMNS: list[str] = [
    "invoice_number",
    "supplier_id",
    "invoice_date",
    "warehouse_id",
    "product_id",
    "variant_id",
    "quantity",
    "purchase_price",
    "item_tax_rate",
    "batch_number",
]

PURCHASE_EXAMPLE_ROW: list[Any] = [
    "BILL-2026-001",
    "SUP-001",
    "2026-09-11",
    "WH-MAIN",
    "PROD-001",
    None,
    10,
    100.0,
    18.0,
    "BATCH-A",
]


def get_purchase_template() -> bytes:
    return generate_xlsx_template(PURCHASE_COLUMNS, PURCHASE_EXAMPLE_ROW)


def _resolve_supplier(db: Session, org_id: str, ident: str) -> Supplier | None:
    return lookup_service.by_id_or_code(
        db, Supplier, ident, org_id, Supplier.name, Supplier.company_name, Supplier.gst_number, Supplier.phone
    )


def _resolve_warehouse(db: Session, org_id: str, ident: str) -> Warehouse | None:
    return lookup_service.by_id_or_code(
        db, Warehouse, ident, org_id, Warehouse.code, Warehouse.name
    )


def _resolve_product(db: Session, org_id: str, ident: str) -> Product | None:
    return lookup_service.by_id_or_code(
        db, Product, ident, org_id, Product.product_id, Product.sku, Product.barcode, Product.name
    )


def _resolve_variant(db: Session, org_id: str, ident: str, product_id: str) -> ProductVariant | None:
    if not ident:
        return None
    if lookup_service.looks_like_uuid(ident):
        var = db.get(ProductVariant, ident)
        if var and var.product_id == product_id:
            return var
    return (
        db.query(ProductVariant)
        .filter(
            ProductVariant.product_id == product_id,
            or_(
                ProductVariant.sku.ilike(ident),
                ProductVariant.barcode.ilike(ident),
                ProductVariant.name.ilike(ident),
            ),
        )
        .first()
    )


def import_purchases_from_file(
    db: Session, org_id: str, user: User, content: bytes, filename: str
) -> ImportSummaryOut:
    required_hdrs = ["invoice_number", "supplier_id", "product_id", "quantity", "purchase_price"]
    rows, parse_errors = parse_spreadsheet_rows(content, filename, required_headers=required_hdrs)
    if parse_errors:
        return ImportSummaryOut(total_rows=len(rows), error_count=len(parse_errors), errors=parse_errors)

    summary = ImportSummaryOut(total_rows=len(rows))

    # Group rows by (invoice_number, supplier_id)
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    group_first_row: dict[tuple[str, str], int] = {}

    for row in rows:
        row_num = row.get("_row_number", 0)
        inv_no = row.get("invoice_number")
        supp_raw = row.get("supplier_id")

        if not inv_no or not str(inv_no).strip():
            summary.errors.append(
                RowError(row=row_num, column="invoice_number", message="invoice_number is required")
            )
            continue

        if not supp_raw or not str(supp_raw).strip():
            summary.errors.append(
                RowError(row=row_num, column="supplier_id", message="supplier_id is required")
            )
            continue

        key = (str(inv_no).strip(), str(supp_raw).strip())
        if key not in group_first_row:
            group_first_row[key] = row_num
        grouped[key].append(row)

    # Process each grouped purchase
    for (inv_no, supp_ident), item_rows in grouped.items():
        first_row_num = group_first_row[(inv_no, supp_ident)]
        first_row = item_rows[0]

        # Resolve supplier
        supplier = _resolve_supplier(db, org_id, supp_ident)
        if supplier is None:
            summary.errors.append(
                RowError(
                    row=first_row_num,
                    column="supplier_id",
                    value=supp_ident,
                    message=f"Supplier '{supp_ident}' was not found in your organization",
                )
            )
            continue

        if not supplier.is_active:
            summary.errors.append(
                RowError(
                    row=first_row_num,
                    column="supplier_id",
                    value=supp_ident,
                    message=f"Supplier '{supplier.name}' is inactive and cannot be used for new purchases",
                )
            )
            continue

        # Header consistency check
        header_conflict = False
        target_wh_id: str | None = None
        target_inv_date: datetime | None = None

        raw_wh_first = first_row.get("warehouse_id")
        if raw_wh_first and str(raw_wh_first).strip():
            wh = _resolve_warehouse(db, org_id, str(raw_wh_first).strip())
            if wh is None:
                summary.errors.append(
                    RowError(
                        row=first_row_num,
                        column="warehouse_id",
                        value=raw_wh_first,
                        message=f"Warehouse '{raw_wh_first}' was not found in your organization",
                    )
                )
                header_conflict = True
            elif not wh.is_active:
                summary.errors.append(
                    RowError(
                        row=first_row_num,
                        column="warehouse_id",
                        value=raw_wh_first,
                        message=f"Warehouse '{wh.name}' is inactive",
                    )
                )
                header_conflict = True
            else:
                target_wh_id = wh.id

        raw_date_first = first_row.get("invoice_date")
        if raw_date_first and str(raw_date_first).strip():
            try:
                target_inv_date = datetime.fromisoformat(str(raw_date_first).replace("Z", "+00:00"))
            except Exception:
                target_inv_date = None

        # Verify no conflicting header values in subsequent rows of the same group
        for subsequent_row in item_rows[1:]:
            s_row_num = subsequent_row.get("_row_number", 0)
            sub_wh = subsequent_row.get("warehouse_id")
            if sub_wh and raw_wh_first and str(sub_wh).strip().lower() != str(raw_wh_first).strip().lower():
                summary.errors.append(
                    RowError(
                        row=s_row_num,
                        column="warehouse_id",
                        value=sub_wh,
                        message=f"Conflicting warehouse_id '{sub_wh}' within purchase invoice group '{inv_no}'. Must match row {first_row_num} ('{raw_wh_first}')",
                    )
                )
                header_conflict = True

        if header_conflict:
            continue

        # Build item lines
        items_in: list[PurchaseItemIn] = []
        item_has_error = False

        for r in item_rows:
            r_num = r.get("_row_number", 0)
            prod_ident = r.get("product_id")
            if not prod_ident or not str(prod_ident).strip():
                summary.errors.append(
                    RowError(row=r_num, column="product_id", message="product_id is required")
                )
                item_has_error = True
                continue

            product = _resolve_product(db, org_id, str(prod_ident).strip())
            if product is None:
                summary.errors.append(
                    RowError(
                        row=r_num,
                        column="product_id",
                        value=prod_ident,
                        message=f"Product '{prod_ident}' was not found in your organization",
                    )
                )
                item_has_error = True
                continue

            var_id = None
            var_ident = r.get("variant_id")
            if var_ident and str(var_ident).strip():
                variant = _resolve_variant(db, org_id, str(var_ident).strip(), product.id)
                if variant is None:
                    summary.errors.append(
                        RowError(
                            row=r_num,
                            column="variant_id",
                            value=var_ident,
                            message=f"Variant '{var_ident}' was not found for product '{product.name}'",
                        )
                    )
                    item_has_error = True
                    continue
                var_id = variant.id

            raw_qty = r.get("quantity")
            try:
                qty = int(raw_qty) if raw_qty is not None else 0
                if qty <= 0:
                    summary.errors.append(
                        RowError(row=r_num, column="quantity", value=raw_qty, message="quantity must be greater than 0")
                    )
                    item_has_error = True
                    continue
            except (ValueError, TypeError):
                summary.errors.append(
                    RowError(row=r_num, column="quantity", value=raw_qty, message=f"Invalid integer for quantity: '{raw_qty}'")
                )
                item_has_error = True
                continue

            raw_price = r.get("purchase_price")
            try:
                price = float(str(raw_price).replace(",", "").strip()) if raw_price is not None else -1.0
                if price < 0:
                    summary.errors.append(
                        RowError(row=r_num, column="purchase_price", value=raw_price, message="purchase_price cannot be negative")
                    )
                    item_has_error = True
                    continue
            except (ValueError, TypeError):
                summary.errors.append(
                    RowError(row=r_num, column="purchase_price", value=raw_price, message=f"Invalid numeric value for purchase_price: '{raw_price}'")
                )
                item_has_error = True
                continue

            raw_tax = r.get("item_tax_rate")
            tax_rate = 0.0
            if raw_tax is not None and str(raw_tax).strip() != "":
                try:
                    tax_rate = float(str(raw_tax).replace("%", "").strip())
                    if tax_rate < 0 or tax_rate > 100:
                        summary.errors.append(
                            RowError(row=r_num, column="item_tax_rate", value=raw_tax, message="item_tax_rate must be between 0 and 100")
                        )
                        item_has_error = True
                        continue
                except (ValueError, TypeError):
                    summary.errors.append(
                        RowError(row=r_num, column="item_tax_rate", value=raw_tax, message=f"Invalid numeric value for item_tax_rate: '{raw_tax}'")
                    )
                    item_has_error = True
                    continue

            batch_no = str(r["batch_number"]).strip() if r.get("batch_number") else None

            items_in.append(
                PurchaseItemIn(
                    product_id=product.id,
                    variant_id=var_id,
                    quantity=qty,
                    purchase_price=price,
                    tax_rate=tax_rate,
                    batch_number=batch_no,
                )
            )

        if item_has_error or not items_in:
            continue

        try:
            built_items, subtotal, item_discounts, item_taxes = purchase_service.build_and_calculate_items(
                db, org_id, items_in
            )
            net_subtotal, effective_discount, effective_tax, grand_total = purchase_service.calculate_header_totals(
                subtotal=subtotal,
                item_discounts=item_discounts,
                item_taxes=item_taxes,
                overall_discount=0.0,
                header_tax=0.0,
            )

            inv = PurchaseInvoice(
                organization_id=org_id,
                invoice_number=inv_no,
                supplier_id=supplier.id,
                invoice_date=target_inv_date or datetime.now(timezone.utc),
                status="draft",
                payment_status="unpaid",
                subtotal=net_subtotal,
                discount=effective_discount,
                tax=effective_tax,
                total=grand_total,
                amount_paid=0.0,
                created_by=user.id,
                purchase_id=numbering_service.next_number(db, org_id, PurchaseInvoice.purchase_id, "PURID"),
                purchase_number=numbering_service.next_number(db, org_id, PurchaseInvoice.purchase_number, "PUR"),
                purchase_type="Direct Purchase",
                purchase_date=target_inv_date or datetime.now(timezone.utc),
                purchase_status="Draft",
                reference_number=inv_no,
                contact_person=supplier.contact_person,
                mobile_number=supplier.phone,
                email_address=supplier.email,
                payee_gstin=supplier.gst_number,
                billing_address=supplier.address,
                warehouse_id=target_wh_id,
                receiving_status="not_received",
                approval_status="Pending",
                requested_by=user.id,
            )
            inv.items = built_items
            db.add(inv)
            db.flush()
            summary.created_ids.append(inv.id)
            summary.success_count += 1
        except Exception as exc:
            summary.errors.append(
                RowError(row=first_row_num, message=f"Failed to create purchase invoice '{inv_no}': {exc}")
            )

    summary.total_records = summary.success_count
    summary.error_count = len(summary.errors)
    if summary.success_count > 0:
        db.commit()
    else:
        db.rollback()

    return summary
