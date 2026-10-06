"""One-off manual idempotent backfill script for historical suppliers with supplier_code = NULL.

Usage (manual execution only):
    python -m app.scripts.backfill_supplier_codes [--dry-run]

NOTE:
- Does NOT execute automatically on startup.
- Does NOT run against production during deployment/testing.
- Idempotent: skips suppliers that already have supplier_code set.
- Scoped per organization in deterministic created_at, id order.
"""

import argparse
import sys
from sqlalchemy.orm import Session
from app.core.database import SessionLocal
from app.models import Organization, Supplier
from app.services import numbering_service


def backfill_supplier_codes(db: Session | None = None, dry_run: bool = False, org_id: str | None = None) -> int:
    should_close = False
    if db is None:
        db = SessionLocal()
        should_close = True
    updated = 0
    try:
        if org_id:
            orgs = [(org_id,)]
        else:
            orgs = db.query(Organization.id).all()

        for (oid,) in orgs:
            suppliers = (
                db.query(Supplier)
                .filter(
                    Supplier.organization_id == oid,
                    (Supplier.supplier_code.is_(None)) | (Supplier.supplier_code == ""),
                )
                .order_by(Supplier.created_at, Supplier.id)
                .all()
            )
            for s in suppliers:
                code = numbering_service.next_master_number(
                    db, oid, Supplier.supplier_code, "SUP"
                )
                s.supplier_code = code
                updated += 1
                if not dry_run:
                    db.flush()

        if dry_run:
            db.rollback()
            print(f"[DRY-RUN] Would update {updated} suppliers with supplier_code.")
        else:
            db.commit()
            print(f"Backfill complete: updated {updated} suppliers with supplier_code.")
        return updated
    except Exception as exc:
        db.rollback()
        print(f"Error during backfill: {exc}", file=sys.stderr)
        raise
    finally:
        if should_close:
            db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backfill supplier_code on historical suppliers")
    parser.add_argument("--dry-run", action="store_true", help="Simulate without committing")
    args = parser.parse_args()
    backfill_supplier_codes(dry_run=args.dry_run)
