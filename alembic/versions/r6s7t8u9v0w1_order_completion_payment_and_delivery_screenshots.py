"""add payment_proof_url to customer_payments and delivery_proof_url to sales_orders

Revision ID: r6s7t8u9v0w1
Revises: q5r6s7t8u9v0
Create Date: 2026-10-03 17:00:00.000000

"""
import logging
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "r6s7t8u9v0w1"
down_revision: Union[str, Sequence[str], None] = "q5r6s7t8u9v0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

logger = logging.getLogger("alembic.runtime.migration")


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = inspector.get_table_names()

    if "customer_payments" in tables:
        columns = [c["name"] for c in inspector.get_columns("customer_payments")]
        if "payment_proof_url" not in columns:
            op.add_column(
                "customer_payments",
                sa.Column("payment_proof_url", sa.String(length=500), nullable=True),
            )
            logger.info("Added payment_proof_url to customer_payments")

    if "sales_orders" in tables:
        columns = [c["name"] for c in inspector.get_columns("sales_orders")]
        if "delivery_proof_url" not in columns:
            op.add_column(
                "sales_orders",
                sa.Column("delivery_proof_url", sa.String(length=500), nullable=True),
            )
            logger.info("Added delivery_proof_url to sales_orders")


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = inspector.get_table_names()

    if "customer_payments" in tables:
        columns = [c["name"] for c in inspector.get_columns("customer_payments")]
        if "payment_proof_url" in columns:
            op.drop_column("customer_payments", "payment_proof_url")

    if "sales_orders" in tables:
        columns = [c["name"] for c in inspector.get_columns("sales_orders")]
        if "delivery_proof_url" in columns:
            op.drop_column("sales_orders", "delivery_proof_url")
