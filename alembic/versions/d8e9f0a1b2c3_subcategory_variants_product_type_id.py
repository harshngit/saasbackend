"""subcategory_variants table, and products.product_type_id pointing at it

A sub category's Product Type options (Casual Footwear -> Shoe / Cheppal / Sandal) get
their own table, `subcategory_variants` -- category master data, unrelated to
`product_variants`. Products gain a nullable `product_type_id` referencing it (ON DELETE
SET NULL); the free-text `products.product_type` is left exactly as it was, and nothing
is mapped from it.

Guarded throughout (alembic/README.md), following 8175f96a08e4_quotations_lead_id_fk.py:
safe on a database whose app startup already created the table (create_all) or added the
column (auto_add_missing_columns).

Revision ID: d8e9f0a1b2c3
Revises: c7d8e9f0a1b2
Create Date: 2026-10-24 12:00:00.000000

"""
import logging
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d8e9f0a1b2c3"
down_revision: Union[str, Sequence[str], None] = "c7d8e9f0a1b2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

logger = logging.getLogger("alembic.runtime.migration")

_VARIANTS = "subcategory_variants"
_PRODUCTS = "products"
_COLUMN = "product_type_id"
_FK_NAME = "fk_products_product_type_id_subcategory_variants"
_INDEX_NAME = "ix_products_product_type_id"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = inspector.get_table_names()

    if _VARIANTS not in tables:
        logger.info("Creating %s", _VARIANTS)
        op.create_table(
            _VARIANTS,
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("organization_id", sa.String(length=36),
                      sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
            sa.Column("subcategory_id", sa.String(length=36),
                      sa.ForeignKey("categories.id", ondelete="SET NULL"), nullable=True),
            sa.Column("name", sa.String(length=150), nullable=False),
            sa.Column("name_key", sa.String(length=150), nullable=False),
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("organization_id", "subcategory_id", "name_key", name="uq_subcategory_variant_name"),
        )
    variant_indexes = {ix["name"] for ix in sa.inspect(bind).get_indexes(_VARIANTS)}
    for name, column in (("ix_subcategory_variants_organization_id", "organization_id"),
                         ("ix_subcategory_variants_subcategory_id", "subcategory_id")):
        if name not in variant_indexes:
            op.create_index(name, _VARIANTS, [column])

    if _PRODUCTS not in tables:
        logger.info("%s table does not exist yet -- skipping %s", _PRODUCTS, _COLUMN)
        return

    if _COLUMN not in {c["name"] for c in inspector.get_columns(_PRODUCTS)}:
        logger.info("Adding %s.%s", _PRODUCTS, _COLUMN)
        op.add_column(_PRODUCTS, sa.Column(_COLUMN, sa.String(length=36), nullable=True))
    inspector = sa.inspect(bind)

    # A column added earlier by auto_add_missing_columns could hold ids of rows that no
    # longer exist; clear those (matching ON DELETE SET NULL) before adding the constraint.
    orphan_condition = f'"{_COLUMN}" IS NOT NULL AND "{_COLUMN}" NOT IN (SELECT "id" FROM "{_VARIANTS}")'
    orphans = bind.execute(sa.text(f'SELECT COUNT(*) FROM "{_PRODUCTS}" WHERE {orphan_condition}')).scalar()
    if orphans:
        bind.execute(sa.text(f'UPDATE "{_PRODUCTS}" SET "{_COLUMN}" = NULL WHERE {orphan_condition}'))
        logger.warning("Cleared %d orphaned %s.%s value(s)", orphans, _PRODUCTS, _COLUMN)

    has_fk = any(
        fk["constrained_columns"] == [_COLUMN] and fk["referred_table"] == _VARIANTS
        for fk in inspector.get_foreign_keys(_PRODUCTS)
    )
    if not has_fk:
        logger.info("Adding FK %s (%s.%s -> %s.id) ON DELETE SET NULL", _FK_NAME, _PRODUCTS, _COLUMN, _VARIANTS)
        if bind.dialect.name == "sqlite":
            with op.batch_alter_table(_PRODUCTS, schema=None) as batch_op:
                batch_op.create_foreign_key(_FK_NAME, _VARIANTS, [_COLUMN], ["id"], ondelete="SET NULL")
        else:
            op.create_foreign_key(_FK_NAME, _PRODUCTS, _VARIANTS, [_COLUMN], ["id"], ondelete="SET NULL")

    if _INDEX_NAME not in {ix["name"] for ix in sa.inspect(bind).get_indexes(_PRODUCTS)}:
        op.create_index(_INDEX_NAME, _PRODUCTS, [_COLUMN])


def downgrade() -> None:
    """Reverses what this revision is guaranteed to have added: the products index and FK,
    and the subcategory_variants table. products.product_type_id itself is kept
    (alembic/README.md rule 5) -- app startup may have added it, and it may hold data."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = inspector.get_table_names()

    if _PRODUCTS in tables:
        if _INDEX_NAME in {ix["name"] for ix in inspector.get_indexes(_PRODUCTS)}:
            op.drop_index(_INDEX_NAME, table_name=_PRODUCTS)
        if _FK_NAME in {fk["name"] for fk in inspector.get_foreign_keys(_PRODUCTS) if fk["name"]}:
            if bind.dialect.name == "sqlite":
                with op.batch_alter_table(_PRODUCTS, schema=None) as batch_op:
                    batch_op.drop_constraint(_FK_NAME, type_="foreignkey")
            else:
                op.drop_constraint(_FK_NAME, _PRODUCTS, type_="foreignkey")

    if _VARIANTS in tables:
        op.drop_table(_VARIANTS)
