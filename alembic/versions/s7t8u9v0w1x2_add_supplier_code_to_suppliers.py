"""add supplier_code to suppliers and tenant-scoped unique constraint

Revision ID: s7t8u9v0w1x2
Revises: r6s7t8u9v0w1
Create Date: 2026-10-06 12:00:00.000000

"""
import logging
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "s7t8u9v0w1x2"
down_revision: Union[str, Sequence[str], None] = "r6s7t8u9v0w1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

logger = logging.getLogger("alembic.runtime.migration")


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = inspector.get_table_names()

    if "suppliers" in tables:
        columns = [c["name"] for c in inspector.get_columns("suppliers")]
        if "supplier_code" not in columns:
            op.add_column(
                "suppliers",
                sa.Column("supplier_code", sa.String(length=50), nullable=True),
            )
            op.create_index(
                op.f("ix_suppliers_supplier_code"),
                "suppliers",
                ["supplier_code"],
                unique=False,
            )
            logger.info("Added supplier_code column to suppliers")

        # Backfill existing suppliers with supplier_code before applying unique constraint
        try:
            from sqlalchemy.orm import Session
            session = Session(bind=bind)
            from app.scripts.backfill_supplier_codes import backfill_supplier_codes
            backfill_supplier_codes(db=session)
        except Exception as exc:
            logger.warning("Could not backfill suppliers during migration: %s", exc)

        # Unique constraint per tenant
        try:
            op.create_unique_constraint(
                "uq_suppliers_org_supplier_code",
                "suppliers",
                ["organization_id", "supplier_code"],
            )
            logger.info("Added uq_suppliers_org_supplier_code constraint")
        except Exception as exc:
            logger.warning("Could not create uq_suppliers_org_supplier_code: %s", exc)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = inspector.get_table_names()

    if "suppliers" in tables:
        try:
            op.drop_constraint("uq_suppliers_org_supplier_code", "suppliers", type_="unique")
        except Exception:
            pass
        columns = [c["name"] for c in inspector.get_columns("suppliers")]
        if "supplier_code" in columns:
            try:
                op.drop_index(op.f("ix_suppliers_supplier_code"), table_name="suppliers")
            except Exception:
                pass
            op.drop_column("suppliers", "supplier_code")
