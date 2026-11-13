"""Read-only audit of purchase returns whose stock has already left (confirmed /
dispatched / completed). Writes two CSV files and changes nothing.

Why: before the purchase-return fix, confirm moved ONLY the product / variant catalog
counter (`total_inventory` / `inventory`) and wrote a stock_movements row; it never
moved warehouse stock (`warehouse_stock.on_hand_quantity`). When the return's warehouse
already had a stock row for the product, confirm crashed instead (500), so the returns
that did get confirmed are exactly the ones where the warehouse was never touched.

purchase_return_lines.csv            one row per return line (every audited return)
flagged_purchase_returns.csv         one row per return with at least one flag

Flags (per line; a return is flagged when any of its lines is):
  NO_STOCK_MOVEMENT        no purchase_return stock_movements row for this return + item
  MOVEMENT_QTY_MISMATCH    the matching movements do not add up to -return qty
  MOVEMENT_WAREHOUSE_DIFFERS
                           the movement names no warehouse, or another warehouse than the return
  WAREHOUSE_NOT_TRACKED    the return's warehouse has no stock row for the item: the goods
                           left on paper (counter) but never out of a warehouse
  COUNTER_DISAGREES        the catalog counter != the sum of on_hand over all warehouses
                           (only checked when some warehouse tracks the item). This can
                           also have other causes; it is reported, not interpreted.

Usage (run against a COPY / branch of the database first):
    DATABASE_URL=postgresql://... python scripts/audit_confirmed_purchase_returns.py --out-dir audit_out
    python scripts/audit_confirmed_purchase_returns.py --out-dir audit_out --organization-id <org uuid>

On PostgreSQL the whole audit runs in a READ ONLY transaction that is rolled back, so even
a bug in this script cannot write. Exit code 0 = done (finding rows is not a failure).

There is deliberately NO auto-fix. Safe correction, per flagged line, after a person has
checked the physical stock (text only; see the deploy message for the full steps):
  * the goods really went back to the supplier but the warehouse still shows them:
    one stock_service.adjust_on_hand(warehouse, product, variant, -qty,
    movement_type="adjustment", note="Correction for purchase return <PR no>: warehouse
    stock was not reduced on confirm (<who>, <date>)") plus an ActivityLog row;
    adjust_on_hand re-syncs the catalog counter from the warehouses, so the counter is
    not touched by hand;
  * no correction when WAREHOUSE_NOT_TRACKED and the item is tracked in no warehouse at
    all (the counter is the only stock figure and it was moved correctly).
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.orm import Session, lazyload  # noqa: E402

from app.models import (  # noqa: E402
    Organization, Product, ProductVariant, StockMovement, Supplier, Warehouse, WarehouseStock,
)
from app.models.purchase_return import PurchaseReturn, PurchaseReturnItem  # noqa: E402

STOCK_LEFT_STATUSES = ("confirmed", "dispatched", "completed")

FILE_LINES = "purchase_return_lines.csv"
FILE_FLAGGED = "flagged_purchase_returns.csv"

LINE_COLUMNS = [
    "organization_id", "organization_name", "return_id", "return_number", "status", "confirmed_at",
    "supplier_id", "supplier_name", "warehouse_id", "warehouse_name", "line_id", "product_id",
    "product_name", "variant_id", "variant_name", "return_qty", "movement_found", "movement_qty",
    "movement_warehouse_ids", "warehouse_on_hand_now", "all_warehouses_on_hand_now", "counter_now",
    "flags",
]
FLAGGED_COLUMNS = [
    "organization_id", "organization_name", "return_id", "return_number", "status", "confirmed_at",
    "supplier_name", "warehouse_name", "lines", "flagged_lines", "flags",
]


def _iso(value) -> str:  # noqa: ANN001
    return value.isoformat() if value is not None else ""


def _num(value) -> str:  # noqa: ANN001
    if value is None:
        return ""
    value = float(value)
    return str(int(value)) if value.is_integer() else str(value)


def _movements(db: Session, ret: PurchaseReturn) -> dict[tuple[str, str | None], list[StockMovement]]:
    """This return's outbound purchase_return movements, by (product, variant). Both the old
    and the new confirm write the note "Return <number> on <purchase>[ — reason]"; a
    cancellation writes "Cancelled Return ..." and is excluded by the prefix."""
    rows = db.execute(
        select(StockMovement).options(lazyload("*")).where(
            StockMovement.organization_id == ret.organization_id,
            StockMovement.movement_type == "purchase_return",
            StockMovement.quantity < 0,
            StockMovement.note.like(f"Return {ret.return_number} %"),
        )
    ).scalars()
    out: dict[tuple[str, str | None], list[StockMovement]] = defaultdict(list)
    for m in rows:
        out[(m.product_id, m.variant_id)].append(m)
    return out


def _stock_rows(db: Session, org_id: str, product_id: str, variant_id: str | None) -> list[WarehouseStock]:
    return list(db.execute(
        select(WarehouseStock).options(lazyload("*")).where(
            WarehouseStock.organization_id == org_id,
            WarehouseStock.product_id == product_id,
            WarehouseStock.variant_id.is_(None) if variant_id is None else WarehouseStock.variant_id == variant_id,
        )
    ).scalars())


def _counter(db: Session, product_id: str, variant_id: str | None) -> int | None:
    if variant_id:
        v = db.get(ProductVariant, variant_id)
        return int(v.inventory or 0) if v is not None else None
    p = db.get(Product, product_id)
    return int(p.total_inventory or 0) if p is not None else None


def audit_lines(db: Session, organization_id: str | None = None) -> list[dict]:
    stmt = (
        select(PurchaseReturn)
        .options(lazyload("*"))
        .where(PurchaseReturn.status.in_(STOCK_LEFT_STATUSES))
        .order_by(PurchaseReturn.organization_id, PurchaseReturn.created_at, PurchaseReturn.id)
    )
    if organization_id:
        stmt = stmt.where(PurchaseReturn.organization_id == organization_id)

    names: dict[tuple[type, str], str] = {}

    def name_of(model, pk, attr="name") -> str:  # noqa: ANN001
        if not pk:
            return ""
        key = (model, pk)
        if key not in names:
            obj = db.get(model, pk)
            names[key] = (getattr(obj, attr, None) or "") if obj is not None else ""
        return names[key]

    rows: list[dict] = []
    for ret in db.execute(stmt).scalars():
        moves = _movements(db, ret)
        items = db.execute(
            select(PurchaseReturnItem).options(lazyload("*"))
            .where(PurchaseReturnItem.purchase_return_id == ret.id)
            .order_by(PurchaseReturnItem.id)
        ).scalars()
        for ri in items:
            flags: list[str] = []
            row = {
                "organization_id": ret.organization_id,
                "organization_name": name_of(Organization, ret.organization_id),
                "return_id": ret.id, "return_number": ret.return_number, "status": ret.status,
                "confirmed_at": _iso(ret.confirmed_at), "supplier_id": ret.supplier_id or "",
                "supplier_name": name_of(Supplier, ret.supplier_id), "warehouse_id": ret.warehouse_id or "",
                "warehouse_name": name_of(Warehouse, ret.warehouse_id), "line_id": ri.id,
                "product_id": ri.product_id or "", "product_name": ri.product_name,
                "variant_id": ri.variant_id or "", "variant_name": name_of(ProductVariant, ri.variant_id),
                "return_qty": ri.quantity,
            }
            if not ri.product_id:  # product deleted since: nothing to compare against
                row.update(movement_found="", movement_qty="", movement_warehouse_ids="",
                           warehouse_on_hand_now="", all_warehouses_on_hand_now="", counter_now="",
                           flags="PRODUCT_DELETED")
                rows.append(row)
                continue

            found = moves.get((ri.product_id, ri.variant_id), [])
            moved = -sum(float(m.quantity or 0) for m in found)
            move_whs = sorted({m.warehouse_id or "" for m in found})
            if not found:
                flags.append("NO_STOCK_MOVEMENT")
            else:
                if abs(moved - ri.quantity) > 1e-6:
                    flags.append("MOVEMENT_QTY_MISMATCH")
                if any(w != (ret.warehouse_id or "") or not w for w in move_whs):
                    flags.append("MOVEMENT_WAREHOUSE_DIFFERS")

            stock = _stock_rows(db, ret.organization_id, ri.product_id, ri.variant_id)
            here = next((s for s in stock if s.warehouse_id == ret.warehouse_id), None)
            total_on_hand = sum(float(s.on_hand_quantity or 0) for s in stock)
            counter = _counter(db, ri.product_id, ri.variant_id)
            if here is None:
                flags.append("WAREHOUSE_NOT_TRACKED")
            if stock and counter is not None and abs(counter - int(total_on_hand)) > 0:
                flags.append("COUNTER_DISAGREES")

            row.update(
                movement_found="yes" if found else "no",
                movement_qty=_num(moved) if found else "",
                movement_warehouse_ids=";".join(w or "(none)" for w in move_whs),
                warehouse_on_hand_now=_num(here.on_hand_quantity) if here is not None else "",
                all_warehouses_on_hand_now=_num(total_on_hand) if stock else "",
                counter_now="" if counter is None else counter,
                flags=";".join(flags),
            )
            rows.append(row)
    return rows


def flagged_returns(lines: list[dict]) -> list[dict]:
    by_return: dict[str, dict] = {}
    for line in lines:
        entry = by_return.setdefault(line["return_id"], {
            "organization_id": line["organization_id"], "organization_name": line["organization_name"],
            "return_id": line["return_id"], "return_number": line["return_number"], "status": line["status"],
            "confirmed_at": line["confirmed_at"], "supplier_name": line["supplier_name"],
            "warehouse_name": line["warehouse_name"], "lines": 0, "flagged_lines": 0, "_flags": set(),
        })
        entry["lines"] += 1
        if line["flags"]:
            entry["flagged_lines"] += 1
            entry["_flags"].update(line["flags"].split(";"))
    out = []
    for entry in by_return.values():
        if entry["flagged_lines"]:
            entry["flags"] = ";".join(sorted(entry.pop("_flags")))
            out.append(entry)
    return out


def write_csv(path: Path, columns: list[str], rows: list[dict]) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def run_audit(db: Session, out_dir: Path, organization_id: str | None = None) -> dict[str, int]:
    if db.get_bind().dialect.name == "postgresql":
        db.connection().exec_driver_sql("SET TRANSACTION READ ONLY")
    try:
        lines = audit_lines(db, organization_id)
    finally:
        db.rollback()  # read-only: never commit anything
    flagged = flagged_returns(lines)
    out_dir.mkdir(parents=True, exist_ok=True)
    write_csv(out_dir / FILE_LINES, LINE_COLUMNS, lines)
    write_csv(out_dir / FILE_FLAGGED, FLAGGED_COLUMNS, flagged)
    return {FILE_LINES: len(lines), FILE_FLAGGED: len(flagged)}


def flag_counts(lines: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    for line in lines:
        for flag in filter(None, line["flags"].split(";")):
            counts[flag] += 1
    return dict(counts)


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
    with (Path(args.out_dir) / FILE_LINES).open(encoding="utf-8") as fh:
        for flag, n in sorted(flag_counts(list(csv.DictReader(fh))).items()):
            print(f"{n:6d} lines flagged {flag}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
