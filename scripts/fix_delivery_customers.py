"""Set the customer on deliveries that have neither a customer nor a sales order.

Input: a CSV with the columns `delivery_note_number,customer_number`
(customer_number is the customer's code, e.g. CS-0001, as shown in the app).
An optional `organization_id` column, or --organization-id, picks the firm when the
same delivery number exists in more than one.

Default is a DRY RUN: it prints what would change and changes nothing.

    python scripts/fix_delivery_customers.py fixes.csv                       # dry run
    python scripts/fix_delivery_customers.py fixes.csv --apply \\
        --confirm-apply --actor "Name <email>"                               # writes

Rules:
- Only a delivery whose customer_id AND sales_order_id are both NULL is updated.
  Anything else is refused: unknown or ambiguous delivery number, unknown customer
  number (customer must be in the delivery's own firm), a delivery that already has a
  different customer or has a sales order, the same delivery listed twice.
- A delivery that already has exactly the requested customer (and no order) is
  reported as "already done" and left alone, so re-running the same file is safe.
- --apply is all-or-nothing: if ANY row is refused nothing is written (exit code 2).
  All changes go in ONE transaction; the rows are locked and re-checked first.
- Every change writes an audit entry in the same transaction (activity_logs:
  entity delivery, action customer_backfilled, old -> new, actor) and, after the
  commit, one JSON line to --audit-file (default fix_delivery_customers.audit.jsonl).

Exit codes: 0 ok (dry run, or applied), 1 bad input/usage, 2 refused rows.
"""

from __future__ import annotations

import argparse
import csv
import getpass
import json
import platform
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.orm import Session, lazyload  # noqa: E402

from app.models import ActivityLog, Customer, Delivery  # noqa: E402

AUDIT_ACTION = "customer_backfilled"
REQUIRED_COLUMNS = ("delivery_note_number", "customer_number")


@dataclass
class RowResult:
    line: int
    delivery_note_number: str
    customer_number: str
    outcome: str  # "change" | "already_done" | "refused"
    reason: str = ""
    organization_id: str | None = None
    delivery_id: str | None = None
    old_customer_id: str | None = None
    new_customer_id: str | None = None


@dataclass
class Plan:
    rows: list[RowResult] = field(default_factory=list)

    @property
    def changes(self) -> list[RowResult]:
        return [r for r in self.rows if r.outcome == "change"]

    @property
    def refused(self) -> list[RowResult]:
        return [r for r in self.rows if r.outcome == "refused"]


class InputError(Exception):
    pass


def read_csv(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        header = [h.strip() for h in (reader.fieldnames or [])]
        missing = [c for c in REQUIRED_COLUMNS if c not in header]
        if missing:
            raise InputError(f"CSV is missing column(s): {', '.join(missing)}")
        return [{(k or "").strip(): (v or "").strip() for k, v in row.items()} for row in reader]


def build_plan(db: Session, rows: list[dict], organization_id: str | None = None, *, lock: bool = False) -> Plan:
    """Decide, for every CSV row, what would happen. Writes nothing."""
    plan = Plan()
    seen: dict[str, int] = {}
    for index, raw in enumerate(rows, start=2):  # line 1 is the header
        number, customer_number = raw.get("delivery_note_number", ""), raw.get("customer_number", "")
        org_filter = raw.get("organization_id") or organization_id
        result = RowResult(index, number, customer_number, "refused")
        plan.rows.append(result)
        if not number or not customer_number:
            result.reason = "delivery_note_number and customer_number are both required"
            continue

        # lazyload: no eager JOINs, so FOR UPDATE locks just the delivery rows.
        stmt = select(Delivery).options(lazyload("*")).where(Delivery.delivery_note_number == number)
        if org_filter:
            stmt = stmt.where(Delivery.organization_id == org_filter)
        if lock:
            stmt = stmt.with_for_update()
        matches = list(db.scalars(stmt))
        if not matches:
            result.reason = "no delivery with this number" + (" in that organization" if org_filter else "")
            continue
        if len(matches) > 1:
            result.reason = (f"{len(matches)} deliveries have this number; add organization_id "
                             "(column or --organization-id)")
            continue
        delivery = matches[0]
        result.organization_id, result.delivery_id = delivery.organization_id, delivery.id
        result.old_customer_id = delivery.customer_id

        if delivery.id in seen:
            result.reason = f"same delivery already listed on line {seen[delivery.id]}"
            continue
        seen[delivery.id] = index

        customers = list(db.scalars(select(Customer).options(lazyload("*")).where(
            Customer.organization_id == delivery.organization_id, Customer.customer_id == customer_number)))
        if len(customers) != 1:
            result.reason = ("no customer with this number in the delivery's organization" if not customers
                             else "customer number is not unique in this organization")
            continue
        customer = customers[0]
        result.new_customer_id = customer.id

        if delivery.sales_order_id is not None:
            result.reason = "delivery has a sales order; its customer comes from the order"
            continue
        if delivery.customer_id == customer.id:
            result.outcome, result.reason = "already_done", "already has this customer"
            continue
        if delivery.customer_id is not None:
            result.reason = "delivery already has a different customer"
            continue
        result.outcome = "change"
    return plan


def print_plan(plan: Plan, out=sys.stdout) -> None:  # noqa: ANN001
    for r in plan.rows:
        if r.outcome == "change":
            out.write(f"  line {r.line}: {r.delivery_note_number}: customer_id NULL -> {r.new_customer_id} "
                      f"({r.customer_number})\n")
        elif r.outcome == "already_done":
            out.write(f"  line {r.line}: {r.delivery_note_number}: unchanged, {r.reason}\n")
        else:
            out.write(f"  line {r.line}: {r.delivery_note_number or '?'}: REFUSED, {r.reason}\n")
    out.write(f"{len(plan.changes)} to change, "
              f"{sum(r.outcome == 'already_done' for r in plan.rows)} already done, {len(plan.refused)} refused\n")


def default_actor() -> str:
    try:
        user = getpass.getuser()
    except Exception:  # noqa: BLE001
        user = "unknown"
    return f"{user}@{platform.node() or 'unknown-host'}"


def apply_plan(db: Session, rows: list[dict], actor: str, organization_id: str | None = None) -> Plan:
    """Lock, re-check and write every change in one transaction. If any row is refused,
    rolls back and returns the plan with nothing written. Otherwise the caller commits."""
    plan = build_plan(db, rows, organization_id, lock=True)
    if plan.refused:
        db.rollback()
        return plan
    for r in plan.changes:
        delivery = db.get(Delivery, r.delivery_id)
        # Re-checked under the row lock taken by build_plan(lock=True).
        if delivery.customer_id is not None or delivery.sales_order_id is not None:
            db.rollback()
            raise RuntimeError(f"delivery {r.delivery_note_number} changed while running; nothing written")
        delivery.customer_id = r.new_customer_id
        db.add(ActivityLog(
            organization_id=r.organization_id, user_id=None, actor_name=actor[:150], type="delivery",
            title=f"Customer set on delivery {r.delivery_note_number}"[:200], entity_type="delivery",
            entity_id=r.delivery_id, action=AUDIT_ACTION,
            description=json.dumps({"field": "customer_id", "old": r.old_customer_id, "new": r.new_customer_id,
                                    "customer_number": r.customer_number,
                                    "source": "scripts/fix_delivery_customers.py"}),
        ))
    db.flush()
    return plan


def write_audit_file(path: Path, plan: Plan, actor: str) -> None:
    at = datetime.now(timezone.utc).isoformat()
    with path.open("a", encoding="utf-8") as fh:
        for r in plan.changes:
            entry = {"at": at, "actor": actor, **{k: v for k, v in asdict(r).items() if k not in ("outcome", "reason")}}
            fh.write(json.dumps(entry) + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("csv_file")
    parser.add_argument("--organization-id", help="the firm, when a delivery number exists in several")
    parser.add_argument("--apply", action="store_true", help="write the changes (default is a dry run)")
    parser.add_argument("--confirm-apply", action="store_true", help="required together with --apply")
    parser.add_argument("--actor", help="who is running this (name / email); recorded in the audit log")
    parser.add_argument("--audit-file", default="fix_delivery_customers.audit.jsonl")
    args = parser.parse_args(argv)

    if args.apply and not args.confirm_apply:
        print("--apply also needs --confirm-apply (take a database backup first).", file=sys.stderr)
        return 1
    try:
        rows = read_csv(Path(args.csv_file))
    except (OSError, InputError) as exc:
        print(f"Cannot read {args.csv_file}: {exc}", file=sys.stderr)
        return 1
    actor = (args.actor or default_actor()).strip()

    from app.core.database import SessionLocal, engine

    print(f"Database: {engine.url.render_as_string(hide_password=True)}")
    db = SessionLocal()
    try:
        if not args.apply:
            plan = build_plan(db, rows, args.organization_id)
            db.rollback()
            print("DRY RUN -- nothing is written.")
            print_plan(plan)
            return 2 if plan.refused else 0
        plan = apply_plan(db, rows, actor, args.organization_id)
        print_plan(plan)
        if plan.refused:
            print("Refused rows found: NOTHING was written. Fix the CSV and run again.", file=sys.stderr)
            return 2
        db.commit()
        write_audit_file(Path(args.audit_file), plan, actor)
        print(f"Applied {len(plan.changes)} change(s) as {actor!r}; audit lines -> {args.audit_file}")
        return 0
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
