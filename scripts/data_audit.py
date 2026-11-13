"""Read-only delivery data audit. Writes two CSV files and changes nothing.

(A) cancelled_order_open_deliveries.csv
    Deliveries whose sales order is cancelled but whose own status is NOT one of
    cancelled / planned / accepted / ready / rejected -- i.e. goods that were loaded,
    are on the road, or were (partly) delivered or failed under a cancelled order.
    These are reported only: nothing is changed automatically (see the return-flow
    notes in the deploy message).

(B) deliveries_without_customer_or_order.csv
    Deliveries with customer_id IS NULL AND sales_order_id IS NULL. These can be
    repaired with scripts/fix_delivery_customers.py.

Usage (run against a COPY / branch of the database first):
    DATABASE_URL=postgresql://... python scripts/data_audit.py --out-dir audit_out
    python scripts/data_audit.py --out-dir audit_out --organization-id <org uuid>

On PostgreSQL the whole audit runs in a READ ONLY transaction, so even a bug in this
script cannot write. Exit code 0 = done (it does not fail just because rows were found).
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.orm import Session, lazyload  # noqa: E402

from app.models import Customer, Delivery, Organization, SalesOrder  # noqa: E402

# Delivery statuses that are fine under a cancelled order (nothing has left the warehouse).
SAFE_UNDER_CANCELLED_ORDER = ("cancelled", "planned", "accepted", "ready", "rejected")

FILE_A = "cancelled_order_open_deliveries.csv"
FILE_B = "deliveries_without_customer_or_order.csv"

COLUMNS_A = [
    "organization_id", "organization_name", "delivery_id", "delivery_note_number", "delivery_status",
    "picking_status", "sales_order_id", "order_number", "order_status", "order_fulfilment_status",
    "customer_id", "customer_number", "delivery_partner_id", "scheduled_date", "dispatched_at", "created_at",
]
COLUMNS_B = [
    "organization_id", "organization_name", "delivery_id", "delivery_note_number", "status",
    "created_at", "scheduled_date", "delivery_date",
]


def _iso(value) -> str:  # noqa: ANN001
    return value.isoformat() if value is not None else ""


def cancelled_order_open_deliveries(db: Session, organization_id: str | None = None) -> list[dict]:
    """Category (A), oldest first."""
    stmt = (
        select(Delivery, SalesOrder, Organization.name, Customer.customer_id)
        .options(lazyload("*"))
        .join(SalesOrder, SalesOrder.id == Delivery.sales_order_id)
        .join(Organization, Organization.id == Delivery.organization_id)
        .outerjoin(Customer, Customer.id == Delivery.customer_id)
        .where(SalesOrder.status == "cancelled")
        .where(Delivery.status.not_in(SAFE_UNDER_CANCELLED_ORDER))
        .order_by(Delivery.created_at, Delivery.id)
    )
    if organization_id:
        stmt = stmt.where(Delivery.organization_id == organization_id)
    rows = []
    for d, order, org_name, customer_number in db.execute(stmt):
        rows.append({
            "organization_id": d.organization_id, "organization_name": org_name or "",
            "delivery_id": d.id, "delivery_note_number": d.delivery_note_number,
            "delivery_status": d.status, "picking_status": d.picking_status or "",
            "sales_order_id": order.id, "order_number": order.sales_order_number or order.order_number or "",
            "order_status": order.status, "order_fulfilment_status": order.fulfilment_status or "",
            "customer_id": d.customer_id or "", "customer_number": customer_number or "",
            "delivery_partner_id": d.delivery_partner_id or "", "scheduled_date": _iso(d.scheduled_date),
            "dispatched_at": _iso(d.dispatched_at), "created_at": _iso(d.created_at),
        })
    return rows


def deliveries_without_customer_or_order(db: Session, organization_id: str | None = None) -> list[dict]:
    """Category (B), oldest first."""
    stmt = (
        select(Delivery, Organization.name)
        .options(lazyload("*"))
        .join(Organization, Organization.id == Delivery.organization_id)
        .where(Delivery.customer_id.is_(None), Delivery.sales_order_id.is_(None))
        .order_by(Delivery.created_at, Delivery.id)
    )
    if organization_id:
        stmt = stmt.where(Delivery.organization_id == organization_id)
    return [
        {
            "organization_id": d.organization_id, "organization_name": org_name or "", "delivery_id": d.id,
            "delivery_note_number": d.delivery_note_number, "status": d.status,
            "created_at": _iso(d.created_at), "scheduled_date": _iso(d.scheduled_date),
            "delivery_date": _iso(d.delivery_date),
        }
        for d, org_name in db.execute(stmt)
    ]


def write_csv(path: Path, columns: list[str], rows: list[dict]) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


def run_audit(db: Session, out_dir: Path, organization_id: str | None = None) -> dict[str, int]:
    if db.get_bind().dialect.name == "postgresql":
        db.connection().exec_driver_sql("SET TRANSACTION READ ONLY")
    try:
        a = cancelled_order_open_deliveries(db, organization_id)
        b = deliveries_without_customer_or_order(db, organization_id)
    finally:
        db.rollback()  # read-only: never commit anything
    out_dir.mkdir(parents=True, exist_ok=True)
    write_csv(out_dir / FILE_A, COLUMNS_A, a)
    write_csv(out_dir / FILE_B, COLUMNS_B, b)
    return {FILE_A: len(a), FILE_B: len(b)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out-dir", default="audit_out", help="folder for the CSV files (default: audit_out)")
    parser.add_argument("--organization-id", help="limit the audit to one organization")
    args = parser.parse_args(argv)

    from app.core.database import SessionLocal, engine

    print(f"Database: {engine.url.render_as_string(hide_password=True)}  (read-only audit)")
    db = SessionLocal()
    try:
        counts = run_audit(db, Path(args.out_dir), args.organization_id)
    finally:
        db.close()
    for name, count in counts.items():
        print(f"{count:6d} rows -> {os.path.join(args.out_dir, name)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
