"""delivery_collections table

Adds delivery_collections table backing app.models.delivery.DeliveryCollection.

Revision ID: d4e5f6a7b8c9
Revises: 75fa492c339a
Create Date: 2026-09-05 12:00:00.000000

"""
import logging
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'd4e5f6a7b8c9'
down_revision: Union[str, Sequence[str], None] = '75fa492c339a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

logger = logging.getLogger("alembic.runtime.migration")

_TABLE = "delivery_collections"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if _TABLE in inspector.get_table_names():
        logger.info("%s already exists -- skipping table creation", _TABLE)
    else:
        logger.info("Creating %s", _TABLE)
        op.create_table(
            _TABLE,
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column(
                "organization_id", sa.String(length=36),
                sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False,
            ),
            sa.Column(
                "delivery_id", sa.String(length=36),
                sa.ForeignKey("deliveries.id", ondelete="CASCADE"), nullable=False,
            ),
            sa.Column(
                "sales_order_id", sa.String(length=36),
                sa.ForeignKey("sales_orders.id", ondelete="SET NULL"), nullable=True,
            ),
            sa.Column(
                "customer_id", sa.String(length=36),
                sa.ForeignKey("customers.id", ondelete="SET NULL"), nullable=True,
            ),
            sa.Column(
                "delivery_partner_id", sa.String(length=36),
                sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True,
            ),
            sa.Column("amount", sa.Float(), nullable=False),
            sa.Column("payment_mode", sa.String(length=30), nullable=False, server_default="cash"),
            sa.Column("reference", sa.String(length=150), nullable=True),
            sa.Column("notes", sa.Text(), nullable=True),
            sa.Column("collected_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("reconciliation_status", sa.String(length=30), nullable=False, server_default="recorded"),
            sa.Column("reconciled_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column(
                "reconciled_by_id", sa.String(length=36),
                sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True,
            ),
            sa.Column(
                "customer_payment_id", sa.String(length=36),
                sa.ForeignKey("customer_payments.id", ondelete="SET NULL"), nullable=True,
            ),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )

    inspector = sa.inspect(bind)
    existing_indexes = {ix["name"] for ix in inspector.get_indexes(_TABLE)}

    def _ensure_index(name: str, columns: list[str]) -> None:
        if name not in existing_indexes:
            logger.info("Adding index %s", name)
            op.create_index(name, _TABLE, columns)

    _ensure_index("ix_delivery_collections_organization_id", ["organization_id"])
    _ensure_index("ix_delivery_collections_delivery_id", ["delivery_id"])
    _ensure_index("ix_delivery_collections_sales_order_id", ["sales_order_id"])
    _ensure_index("ix_delivery_collections_customer_id", ["customer_id"])
    _ensure_index("ix_delivery_collections_delivery_partner_id", ["delivery_partner_id"])
    _ensure_index("ix_delivery_collections_reconciliation_status", ["reconciliation_status"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if _TABLE in inspector.get_table_names():
        op.drop_table(_TABLE)
