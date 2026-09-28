"""catalog hierarchy associations (supplier_brands and brand_categories)

Creates supplier_brands and brand_categories tables and backfills associations
from existing supplier_products and products records.

Revision ID: h6c7d8e9f0a1
Revises: g5b6c7d8e9f0
Create Date: 2026-09-28 12:00:00.000000

"""
from datetime import datetime, timezone
import logging
from typing import Sequence, Union
import uuid

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'h6c7d8e9f0a1'
down_revision: Union[str, Sequence[str], None] = 'g5b6c7d8e9f0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

logger = logging.getLogger("alembic.runtime.migration")

_SUPPLIER_BRANDS_TABLE = "supplier_brands"
_BRAND_CATEGORIES_TABLE = "brand_categories"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    # 1. Create supplier_brands table
    if _SUPPLIER_BRANDS_TABLE in inspector.get_table_names():
        logger.info("%s already exists -- skipping creation", _SUPPLIER_BRANDS_TABLE)
    else:
        logger.info("Creating %s", _SUPPLIER_BRANDS_TABLE)
        op.create_table(
            _SUPPLIER_BRANDS_TABLE,
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
                "brand_id", sa.String(length=36),
                sa.ForeignKey("brands.id", ondelete="CASCADE"), nullable=False,
            ),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("supplier_id", "brand_id", name="uq_supplier_brand"),
        )

    # 2. Create brand_categories table
    if _BRAND_CATEGORIES_TABLE in inspector.get_table_names():
        logger.info("%s already exists -- skipping creation", _BRAND_CATEGORIES_TABLE)
    else:
        logger.info("Creating %s", _BRAND_CATEGORIES_TABLE)
        op.create_table(
            _BRAND_CATEGORIES_TABLE,
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column(
                "organization_id", sa.String(length=36),
                sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False,
            ),
            sa.Column(
                "brand_id", sa.String(length=36),
                sa.ForeignKey("brands.id", ondelete="CASCADE"), nullable=False,
            ),
            sa.Column(
                "category_id", sa.String(length=36),
                sa.ForeignKey("categories.id", ondelete="CASCADE"), nullable=False,
            ),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("brand_id", "category_id", name="uq_brand_category"),
        )

    # 3. Ensure indexes
    inspector = sa.inspect(bind)
    if _SUPPLIER_BRANDS_TABLE in inspector.get_table_names():
        existing_sb_indexes = {ix["name"] for ix in inspector.get_indexes(_SUPPLIER_BRANDS_TABLE)}
        if "ix_supplier_brands_organization_id" not in existing_sb_indexes:
            op.create_index("ix_supplier_brands_organization_id", _SUPPLIER_BRANDS_TABLE, ["organization_id"])
        if "ix_supplier_brands_supplier_id" not in existing_sb_indexes:
            op.create_index("ix_supplier_brands_supplier_id", _SUPPLIER_BRANDS_TABLE, ["supplier_id"])
        if "ix_supplier_brands_brand_id" not in existing_sb_indexes:
            op.create_index("ix_supplier_brands_brand_id", _SUPPLIER_BRANDS_TABLE, ["brand_id"])

    if _BRAND_CATEGORIES_TABLE in inspector.get_table_names():
        existing_bc_indexes = {ix["name"] for ix in inspector.get_indexes(_BRAND_CATEGORIES_TABLE)}
        if "ix_brand_categories_organization_id" not in existing_bc_indexes:
            op.create_index("ix_brand_categories_organization_id", _BRAND_CATEGORIES_TABLE, ["organization_id"])
        if "ix_brand_categories_brand_id" not in existing_bc_indexes:
            op.create_index("ix_brand_categories_brand_id", _BRAND_CATEGORIES_TABLE, ["brand_id"])
        if "ix_brand_categories_category_id" not in existing_bc_indexes:
            op.create_index("ix_brand_categories_category_id", _BRAND_CATEGORIES_TABLE, ["category_id"])

    # 4. Backfill supplier_brands from supplier_products + products + brands
    table_names = inspector.get_table_names()
    if "supplier_products" in table_names and "products" in table_names and "brands" in table_names:
        logger.info("Backfilling supplier_brands from existing supplier_products and products")
        sb_rows = bind.execute(sa.text("""
            SELECT DISTINCT sp.organization_id, sp.supplier_id, p.brand_id
            FROM supplier_products sp
            JOIN products p ON sp.product_id = p.id
            JOIN brands b ON p.brand_id = b.id
            WHERE p.brand_id IS NOT NULL
              AND sp.organization_id = p.organization_id
              AND p.organization_id = b.organization_id
        """)).fetchall()

        for row in sb_rows:
            org_id, supplier_id, brand_id = row[0], row[1], row[2]
            exists = bind.execute(sa.text("""
                SELECT 1 FROM supplier_brands
                WHERE supplier_id = :s_id AND brand_id = :b_id
            """), {"s_id": supplier_id, "b_id": brand_id}).first()
            if not exists:
                bind.execute(sa.text("""
                    INSERT INTO supplier_brands (id, organization_id, supplier_id, brand_id, created_at)
                    VALUES (:id, :org_id, :supplier_id, :brand_id, :created_at)
                """), {
                    "id": str(uuid.uuid4()),
                    "org_id": org_id,
                    "supplier_id": supplier_id,
                    "brand_id": brand_id,
                    "created_at": datetime.now(timezone.utc),
                })

    # 5. Backfill brand_categories from products + brands + categories
    if "products" in table_names and "brands" in table_names and "categories" in table_names:
        logger.info("Backfilling brand_categories from existing products, brands and categories")
        bc_rows = bind.execute(sa.text("""
            SELECT DISTINCT p.organization_id, p.brand_id, p.category_id
            FROM products p
            JOIN brands b ON p.brand_id = b.id
            JOIN categories c ON p.category_id = c.id
            WHERE p.brand_id IS NOT NULL
              AND p.category_id IS NOT NULL
              AND p.organization_id = b.organization_id
              AND p.organization_id = c.organization_id
        """)).fetchall()

        for row in bc_rows:
            org_id, brand_id, category_id = row[0], row[1], row[2]
            exists = bind.execute(sa.text("""
                SELECT 1 FROM brand_categories
                WHERE brand_id = :b_id AND category_id = :c_id
            """), {"b_id": brand_id, "c_id": category_id}).first()
            if not exists:
                bind.execute(sa.text("""
                    INSERT INTO brand_categories (id, organization_id, brand_id, category_id, created_at)
                    VALUES (:id, :org_id, :brand_id, :category_id, :created_at)
                """), {
                    "id": str(uuid.uuid4()),
                    "org_id": org_id,
                    "brand_id": brand_id,
                    "category_id": category_id,
                    "created_at": datetime.now(timezone.utc),
                })


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if _BRAND_CATEGORIES_TABLE in inspector.get_table_names():
        op.drop_table(_BRAND_CATEGORIES_TABLE)

    if _SUPPLIER_BRANDS_TABLE in inspector.get_table_names():
        op.drop_table(_SUPPLIER_BRANDS_TABLE)
