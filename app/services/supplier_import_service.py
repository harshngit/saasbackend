from typing import Any
from sqlalchemy.orm import Session
from pydantic import ValidationError

from app.core.excel_import import ImportSummaryOut, RowError, generate_xlsx_template, parse_spreadsheet_rows
from app.models import Supplier, User
from app.schemas.supplier import SupplierCreate

SUPPLIER_COLUMNS: list[str] = [
    "supplier_name",
    "contact_person",
    "phone",
    "email",
    "gst_number",
    "company_name",
    "address",
    "city",
    "payment_terms",
    "category",
]

SUPPLIER_EXAMPLE_ROW: list[Any] = [
    "Apex Supplies Ltd",
    "Rajesh Kumar",
    "9876543210",
    "vendor@apexsupplies.com",
    "27AAAAA0000A1Z5",
    "Apex Group",
    "Plot 12, MIDC Industrial Area",
    "Mumbai",
    "net_30",
    "Raw Materials",
]


def get_supplier_template() -> bytes:
    return generate_xlsx_template(SUPPLIER_COLUMNS, SUPPLIER_EXAMPLE_ROW)


def import_suppliers_from_file(
    db: Session, org_id: str, user: User, content: bytes, filename: str
) -> ImportSummaryOut:
    rows, parse_errors = parse_spreadsheet_rows(
        content,
        filename,
        required_headers=None,  # We allow either 'supplier_name' or 'name'
    )
    if parse_errors:
        summary = ImportSummaryOut(total_rows=len(rows), error_count=len(parse_errors), errors=parse_errors)
        summary.sync_counts()
        return summary

    summary = ImportSummaryOut(total_rows=len(rows))

    for row in rows:
        row_num = row.get("_row_number", 0)
        s_name = row.get("supplier_name") or row.get("name")
        if not s_name or not str(s_name).strip():
            summary.errors.append(
                RowError(row=row_num, column="supplier_name", field="supplier_name", message="supplier_name is required")
            )
            continue

        raw_phone = row.get("phone") or row.get("mobile_number")
        raw_email = row.get("email") or row.get("email_address")
        raw_gst = row.get("gst_number") or row.get("gstin") or row.get("gstin_tax_id")
        raw_category = row.get("category") or row.get("categories")

        payload_dict = {
            "name": str(s_name).strip(),
            "contact_person": str(row["contact_person"]).strip() if row.get("contact_person") else None,
            "phone": str(raw_phone).strip() if raw_phone else None,
            "email": str(raw_email).strip() if raw_email else None,
            "gst_number": str(raw_gst).strip() if raw_gst else None,
            "company_name": str(row["company_name"]).strip() if row.get("company_name") else None,
            "address": str(row["address"]).strip() if row.get("address") else None,
            "city": str(row["city"]).strip() if row.get("city") else None,
            "payment_terms": str(row["payment_terms"]).strip() if row.get("payment_terms") else None,
            "category": str(raw_category).strip() if raw_category else None,
        }

        try:
            supplier_in = SupplierCreate.model_validate(payload_dict)
            data = supplier_in.model_dump(exclude_unset=True)
        except ValidationError as val_err:
            for err in val_err.errors():
                loc = ".".join(str(e) for e in err.get("loc", []))
                summary.errors.append(
                    RowError(
                        row=row_num,
                        column=loc or "supplier_name",
                        field=loc or "supplier_name",
                        value=payload_dict.get(loc),
                        message=err.get("msg", "Validation error"),
                    )
                )
            continue
        except Exception as exc:
            summary.errors.append(RowError(row=row_num, message=f"Row validation failed: {exc}"))
            continue

        try:
            supplier_cats = data.pop("supplier_categories", None)
            if supplier_cats is not None:
                data["categories"] = supplier_cats
                if supplier_cats and not data.get("category"):
                    data["category"] = supplier_cats[0]
            elif data.get("category") and not data.get("categories"):
                data["categories"] = [data["category"]]

            supplier = Supplier(organization_id=org_id, **data)
            db.add(supplier)
            db.flush()
            summary.created_ids.append(supplier.id)
            summary.success_count += 1
        except Exception as exc:
            summary.errors.append(RowError(row=row_num, message=f"Failed to create supplier: {exc}"))

    summary.sync_counts()
    if summary.success_count > 0:
        db.commit()
    else:
        db.rollback()

    return summary
