"""create purchase_returns and purchase_return_items tables

Revision ID: p4q5r6s7t8u9
Revises: o3p4q5r6s7t8
Create Date: 2026-10-02 12:00:00.000000

"""
import logging
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "p4q5r6s7t8u9"
down_revision: Union[str, Sequence[str], None] = "o3p4q5r6s7t8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

logger = logging.getLogger("alembic.runtime.migration")


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = inspector.get_table_names()

    if "purchase_returns" not in tables:
        op.create_table(
            "purchase_returns",
            sa.Column("id", sa.String(length=36), primary_key=True, nullable=False),
            sa.Column("organization_id", sa.String(length=36), sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True),
            sa.Column("return_number", sa.String(length=50), nullable=False, index=True),
            sa.Column("purchase_id", sa.String(length=36), sa.ForeignKey("purchase_invoices.id", ondelete="SET NULL"), nullable=True, index=True),
            sa.Column("supplier_id", sa.String(length=36), sa.ForeignKey("suppliers.id", ondelete="SET NULL"), nullable=True, index=True),
            sa.Column("grn_id", sa.String(length=36), sa.ForeignKey("goods_receipt_notes.id", ondelete="SET NULL"), nullable=True, index=True),
            sa.Column("warehouse_id", sa.String(length=36), sa.ForeignKey("warehouses.id", ondelete="SET NULL"), nullable=True, index=True),
            sa.Column("status", sa.String(length=30), nullable=False, server_default="draft", index=True),
            sa.Column("return_date", sa.DateTime(timezone=True), nullable=False),
            sa.Column("reason", sa.String(length=255), nullable=True),
            sa.Column("notes", sa.Text(), nullable=True),
            sa.Column("cancel_reason", sa.String(length=500), nullable=True),
            sa.Column("stock_deducted", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("created_by", sa.String(length=36), nullable=True),
            sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("confirmed_by", sa.String(length=36), nullable=True),
            sa.Column("dispatched_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("dispatched_by", sa.String(length=36), nullable=True),
            sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("completed_by", sa.String(length=36), nullable=True),
            sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("cancelled_by", sa.String(length=36), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )
        logger.info("Created table purchase_returns")

    if "purchase_return_items" not in tables:
        op.create_table(
            "purchase_return_items",
            sa.Column("id", sa.String(length=36), primary_key=True, nullable=False),
            sa.Column("purchase_return_id", sa.String(length=36), sa.ForeignKey("purchase_returns.id", ondelete="CASCADE"), nullable=False, index=True),
            sa.Column("purchase_item_id", sa.String(length=36), sa.ForeignKey("purchase_invoice_items.id", ondelete="SET NULL"), nullable=True, index=True),
            sa.Column("product_id", sa.String(length=36), sa.ForeignKey("products.id", ondelete="SET NULL"), nullable=True, index=True),
            sa.Column("variant_id", sa.String(length=36), sa.ForeignKey("product_variants.id", ondelete="SET NULL"), nullable=True, index=True),
            sa.Column("product_code", sa.String(length=100), nullable=True),
            sa.Column("barcode", sa.String(length=100), nullable=True),
            sa.Column("product_name", sa.String(length=200), nullable=False),
            sa.Column("unit_of_measure_uom", sa.String(length=30), nullable=True),
            sa.Column("quantity", sa.Integer(), nullable=False),
            sa.Column("unit_price", sa.Float(), nullable=False, server_default="0.0"),
            sa.Column("tax_rate", sa.Float(), nullable=False, server_default="0.0"),
            sa.Column("tax_amount", sa.Float(), nullable=False, server_default="0.0"),
            sa.Column("line_total", sa.Float(), nullable=False, server_default="0.0"),
            sa.Column("reason", sa.String(length=255), nullable=True),
            sa.Column("batch_number", sa.String(length=100), nullable=True),
            sa.Column("serial_numbers", sa.JSON(), nullable=True),
            sa.Column("expiry_date", sa.DateTime(timezone=True), nullable=True),
        )
        logger.info("Created table purchase_return_items")


def downgrade() -> None:
    op.drop_table("purchase_return_items")
    op.drop_table("purchase_returns")
