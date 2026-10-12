"""add customer_po_number to sales_orders, and eway_bill_number, vehicle_number to invoices

Revision ID: y3z4a5b6c7d8
Revises: x2y3z4a5b6c7
Create Date: 2026-10-12 14:00:00.000000

"""
import logging
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "y3z4a5b6c7d8"
down_revision: Union[str, Sequence[str], None] = "x2y3z4a5b6c7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

logger = logging.getLogger("alembic.runtime.migration")


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = inspector.get_table_names()

    if "sales_orders" in tables:
        so_cols = [c["name"] for c in inspector.get_columns("sales_orders")]
        if "customer_po_number" not in so_cols:
            op.add_column(
                "sales_orders",
                sa.Column("customer_po_number", sa.String(length=60), nullable=True),
            )
            logger.info("Added customer_po_number to sales_orders")

    if "invoices" in tables:
        inv_cols = [c["name"] for c in inspector.get_columns("invoices")]
        if "eway_bill_number" not in inv_cols:
            op.add_column(
                "invoices",
                sa.Column("eway_bill_number", sa.String(length=50), nullable=True),
            )
            logger.info("Added eway_bill_number to invoices")

        if "vehicle_number" not in inv_cols:
            op.add_column(
                "invoices",
                sa.Column("vehicle_number", sa.String(length=50), nullable=True),
            )
            logger.info("Added vehicle_number to invoices")


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = inspector.get_table_names()

    if "invoices" in tables:
        inv_cols = [c["name"] for c in inspector.get_columns("invoices")]
        if "vehicle_number" in inv_cols:
            op.drop_column("invoices", "vehicle_number")
        if "eway_bill_number" in inv_cols:
            op.drop_column("invoices", "eway_bill_number")

    if "sales_orders" in tables:
        so_cols = [c["name"] for c in inspector.get_columns("sales_orders")]
        if "customer_po_number" in so_cols:
            op.drop_column("sales_orders", "customer_po_number")
