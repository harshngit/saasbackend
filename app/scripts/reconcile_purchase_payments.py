"""Manual reconciliation of purchase / supplier invoice payment summaries.

Usage (manual execution only):
    python -m app.scripts.reconcile_purchase_payments                 # dry run: report only
    python -m app.scripts.reconcile_purchase_payments --csv report.csv
    python -m app.scripts.reconcile_purchase_payments --apply         # recalculate + log

What it compares:
- each supplier invoice's stored amount_paid with the sum of its allocations from recorded
  (non-voided) supplier payments -- the canonical payment record;
- each purchase's stored amount_paid / payment_status with the sum over its non-cancelled
  supplier invoices.

NOTE:
- Dry run by default; nothing is written without --apply.
- --apply only recalculates the stored summaries from existing supplier payments and writes
  one activity-log entry per change. It never creates, edits or deletes a supplier payment:
  an amount recorded only on the old purchase-level fields (has_supplier_invoice / no
  payment behind it) is reported so the team can record the real payment if one was made.
- Does NOT execute automatically on startup or deploy.
"""

import argparse
import csv
import sys

from app.core.database import SessionLocal
from app.services import supplier_payment_service


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="recalculate stored summaries (default: dry run)")
    parser.add_argument("--org", help="limit to one organization id")
    parser.add_argument("--csv", help="write the findings to this CSV file")
    args = parser.parse_args()

    db = SessionLocal()
    try:
        findings = supplier_payment_service.reconcile_purchase_payments(db, org_id=args.org, apply=args.apply)
    finally:
        db.close()

    invoices = [f for f in findings if f["kind"] == "supplier_invoice"]
    purchases = [f for f in findings if f["kind"] == "purchase"]
    print(f"{'APPLIED' if args.apply else 'DRY RUN'}: {len(invoices)} supplier invoice(s) and "
          f"{len(purchases)} purchase(s) differ from the supplier payment ledger")
    for f in findings:
        print(f"  {f['kind']:16s} {f['number']}: stored {f['stored_amount_paid']} -> canonical {f['canonical_amount_paid']}"
              + (f" ({f['stored_payment_status']} -> {f['canonical_payment_status']})" if f["kind"] == "purchase" else ""))
    if args.csv and findings:
        keys = sorted({k for f in findings for k in f})
        with open(args.csv, "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=keys)
            writer.writeheader()
            writer.writerows(findings)
        print(f"findings written to {args.csv}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
