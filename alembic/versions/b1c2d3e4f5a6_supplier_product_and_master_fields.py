"""supplier product relationship and missing master fields

Adds supplier_products table and adds company_name, pan_number, state, pincode,
country, supplier_type, payment_terms, credit_limit, notes columns to suppliers table.

Revision ID: b1c2d3e4f5a6
Revises: f1e2d3c4b5a6
Create Date: 2026-09-07 18:10:00.000000

"""
import logging
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'b1c2d3e4f5a6'
down_revision: Union[str, Sequence[str], None] = 'f1e2d3c4b5a6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

logger = logging.getLogger("alembic.runtime.migration")

_SUPPLIERS_TABLE = "suppliers"
_LINK_TABLE = "supplier_products"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    # 1. Ensure new master columns on suppliers table
    if _SUPPLIERS_TABLE in inspector.get_table_names():
        existing_cols = {c["name"] for c in inspector.get_columns(_SUPPLIERS_TABLE)}
        new_columns = [
            ("company_name", sa.String(length=200)),
            ("pan_number", sa.String(length=20)),
            ("state", sa.String(length=100)),
            ("pincode", sa.String(length=20)),
            ("country", sa.String(length=100)),
            ("supplier_type", sa.String(length=100)),
            ("payment_terms", sa.String(length=100)),
            ("credit_limit", sa.Float()),
            ("notes", sa.Text()),
        ]
        for col_name, col_type in new_columns:
            if col_name not in existing_cols:
                logger.info("Adding column %s to %s table", col_name, _SUPPLIERS_TABLE)
                op.add_column(_SUPPLIERS_TABLE, sa.Column(col_name, col_type, nullable=True))

    # 2. Ensure supplier_products table
    if _LINK_TABLE in inspector.get_table_names():
        logger.info("%s already exists -- skipping table creation", _LINK_TABLE)
    else:
        logger.info("Creating %s", _LINK_TABLE)
        op.create_table(
            _LINK_TABLE,
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column(
                "organization_id", sa.String(length=36),
                sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False,
            ),
            sa.Column(
                "supplier_id", sa.String(length=36),
                sa.ForeignKey("suppliers.id", ondelete="CASCADE"), nullable=False,
            ),
            sa.Column(
                "product_id", sa.String(length=36),
                sa.ForeignKey("products.id", ondelete="CASCADE"), nullable=False,
            ),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("supplier_id", "product_id", name="uq_supplier_product"),
        )

    inspector = sa.inspect(bind)
    if _LINK_TABLE in inspector.get_table_names():
        existing_indexes = {ix["name"] for ix in inspector.get_indexes(_LINK_TABLE)}

        def _ensure_index(name: str, columns: list[str]) -> None:
            if name not in existing_indexes:
                logger.info("Adding index %s", name)
                op.create_index(name, _LINK_TABLE, columns)

        _ensure_index("ix_supplier_products_organization_id", ["organization_id"])
        _ensure_index("ix_supplier_products_supplier_id", ["supplier_id"])
        _ensure_index("ix_supplier_products_product_id", ["product_id"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if _LINK_TABLE in inspector.get_table_names():
        op.drop_table(_LINK_TABLE)

    if _SUPPLIERS_TABLE in inspector.get_table_names():
        existing_cols = {c["name"] for c in inspector.get_columns(_SUPPLIERS_TABLE)}
        cols_to_drop = [
            "company_name", "pan_number", "state", "pincode", "country",
            "supplier_type", "payment_terms", "credit_limit", "notes",
        ]
        with op.batch_alter_table(_SUPPLIERS_TABLE) as batch_op:
            for col in cols_to_drop:
                if col in existing_cols:
                    batch_op.drop_column(col)
