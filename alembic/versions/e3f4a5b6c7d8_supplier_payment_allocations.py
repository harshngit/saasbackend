"""supplier payment allocations and payment enhancements

Revision ID: e3f4a5b6c7d8
Revises: d2e3f4a5b6c7
Create Date: 2026-09-08 20:00:00.000000

"""
import logging
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'e3f4a5b6c7d8'
down_revision: Union[str, Sequence[str], None] = 'd2e3f4a5b6c7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

logger = logging.getLogger("alembic.runtime.migration")

_PAYMENT_TABLE = "supplier_payments"
_ALLOC_TABLE = "supplier_payment_allocations"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if _PAYMENT_TABLE in inspector.get_table_names():
        existing_cols = {c["name"] for c in inspector.get_columns(_PAYMENT_TABLE)}
        if "payment_number" not in existing_cols:
            op.add_column(_PAYMENT_TABLE, sa.Column("payment_number", sa.String(length=100), nullable=True))
        if "payment_method" not in existing_cols:
            op.add_column(_PAYMENT_TABLE, sa.Column("payment_method", sa.String(length=50), server_default="cash", nullable=True))
        if "status" not in existing_cols:
            op.add_column(_PAYMENT_TABLE, sa.Column("status", sa.String(length=20), server_default="recorded", nullable=False))
        if "allocated_amount" not in existing_cols:
            op.add_column(_PAYMENT_TABLE, sa.Column("allocated_amount", sa.Float(), server_default="0.0", nullable=False))
        if "unallocated_amount" not in existing_cols:
            op.add_column(_PAYMENT_TABLE, sa.Column("unallocated_amount", sa.Float(), server_default="0.0", nullable=False))
        if "created_by" not in existing_cols:
            op.add_column(_PAYMENT_TABLE, sa.Column("created_by", sa.String(length=36), nullable=True))
        if "voided_by" not in existing_cols:
            op.add_column(_PAYMENT_TABLE, sa.Column("voided_by", sa.String(length=36), nullable=True))
        if "voided_at" not in existing_cols:
            op.add_column(_PAYMENT_TABLE, sa.Column("voided_at", sa.DateTime(timezone=True), nullable=True))
        if "void_reason" not in existing_cols:
            op.add_column(_PAYMENT_TABLE, sa.Column("void_reason", sa.Text(), nullable=True))

        existing_indexes = {ix["name"] for ix in inspector.get_indexes(_PAYMENT_TABLE)}
        if "ix_supplier_payments_payment_number" not in existing_indexes:
            op.create_index("ix_supplier_payments_payment_number", _PAYMENT_TABLE, ["payment_number"])
        if "ix_supplier_payments_status" not in existing_indexes:
            op.create_index("ix_supplier_payments_status", _PAYMENT_TABLE, ["status"])

    if _ALLOC_TABLE not in inspector.get_table_names():
        logger.info("Creating %s", _ALLOC_TABLE)
        op.create_table(
            _ALLOC_TABLE,
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column(
                "organization_id", sa.String(length=36),
                sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False,
            ),
            sa.Column(
                "supplier_payment_id", sa.String(length=36),
                sa.ForeignKey("supplier_payments.id", ondelete="CASCADE"), nullable=False,
            ),
            sa.Column(
                "supplier_invoice_id", sa.String(length=36),
                sa.ForeignKey("supplier_invoices.id", ondelete="RESTRICT"), nullable=False,
            ),
            sa.Column("amount", sa.Float(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )

        op.create_index("ix_supplier_payment_allocations_org_id", _ALLOC_TABLE, ["organization_id"])
        op.create_index("ix_supplier_payment_allocations_payment_id", _ALLOC_TABLE, ["supplier_payment_id"])
        op.create_index("ix_supplier_payment_allocations_invoice_id", _ALLOC_TABLE, ["supplier_invoice_id"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if _ALLOC_TABLE in inspector.get_table_names():
        op.drop_table(_ALLOC_TABLE)
