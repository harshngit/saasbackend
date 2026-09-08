"""goods receipt notes module tables

Creates goods_receipt_notes and goods_receipt_note_items tables.

Revision ID: c1f2e3d4b5a6
Revises: e2f3a4b5c6d7
Create Date: 2026-09-08 12:00:00.000000

"""
import logging
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'c1f2e3d4b5a6'
down_revision: Union[str, Sequence[str], None] = 'e2f3a4b5c6d7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

logger = logging.getLogger("alembic.runtime.migration")

_GRN_TABLE = "goods_receipt_notes"
_GRN_ITEM_TABLE = "goods_receipt_note_items"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if _GRN_TABLE not in inspector.get_table_names():
        logger.info("Creating %s", _GRN_TABLE)
        op.create_table(
            _GRN_TABLE,
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column(
                "organization_id", sa.String(length=36),
                sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False,
            ),
            sa.Column("grn_number", sa.String(length=100), nullable=False),
            sa.Column(
                "purchase_id", sa.String(length=36),
                sa.ForeignKey("purchase_invoices.id", ondelete="RESTRICT"), nullable=False,
            ),
            sa.Column(
                "supplier_id", sa.String(length=36),
                sa.ForeignKey("suppliers.id", ondelete="RESTRICT"), nullable=False,
            ),
            sa.Column(
                "warehouse_id", sa.String(length=36),
                sa.ForeignKey("warehouses.id", ondelete="RESTRICT"), nullable=False,
            ),
            sa.Column("status", sa.String(length=20), nullable=False, server_default="draft"),
            sa.Column("received_date", sa.DateTime(timezone=True), nullable=True),
            sa.Column("notes", sa.Text(), nullable=True),
            sa.Column(
                "created_by", sa.String(length=36),
                sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True,
            ),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column(
                "confirmed_by", sa.String(length=36),
                sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True,
            ),
        )

    if _GRN_ITEM_TABLE not in inspector.get_table_names():
        logger.info("Creating %s", _GRN_ITEM_TABLE)
        op.create_table(
            _GRN_ITEM_TABLE,
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column(
                "organization_id", sa.String(length=36),
                sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False,
            ),
            sa.Column(
                "grn_id", sa.String(length=36),
                sa.ForeignKey("goods_receipt_notes.id", ondelete="CASCADE"), nullable=False,
            ),
            sa.Column(
                "purchase_item_id", sa.String(length=36),
                sa.ForeignKey("purchase_invoice_items.id", ondelete="RESTRICT"), nullable=False,
            ),
            sa.Column(
                "product_id", sa.String(length=36),
                sa.ForeignKey("products.id", ondelete="RESTRICT"), nullable=False,
            ),
            sa.Column(
                "variant_id", sa.String(length=36),
                sa.ForeignKey("product_variants.id", ondelete="RESTRICT"), nullable=True,
            ),
            sa.Column("received_qty", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("damaged_qty", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("rejected_qty", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("accepted_qty", sa.Integer(), nullable=False, server_default="0"),
        )

    inspector = sa.inspect(bind)
    if _GRN_TABLE in inspector.get_table_names():
        existing_indexes = {ix["name"] for ix in inspector.get_indexes(_GRN_TABLE)}
        def _ensure_grn_index(name: str, columns: list[str]) -> None:
            if name not in existing_indexes:
                op.create_index(name, _GRN_TABLE, columns)

        _ensure_grn_index("ix_goods_receipt_notes_org_grn_num", ["organization_id", "grn_number"])
        _ensure_grn_index("ix_goods_receipt_notes_purchase_id", ["purchase_id"])
        _ensure_grn_index("ix_goods_receipt_notes_supplier_id", ["supplier_id"])
        _ensure_grn_index("ix_goods_receipt_notes_warehouse_id", ["warehouse_id"])
        _ensure_grn_index("ix_goods_receipt_notes_status", ["status"])

    if _GRN_ITEM_TABLE in inspector.get_table_names():
        existing_indexes = {ix["name"] for ix in inspector.get_indexes(_GRN_ITEM_TABLE)}
        def _ensure_grn_item_index(name: str, columns: list[str]) -> None:
            if name not in existing_indexes:
                op.create_index(name, _GRN_ITEM_TABLE, columns)

        _ensure_grn_item_index("ix_goods_receipt_note_items_grn_id", ["grn_id"])
        _ensure_grn_item_index("ix_goods_receipt_note_items_purchase_item_id", ["purchase_item_id"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if _GRN_ITEM_TABLE in inspector.get_table_names():
        op.drop_table(_GRN_ITEM_TABLE)

    if _GRN_TABLE in inspector.get_table_names():
        op.drop_table(_GRN_TABLE)
