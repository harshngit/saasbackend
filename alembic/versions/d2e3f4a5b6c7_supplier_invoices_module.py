"""supplier invoices module tables

Creates supplier_invoices and supplier_invoice_items tables with unique constraint uq_org_supplier_invoice_num.

Revision ID: d2e3f4a5b6c7
Revises: c1f2e3d4b5a6
Create Date: 2026-09-08 17:00:00.000000

"""
import logging
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'd2e3f4a5b6c7'
down_revision: Union[str, Sequence[str], None] = 'c1f2e3d4b5a6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

logger = logging.getLogger("alembic.runtime.migration")

_INV_TABLE = "supplier_invoices"
_ITEM_TABLE = "supplier_invoice_items"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if _INV_TABLE not in inspector.get_table_names():
        logger.info("Creating %s", _INV_TABLE)
        op.create_table(
            _INV_TABLE,
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column(
                "organization_id", sa.String(length=36),
                sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False,
            ),
            sa.Column(
                "supplier_id", sa.String(length=36),
                sa.ForeignKey("suppliers.id", ondelete="RESTRICT"), nullable=False,
            ),
            sa.Column(
                "purchase_id", sa.String(length=36),
                sa.ForeignKey("purchase_invoices.id", ondelete="RESTRICT"), nullable=False,
            ),
            sa.Column("supplier_invoice_number", sa.String(length=100), nullable=False),
            sa.Column("supplier_invoice_date", sa.DateTime(timezone=True), nullable=False),
            sa.Column("due_date", sa.DateTime(timezone=True), nullable=True),
            sa.Column("status", sa.String(length=20), nullable=False, server_default="draft"),
            sa.Column("verification_status", sa.String(length=20), nullable=False, server_default="pending"),
            sa.Column("payment_status", sa.String(length=20), nullable=False, server_default="unpaid"),
            sa.Column("subtotal", sa.Float(), nullable=False, server_default="0.0"),
            sa.Column("tax_amount", sa.Float(), nullable=False, server_default="0.0"),
            sa.Column("discount_amount", sa.Float(), nullable=False, server_default="0.0"),
            sa.Column("grand_total", sa.Float(), nullable=False, server_default="0.0"),
            sa.Column("amount_paid", sa.Float(), nullable=False, server_default="0.0"),
            sa.Column("notes", sa.Text(), nullable=True),
            sa.Column("attachment_url", sa.Text(), nullable=True),
            sa.Column("created_by", sa.String(length=36), nullable=True),
            sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("recorded_by", sa.String(length=36), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("organization_id", "supplier_id", "supplier_invoice_number", name="uq_org_supplier_invoice_num"),
        )

    if _ITEM_TABLE not in inspector.get_table_names():
        logger.info("Creating %s", _ITEM_TABLE)
        op.create_table(
            _ITEM_TABLE,
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column(
                "supplier_invoice_id", sa.String(length=36),
                sa.ForeignKey("supplier_invoices.id", ondelete="CASCADE"), nullable=False,
            ),
            sa.Column(
                "purchase_item_id", sa.String(length=36),
                sa.ForeignKey("purchase_invoice_items.id", ondelete="CASCADE"), nullable=False,
            ),
            sa.Column(
                "product_id", sa.String(length=36),
                sa.ForeignKey("products.id", ondelete="SET NULL"), nullable=True,
            ),
            sa.Column(
                "variant_id", sa.String(length=36),
                sa.ForeignKey("product_variants.id", ondelete="SET NULL"), nullable=True,
            ),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("billed_qty", sa.Integer(), nullable=False),
            sa.Column("unit_price", sa.Float(), nullable=False, server_default="0.0"),
            sa.Column("tax_rate", sa.Float(), nullable=False, server_default="0.0"),
            sa.Column("tax_amount", sa.Float(), nullable=False, server_default="0.0"),
            sa.Column("discount_amount", sa.Float(), nullable=False, server_default="0.0"),
            sa.Column("line_total", sa.Float(), nullable=False, server_default="0.0"),
        )

    inspector = sa.inspect(bind)
    if _INV_TABLE in inspector.get_table_names():
        existing_indexes = {ix["name"] for ix in inspector.get_indexes(_INV_TABLE)}
        def _ensure_inv_index(name: str, columns: list[str]) -> None:
            if name not in existing_indexes:
                op.create_index(name, _INV_TABLE, columns)

        _ensure_inv_index("ix_supplier_invoices_org_id", ["organization_id"])
        _ensure_inv_index("ix_supplier_invoices_supplier_id", ["supplier_id"])
        _ensure_inv_index("ix_supplier_invoices_purchase_id", ["purchase_id"])
        _ensure_inv_index("ix_supplier_invoices_inv_num", ["supplier_invoice_number"])
        _ensure_inv_index("ix_supplier_invoices_status", ["status"])

    if _ITEM_TABLE in inspector.get_table_names():
        existing_indexes = {ix["name"] for ix in inspector.get_indexes(_ITEM_TABLE)}
        def _ensure_item_index(name: str, columns: list[str]) -> None:
            if name not in existing_indexes:
                op.create_index(name, _ITEM_TABLE, columns)

        _ensure_item_index("ix_supplier_invoice_items_inv_id", ["supplier_invoice_id"])
        _ensure_item_index("ix_supplier_invoice_items_pur_item_id", ["purchase_item_id"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if _ITEM_TABLE in inspector.get_table_names():
        op.drop_table(_ITEM_TABLE)

    if _INV_TABLE in inspector.get_table_names():
        op.drop_table(_INV_TABLE)
