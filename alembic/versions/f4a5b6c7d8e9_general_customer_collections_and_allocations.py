"""general customer collections and payment allocations

Revision ID: f4a5b6c7d8e9
Revises: e3f4a5b6c7d8
Create Date: 2026-09-10 16:35:00.000000

"""
import logging
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'f4a5b6c7d8e9'
down_revision: Union[str, Sequence[str], None] = 'e3f4a5b6c7d8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

logger = logging.getLogger("alembic.runtime.migration")

_COLL_TABLE = "delivery_collections"
_COLL_ALLOC_TABLE = "delivery_collection_allocations"
_PAY_ALLOC_TABLE = "customer_payment_allocations"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    # 1. Make delivery_id nullable on delivery_collections to support general field collections
    if _COLL_TABLE in inspector.get_table_names():
        with op.batch_alter_table(_COLL_TABLE) as batch_op:
            batch_op.alter_column("delivery_id", existing_type=sa.String(length=36), nullable=True)

    # 2. Create delivery_collection_allocations table
    if _COLL_ALLOC_TABLE not in inspector.get_table_names():
        logger.info("Creating %s", _COLL_ALLOC_TABLE)
        op.create_table(
            _COLL_ALLOC_TABLE,
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column(
                "organization_id", sa.String(length=36),
                sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False,
            ),
            sa.Column(
                "delivery_collection_id", sa.String(length=36),
                sa.ForeignKey("delivery_collections.id", ondelete="CASCADE"), nullable=False,
            ),
            sa.Column(
                "invoice_id", sa.String(length=36),
                sa.ForeignKey("invoices.id", ondelete="CASCADE"), nullable=False,
            ),
            sa.Column("amount", sa.Float(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index("ix_deliv_coll_alloc_org_id", _COLL_ALLOC_TABLE, ["organization_id"])
        op.create_index("ix_deliv_coll_alloc_coll_id", _COLL_ALLOC_TABLE, ["delivery_collection_id"])
        op.create_index("ix_deliv_coll_alloc_inv_id", _COLL_ALLOC_TABLE, ["invoice_id"])

    # 3. Create customer_payment_allocations table
    if _PAY_ALLOC_TABLE not in inspector.get_table_names():
        logger.info("Creating %s", _PAY_ALLOC_TABLE)
        op.create_table(
            _PAY_ALLOC_TABLE,
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column(
                "organization_id", sa.String(length=36),
                sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False,
            ),
            sa.Column(
                "customer_payment_id", sa.String(length=36),
                sa.ForeignKey("customer_payments.id", ondelete="CASCADE"), nullable=False,
            ),
            sa.Column(
                "invoice_id", sa.String(length=36),
                sa.ForeignKey("invoices.id", ondelete="CASCADE"), nullable=False,
            ),
            sa.Column("amount", sa.Float(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index("ix_cust_pay_alloc_org_id", _PAY_ALLOC_TABLE, ["organization_id"])
        op.create_index("ix_cust_pay_alloc_pay_id", _PAY_ALLOC_TABLE, ["customer_payment_id"])
        op.create_index("ix_cust_pay_alloc_inv_id", _PAY_ALLOC_TABLE, ["invoice_id"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if _PAY_ALLOC_TABLE in inspector.get_table_names():
        op.drop_table(_PAY_ALLOC_TABLE)

    if _COLL_ALLOC_TABLE in inspector.get_table_names():
        op.drop_table(_COLL_ALLOC_TABLE)

    if _COLL_TABLE in inspector.get_table_names():
        with op.batch_alter_table(_COLL_TABLE) as batch_op:
            batch_op.alter_column("delivery_id", existing_type=sa.String(length=36), nullable=False)
