"""product variant business IDs (VAR-0001), organization_id and pricing; product_type cleanup

Adds to product_variants:
- organization_id (owning product's organization, backfilled from products)
- variant_code (VAR-0001, numbered per organization, backfilled; VAR sequence synced)
- purchase_price, mrp, wholesale_price, dealer_price, discount_percent,
  tax_inclusive, currency (nullable; nothing is invented for existing rows --
  the existing `price` column already is the variant's selling price)
- unique index uq_product_variants_org_variant_code on (organization_id, variant_code),
  created only after every row has a code, and NOT NULL on organization_id /
  variant_code (PostgreSQL; SQLite cannot alter nullability in place without
  rebuilding a table that other tables reference).

Also clears products.product_type where it is a copy of the row's own category_id
or sub_category_id -- a category id, never a product type, and the category is
still recorded in those columns. Any other product_type value is left untouched.

Every step aborts the migration on failure rather than logging and continuing, so a
partial backfill can never be followed by a missing unique index.

Revision ID: a5b6c7d8e9f0
Revises: z4a5b6c7d8e9
Create Date: 2026-10-16 12:00:00.000000

"""
import logging
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a5b6c7d8e9f0"
down_revision: Union[str, Sequence[str], None] = "z4a5b6c7d8e9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

logger = logging.getLogger("alembic.runtime.migration")

_TABLE = "product_variants"
_UNIQUE_INDEX = "uq_product_variants_org_variant_code"
_ORG_FK = "fk_product_variants_organization_id"

_NEW_COLUMNS = [
    ("organization_id", sa.String(length=36)),
    ("variant_code", sa.String(length=50)),
    ("purchase_price", sa.Float()),
    ("mrp", sa.Float()),
    ("wholesale_price", sa.Float()),
    ("dealer_price", sa.Float()),
    ("discount_percent", sa.Float()),
    ("tax_inclusive", sa.Boolean()),
    ("currency", sa.String(length=10)),
]


def upgrade() -> None:
    from app.scripts.backfill_variant_codes import backfill_variant_codes

    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if _TABLE not in inspector.get_table_names():
        return
    is_postgres = bind.dialect.name == "postgresql"

    # 1. Columns -- nullable for now; a booting app's auto_add_missing_columns may
    #    already have added some of them (with '' for the NOT NULL strings).
    existing = {c["name"] for c in inspector.get_columns(_TABLE)}
    for name, col_type in _NEW_COLUMNS:
        if name not in existing:
            logger.info("Adding column %s.%s", _TABLE, name)
            op.add_column(_TABLE, sa.Column(name, col_type, nullable=True))

    if is_postgres:
        fks = {fk["name"] for fk in sa.inspect(bind).get_foreign_keys(_TABLE)}
        if _ORG_FK not in fks:
            op.create_foreign_key(
                _ORG_FK, _TABLE, "organizations", ["organization_id"], ["id"], ondelete="CASCADE"
            )

    indexes = {ix["name"] for ix in sa.inspect(bind).get_indexes(_TABLE)}
    for name, column in (
        ("ix_product_variants_organization_id", "organization_id"),
        ("ix_product_variants_variant_code", "variant_code"),
    ):
        if name not in indexes:
            op.create_index(name, _TABLE, [column])

    # 2. Backfill organization_id and variant_code, and sync the VAR sequences.
    result = backfill_variant_codes(bind)
    logger.info(
        "Variant backfill: %d codes assigned across %d organizations, %d organization_id values derived",
        result["codes_assigned"], result["organizations"], result["organization_ids_fixed"],
    )

    missing = bind.execute(
        sa.text(
            f"SELECT COUNT(*) FROM {_TABLE} WHERE organization_id IS NULL OR organization_id = '' "
            "OR variant_code IS NULL OR variant_code = ''"
        )
    ).scalar()
    if missing:
        raise RuntimeError(f"{missing} product variants still lack organization_id/variant_code")

    # 3. Uniqueness, only now that every row has a valid code.
    if _UNIQUE_INDEX not in indexes:
        op.create_index(_UNIQUE_INDEX, _TABLE, ["organization_id", "variant_code"], unique=True)
    if is_postgres:
        op.alter_column(_TABLE, "organization_id", existing_type=sa.String(length=36), nullable=False)
        op.alter_column(_TABLE, "variant_code", existing_type=sa.String(length=50), nullable=False)

    # 4. product_type holding the row's own category / sub category id.
    if "products" in inspector.get_table_names():
        cleared = bind.execute(
            sa.text(
                "UPDATE products SET product_type = NULL WHERE product_type IS NOT NULL "
                "AND (product_type = category_id OR product_type = sub_category_id)"
            )
        ).rowcount
        logger.info("Cleared %s product_type values that were a copy of the category id", cleared)


def downgrade() -> None:
    # Reverses only the constraints. The columns stay: a database booted by the app
    # before this migration ran may already have had them (see alembic/README, rule 5),
    # and the cleared product_type values were category ids, not data to restore.
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if _TABLE not in inspector.get_table_names():
        return
    if _UNIQUE_INDEX in {ix["name"] for ix in inspector.get_indexes(_TABLE)}:
        op.drop_index(_UNIQUE_INDEX, table_name=_TABLE)
    if bind.dialect.name == "postgresql":
        op.alter_column(_TABLE, "organization_id", existing_type=sa.String(length=36), nullable=True)
        op.alter_column(_TABLE, "variant_code", existing_type=sa.String(length=50), nullable=True)
