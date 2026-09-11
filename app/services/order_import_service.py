from collections import defaultdict
from datetime import datetime, timezone
from typing import Any
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.core import scoping
from app.core.excel_import import ImportSummaryOut, RowError, generate_xlsx_template, parse_spreadsheet_rows
from app.models import Customer, Product, ProductVariant, User
from app.services import lookup_service, order_service

ORDER_COLUMNS: list[str] = [
    "order_group_id",
    "customer_id",
    "order_date",
    "delivery_method",
    "delivery_address",
    "payment_type",
    "product_id",
    "variant_id",
    "quantity",
    "unit_price",
]

ORDER_EXAMPLE_ROW: list[Any] = [
    "ORD-001",
    "CUST-001",
    "2026-09-11",
    "takeaway",
    None,
    "credit",
    "PROD-001",
    None,
    2,
    150.0,
]


def get_order_template() -> bytes:
    return generate_xlsx_template(ORDER_COLUMNS, ORDER_EXAMPLE_ROW)


def _resolve_customer(db: Session, org_id: str, ident: str) -> Customer | None:
    return lookup_service.by_id_or_code(
        db, Customer, ident, org_id, Customer.customer_id, Customer.name, Customer.business_name, Customer.phone, Customer.email
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


def import_orders_from_file(
    db: Session, org_id: str, user: User, content: bytes, filename: str
) -> ImportSummaryOut:
    required_hdrs = ["order_group_id", "customer_id", "product_id", "quantity"]
    rows, parse_errors = parse_spreadsheet_rows(content, filename, required_headers=required_hdrs)
    if parse_errors:
        return ImportSummaryOut(total_rows=len(rows), error_count=len(parse_errors), errors=parse_errors)

    summary = ImportSummaryOut(total_rows=len(rows))

    # Group rows by order_group_id
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    group_first_row: dict[str, int] = {}

    for row in rows:
        row_num = row.get("_row_number", 0)
        group_id = row.get("order_group_id")
        cust_ident = row.get("customer_id")

        if not group_id or not str(group_id).strip():
            summary.errors.append(
                RowError(row=row_num, column="order_group_id", message="order_group_id is required")
            )
            continue

        if not cust_ident or not str(cust_ident).strip():
            summary.errors.append(
                RowError(row=row_num, column="customer_id", message="customer_id is required")
            )
            continue

        gid = str(group_id).strip()
        if gid not in group_first_row:
            group_first_row[gid] = row_num
        grouped[gid].append(row)

    # Process each grouped order
    for group_id, item_rows in grouped.items():
        first_row_num = group_first_row[group_id]
        first_row = item_rows[0]
        cust_ident_first = str(first_row.get("customer_id", "")).strip()

        customer = _resolve_customer(db, org_id, cust_ident_first)
        if customer is None:
            summary.errors.append(
                RowError(
                    row=first_row_num,
                    column="customer_id",
                    value=cust_ident_first,
                    message=f"Customer '{cust_ident_first}' was not found in your organization",
                )
            )
            continue

        # Header consistency check
        header_conflict = False
        for subsequent_row in item_rows[1:]:
            s_row_num = subsequent_row.get("_row_number", 0)
            sub_cust = str(subsequent_row.get("customer_id", "")).strip()
            if sub_cust.lower() != cust_ident_first.lower():
                summary.errors.append(
                    RowError(
                        row=s_row_num,
                        column="customer_id",
                        value=sub_cust,
                        message=f"Conflicting customer_id '{sub_cust}' within order_group_id '{group_id}'. Must match row {first_row_num} ('{cust_ident_first}')",
                    )
                )
                header_conflict = True

        if header_conflict:
            continue

        # Delivery method & address validation
        raw_del_method = first_row.get("delivery_method")
        del_method = str(raw_del_method).strip().lower() if raw_del_method else "takeaway"
        if del_method not in ("takeaway", "home_delivery", "pickup", "delivery"):
            summary.errors.append(
                RowError(
                    row=first_row_num,
                    column="delivery_method",
                    value=raw_del_method,
                    message=f"Invalid delivery_method '{raw_del_method}'. Allowed values: takeaway, home_delivery",
                )
            )
            continue

        fulfilment_method = "pickup" if del_method in ("takeaway", "pickup") else "delivery"
        del_address = first_row.get("delivery_address")
        resolved_address = str(del_address).strip() if del_address else (customer.delivery_address or customer.billing_address)

        if fulfilment_method == "delivery" and (not resolved_address or not str(resolved_address).strip()):
            summary.errors.append(
                RowError(
                    row=first_row_num,
                    column="delivery_address",
                    message="delivery_address is required for home delivery orders (no address found on customer profile)",
                )
            )
            continue

        target_order_date: datetime | None = None
        raw_order_date = first_row.get("order_date")
        if raw_order_date and str(raw_order_date).strip():
            try:
                target_order_date = datetime.fromisoformat(str(raw_order_date).replace("Z", "+00:00"))
            except Exception:
                target_order_date = None

        payment_type = str(first_row["payment_type"]).strip().lower() if first_row.get("payment_type") else "cash"

        # Build order line items
        lines: list[order_service.OrderLine] = []
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

            raw_price = r.get("unit_price")
            unit_price = None
            if raw_price is not None and str(raw_price).strip() != "":
                try:
                    p_val = float(str(raw_price).replace(",", "").strip())
                    if p_val < 0:
                        summary.errors.append(
                            RowError(row=r_num, column="unit_price", value=raw_price, message="unit_price cannot be negative")
                        )
                        item_has_error = True
                        continue
                    unit_price = p_val
                except (ValueError, TypeError):
                    summary.errors.append(
                        RowError(row=r_num, column="unit_price", value=raw_price, message=f"Invalid numeric value for unit_price: '{raw_price}'")
                    )
                    item_has_error = True
                    continue

            lines.append(
                order_service.OrderLine(
                    product_id=product.id,
                    variant_id=var_id,
                    quantity=qty,
                    unit_price=unit_price,
                )
            )

        if item_has_error or not lines:
            continue

        # Place the order via canonical order service
        try:
            salesperson_id = user.id if scoping.scope_to_own(db, user) else None
            order, warnings = order_service.place_order(
                db=db,
                user=user,
                customer=customer,
                lines=lines,
                order_date=target_order_date or datetime.now(timezone.utc),
                fulfilment_method=fulfilment_method,
                payment_type=payment_type,
                salesperson_id=salesperson_id,
                source="direct",
                create_as_draft=True,
                billing_address=customer.billing_address,
                shipping_address=resolved_address,
                delivery_address=resolved_address,
                currency="INR",
            )
            db.flush()
            summary.created_ids.append(order.id)
            summary.success_count += 1
        except Exception as exc:
            summary.errors.append(
                RowError(row=first_row_num, message=f"Failed to place order '{group_id}': {exc}")
            )

    summary.total_records = summary.success_count
    summary.error_count = len(summary.errors)
    if summary.success_count > 0:
        db.commit()
    else:
        db.rollback()

    return summary
