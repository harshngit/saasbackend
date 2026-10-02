"""add return_amount to supplier_invoices and purchase_invoices

Revision ID: q5r6s7t8u9v0
Revises: p4q5r6s7t8u9
Create Date: 2026-10-02 13:50:00.000000

"""
import logging
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "q5r6s7t8u9v0"
down_revision: Union[str, Sequence[str], None] = "p4q5r6s7t8u9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

logger = logging.getLogger("alembic.runtime.migration")


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = inspector.get_table_names()

    if "supplier_invoices" in tables:
        columns = [c["name"] for c in inspector.get_columns("supplier_invoices")]
        if "return_amount" not in columns:
            op.add_column(
                "supplier_invoices",
                sa.Column("return_amount", sa.Float(), nullable=False, server_default="0.0"),
            )
            logger.info("Added return_amount to supplier_invoices")

    if "purchase_invoices" in tables:
        columns = [c["name"] for c in inspector.get_columns("purchase_invoices")]
        if "return_amount" not in columns:
            op.add_column(
                "purchase_invoices",
                sa.Column("return_amount", sa.Float(), nullable=False, server_default="0.0"),
            )
            logger.info("Added return_amount to purchase_invoices")


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = inspector.get_table_names()

    if "supplier_invoices" in tables:
        columns = [c["name"] for c in inspector.get_columns("supplier_invoices")]
        if "return_amount" in columns:
            op.drop_column("supplier_invoices", "return_amount")

    if "purchase_invoices" in tables:
        columns = [c["name"] for c in inspector.get_columns("purchase_invoices")]
        if "return_amount" in columns:
            op.drop_column("purchase_invoices", "return_amount")
