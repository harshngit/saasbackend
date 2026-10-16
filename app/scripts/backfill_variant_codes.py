"""Idempotent backfill of product_variants.organization_id and variant_code (VAR-0001).

Run by the a5b6c7d8e9f0 Alembic migration, and runnable by hand:
    python -m app.scripts.backfill_variant_codes [--dry-run]

NOTE:
- Does NOT execute automatically on startup.
- Pure SQL on explicit columns, so the migration keeps behaving the same however
  the ORM models change later.
- Idempotent: a variant that already has a variant_code keeps it.
- organization_id is (re)derived from the owning product wherever it is missing,
  blank, or disagrees with the product.
- variant_code is numbered per organization in a deterministic order (product
  created_at, product id, variant name, variant id), continuing after the highest
  VAR number the firm has already issued -- in the data or in number_sequences --
  and the firm's VAR sequence is then moved up to the last number used, so a
  runtime allocation can never collide with a backfilled one.
"""

import argparse
import json
import re
import sys
import uuid

from sqlalchemy import text
from sqlalchemy.engine import Connection

SERIES = "VAR"
MASTER_YEAR = 0  # master/catalog sequences never reset by year


def _prefix(raw_prefixes: object) -> str:
    """Same rule as numbering_service.prefix_for: the firm's override, else VAR."""
    prefixes = raw_prefixes
    if isinstance(prefixes, str):
        try:
            prefixes = json.loads(prefixes)
        except ValueError:
            prefixes = {}
    if not isinstance(prefixes, dict):
        prefixes = {}
    override = prefixes.get(SERIES) or prefixes.get(SERIES.lower())
    return (override or SERIES).strip() or SERIES


def _sync_organization_ids(conn: Connection) -> int:
    product_org = "(SELECT p.organization_id FROM products p WHERE p.id = product_variants.product_id)"
    stale = (
        f"organization_id IS NULL OR organization_id = '' OR organization_id <> {product_org}"
    )
    count = conn.execute(text(f"SELECT COUNT(*) FROM product_variants WHERE {stale}")).scalar() or 0
    if count:
        conn.execute(text(f"UPDATE product_variants SET organization_id = {product_org} WHERE {stale}"))
    missing = conn.execute(
        text(
            "SELECT COUNT(*) FROM product_variants "
            "WHERE organization_id IS NULL OR organization_id = ''"
        )
    ).scalar()
    if missing:
        raise RuntimeError(
            f"{missing} product variant(s) have no owning product/organization; "
            "fix them before variant codes can be assigned"
        )
    return count


def _backfill_org(conn: Connection, org_id: str, prefix: str) -> int:
    stem = f"{prefix}-"
    highest = 0
    for (code,) in conn.execute(
        text(
            "SELECT variant_code FROM product_variants "
            "WHERE organization_id = :org AND variant_code LIKE :like"
        ),
        {"org": org_id, "like": f"{stem}%"},
    ):
        match = re.fullmatch(rf"{re.escape(stem)}(\d+)", code or "")
        if match:
            highest = max(highest, int(match.group(1)))

    sequence = conn.execute(
        text(
            "SELECT id, last_number FROM number_sequences "
            "WHERE organization_id = :org AND series = :series AND year = :year"
        ),
        {"org": org_id, "series": SERIES, "year": MASTER_YEAR},
    ).first()
    last = max(highest, sequence[1] if sequence else 0)

    pending = conn.execute(
        text(
            "SELECT v.id FROM product_variants v JOIN products p ON p.id = v.product_id "
            "WHERE v.organization_id = :org AND (v.variant_code IS NULL OR v.variant_code = '') "
            "ORDER BY p.created_at, p.id, v.name, v.id"
        ),
        {"org": org_id},
    ).fetchall()
    for (variant_id,) in pending:
        last += 1
        conn.execute(
            text("UPDATE product_variants SET variant_code = :code WHERE id = :id"),
            {"code": f"{stem}{last:04d}", "id": variant_id},
        )

    if sequence is None:
        if last:
            conn.execute(
                text(
                    "INSERT INTO number_sequences (id, organization_id, series, year, last_number) "
                    "VALUES (:id, :org, :series, :year, :last)"
                ),
                {"id": str(uuid.uuid4()), "org": org_id, "series": SERIES, "year": MASTER_YEAR, "last": last},
            )
    elif sequence[1] < last:
        conn.execute(
            text("UPDATE number_sequences SET last_number = :last WHERE id = :id"),
            {"last": last, "id": sequence[0]},
        )
    return len(pending)


def backfill_variant_codes(conn: Connection, dry_run: bool = False) -> dict[str, int]:
    """Backfill inside the caller's transaction. A dry run does the same work in a
    savepoint and rolls it back, so it reports exactly what a real run would change."""
    savepoint = conn.begin_nested()
    try:
        orgs_fixed = _sync_organization_ids(conn)
        assigned = 0
        orgs = conn.execute(
            text(
                "SELECT o.id, o.number_prefixes FROM organizations o "
                "WHERE EXISTS (SELECT 1 FROM product_variants v WHERE v.organization_id = o.id) "
                "ORDER BY o.id"
            )
        ).fetchall()
        for org_id, raw_prefixes in orgs:
            assigned += _backfill_org(conn, org_id, _prefix(raw_prefixes))
    except Exception:
        savepoint.rollback()
        raise
    if dry_run:
        savepoint.rollback()
    else:
        savepoint.commit()
    return {"organization_ids_fixed": orgs_fixed, "codes_assigned": assigned, "organizations": len(orgs)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backfill variant_code on product variants")
    parser.add_argument("--dry-run", action="store_true", help="Simulate without committing")
    args = parser.parse_args()

    from app.core.database import engine

    try:
        with engine.begin() as connection:
            result = backfill_variant_codes(connection, dry_run=args.dry_run)
    except Exception as exc:  # noqa: BLE001
        print(f"Error during backfill: {exc}", file=sys.stderr)
        raise
    label = "[DRY-RUN] Would assign" if args.dry_run else "Backfill complete: assigned"
    print(
        f"{label} {result['codes_assigned']} variant codes across {result['organizations']} "
        f"organizations ({result['organization_ids_fixed']} organization_id values derived)."
    )
