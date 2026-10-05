"""One-off manual idempotent backfill script for historical deliveries with warehouse_id = NULL.

Usage (manual execution only):
    python -m app.scripts.backfill_delivery_warehouses

NOTE:
- Does NOT execute automatically on startup.
- Does NOT run against production during deployment/testing.
- Idempotent: skips deliveries that already have warehouse_id set.
"""

import sys
from app.core.database import SessionLocal
from app.models import Delivery, SalesOrder
from app.services import stock_service


def backfill_delivery_warehouses() -> int:
    db = SessionLocal()
    updated = 0
    try:
        deliveries = db.query(Delivery).filter(Delivery.warehouse_id.is_(None)).all()
        for d in deliveries:
            org_id = d.organization_id
            resolved_wh = None

            # 1. Order warehouse
            if d.sales_order_id:
                order = db.get(SalesOrder, d.sales_order_id)
                if order and order.warehouse_id:
                    resolved_wh = stock_service.owned_warehouse(db, order.warehouse_id, org_id)

            if resolved_wh:
                d.warehouse_id = resolved_wh.id
                updated += 1

        db.commit()
        print(f"Backfill complete: updated {updated} deliveries with warehouse_id.")
        return updated
    except Exception as exc:
        db.rollback()
        print(f"Error during backfill: {exc}", file=sys.stderr)
        raise
    finally:
        db.close()


if __name__ == "__main__":
    backfill_delivery_warehouses()
