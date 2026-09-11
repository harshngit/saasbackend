from typing import Any
from sqlalchemy.orm import Session
from pydantic import ValidationError

from app.core import scoping
from app.core.excel_import import ImportSummaryOut, RowError, generate_xlsx_template, parse_spreadsheet_rows
from app.core.reference_data import CUSTOMER_TYPES
from app.models import Customer, User
from app.schemas.customer_profile import CustomerProfileIn
from app.services import numbering_service

CUSTOMER_COLUMNS: list[str] = [
    "customer_name",
    "customer_type",
    "mobile_number",
    "email_address",
    "gstin_tax_id",
    "primary_contact_person",
    "shipping_address",
    "city",
    "payment_terms",
    "credit_limit",
]

CUSTOMER_EXAMPLE_ROW: list[Any] = [
    "Apex Enterprises",
    "business",
    "9876543210",
    "contact@apex.com",
    "27AAAAA0000A1Z5",
    "Rahul Sharma",
    "Plot 45, MIDC Industrial Area",
    "Mumbai",
    "net_30",
    50000.0,
]


def get_customer_template() -> bytes:
    return generate_xlsx_template(CUSTOMER_COLUMNS, CUSTOMER_EXAMPLE_ROW)


def import_customers_from_file(
    db: Session, org_id: str, user: User, content: bytes, filename: str
) -> ImportSummaryOut:
    rows, parse_errors = parse_spreadsheet_rows(content, filename, required_headers=["customer_name"])
    if parse_errors:
        return ImportSummaryOut(total_rows=len(rows), error_count=len(parse_errors), errors=parse_errors)

    summary = ImportSummaryOut(total_rows=len(rows))

    for row in rows:
        row_num = row.get("_row_number", 0)
        c_name = row.get("customer_name")
        if not c_name or not str(c_name).strip():
            summary.errors.append(
                RowError(row=row_num, column="customer_name", message="customer_name is required")
            )
            continue

        c_type = row.get("customer_type")
        if c_type:
            c_type_clean = str(c_type).strip().lower()
            matched_type = next((t for t in CUSTOMER_TYPES if t.lower() == c_type_clean), None)
            if not matched_type:
                summary.errors.append(
                    RowError(
                        row=row_num,
                        column="customer_type",
                        value=c_type,
                        message=f"Invalid customer_type '{c_type}'. Allowed values: {', '.join(CUSTOMER_TYPES)}",
                    )
                )
                continue
            c_type = matched_type

        credit_limit_val = 0.0
        raw_credit = row.get("credit_limit")
        if raw_credit is not None and str(raw_credit).strip() != "":
            try:
                credit_limit_val = float(str(raw_credit).replace(",", "").strip())
                if credit_limit_val < 0:
                    summary.errors.append(
                        RowError(
                            row=row_num,
                            column="credit_limit",
                            value=raw_credit,
                            message="credit_limit must be greater than or equal to 0",
                        )
                    )
                    continue
            except ValueError:
                summary.errors.append(
                    RowError(
                        row=row_num,
                        column="credit_limit",
                        value=raw_credit,
                        message=f"Invalid numeric value for credit_limit: '{raw_credit}'",
                    )
                )
                continue

        flat_payload = {
            "customer_name": str(c_name).strip(),
            "customer_type": c_type,
            "mobile_number": str(row["mobile_number"]).strip() if row.get("mobile_number") else None,
            "email_address": str(row["email_address"]).strip() if row.get("email_address") else None,
            "gstin_tax_id": str(row["gstin_tax_id"]).strip() if row.get("gstin_tax_id") else None,
            "primary_contact_person": str(row["primary_contact_person"]).strip()
            if row.get("primary_contact_person")
            else None,
            "shipping_address": str(row["shipping_address"]).strip() if row.get("shipping_address") else None,
            "city": str(row["city"]).strip() if row.get("city") else None,
            "payment_terms": str(row["payment_terms"]).strip() if row.get("payment_terms") else None,
            "credit_limit": credit_limit_val,
        }

        try:
            profile_in = CustomerProfileIn.model_validate(flat_payload)
            data = profile_in.to_columns()
        except ValidationError as val_err:
            for err in val_err.errors():
                loc = ".".join(str(e) for e in err.get("loc", []))
                summary.errors.append(
                    RowError(
                        row=row_num,
                        column=loc or "customer",
                        message=err.get("msg", "Validation error"),
                    )
                )
            continue
        except Exception as exc:
            summary.errors.append(RowError(row=row_num, message=f"Row validation failed: {exc}"))
            continue

        try:
            data["customer_id"] = numbering_service.next_number(
                db, org_id, Customer.customer_id, "CUST"
            )
            if not data.get("assigned_sales_officer_id") and scoping.scope_to_own(db, user):
                data["assigned_sales_officer_id"] = user.id

            customer = Customer(organization_id=org_id, **data)
            customer.recompute_outstanding()
            db.add(customer)
            db.flush()
            summary.created_ids.append(customer.id)
            summary.success_count += 1
        except Exception as exc:
            summary.errors.append(RowError(row=row_num, message=f"Failed to create customer: {exc}"))

    summary.total_records = summary.success_count
    summary.error_count = len(summary.errors)
    if summary.success_count > 0:
        db.commit()
    else:
        db.rollback()

    return summary
