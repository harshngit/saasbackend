"""plan entitlements and org overrides

Revision ID: u9v0w1x2y3z4
Revises: t8u9v0w1x2y3
Create Date: 2026-10-09 18:00:00.000000

"""
import json
import logging
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from app.core.entitlements import (
    default_entitlements_for_plan,
)

# revision identifiers, used by Alembic.
revision: str = "u9v0w1x2y3z4"
down_revision: Union[str, Sequence[str], None] = "t8u9v0w1x2y3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

logger = logging.getLogger("alembic.runtime.migration")


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = inspector.get_table_names()

    # 1. Extend plans table
    if "plans" in tables:
        columns = [c["name"] for c in inspector.get_columns("plans")]
        if "entitlements" not in columns:
            op.add_column(
                "plans",
                sa.Column("entitlements", sa.JSON(), nullable=True, server_default="{}"),
            )
            logger.info("Added entitlements column to plans")
        if "max_warehouses" not in columns:
            op.add_column(
                "plans",
                sa.Column("max_warehouses", sa.Integer(), nullable=True),
            )
            logger.info("Added max_warehouses column to plans")

    # 2. Create organization_feature_overrides table
    if "organization_feature_overrides" not in tables:
        op.create_table(
            "organization_feature_overrides",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("organization_id", sa.String(length=36), sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True),
            sa.Column("entitlement_key", sa.String(length=100), nullable=False, index=True),
            sa.Column("effect", sa.String(length=10), server_default="ALLOW", nullable=False),
            sa.Column("is_allowed", sa.Boolean(), server_default=sa.true(), nullable=False),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("reason", sa.String(length=500), nullable=True),
            sa.Column("created_by_user_id", sa.String(length=36), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("organization_id", "entitlement_key", name="uq_org_feature_override"),
        )
        logger.info("Created organization_feature_overrides table")

    # 3. Create organization_limit_overrides table
    if "organization_limit_overrides" not in tables:
        op.create_table(
            "organization_limit_overrides",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("organization_id", sa.String(length=36), sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True),
            sa.Column("limit_key", sa.String(length=100), nullable=False, index=True),
            sa.Column("value", sa.Integer(), nullable=True),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("reason", sa.String(length=500), nullable=True),
            sa.Column("created_by_user_id", sa.String(length=36), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("organization_id", "limit_key", name="uq_org_limit_override"),
        )
        logger.info("Created organization_limit_overrides table")

    # 4. Safe backfill of existing plans
    try:
        plans_table = sa.table(
            "plans",
            sa.column("id", sa.String),
            sa.column("name", sa.String),
            sa.column("max_users", sa.Integer),
            sa.column("max_warehouses", sa.Integer),
            sa.column("entitlements", sa.JSON),
        )

        rows = bind.execute(sa.select(plans_table.c.id, plans_table.c.name, plans_table.c.entitlements, plans_table.c.max_warehouses, plans_table.c.max_users)).fetchall()
        for r in rows:
            plan_id, name, existing_ent, existing_warehouses, existing_users = r[0], r[1], r[2], r[3], r[4]
            # If entitlements is empty or None, backfill
            if not existing_ent or existing_ent == "{}" or existing_ent == {}:
                target_ent = default_entitlements_for_plan(name)
                update_vals = {"entitlements": target_ent}

                # Update max_warehouses / max_users if not set
                if existing_warehouses is None:
                    name_lower = (name or "").lower()
                    if "basic" in name_lower or "free" in name_lower:
                        update_vals["max_warehouses"] = 1
                    else:
                        update_vals["max_warehouses"] = None

                if existing_users is None and "basic" in (name or "").lower():
                    update_vals["max_users"] = 1

                bind.execute(
                    plans_table.update().where(plans_table.c.id == plan_id).values(**update_vals)
                )
                logger.info(f"Backfilled entitlements for plan {name} ({plan_id})")
    except Exception as e:
        logger.warning(f"Could not backfill plan entitlements: {e}")


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = inspector.get_table_names()

    if "organization_limit_overrides" in tables:
        op.drop_table("organization_limit_overrides")
        logger.info("Dropped organization_limit_overrides table")

    if "organization_feature_overrides" in tables:
        op.drop_table("organization_feature_overrides")
        logger.info("Dropped organization_feature_overrides table")

    if "plans" in tables:
        columns = [c["name"] for c in inspector.get_columns("plans")]
        if "max_warehouses" in columns:
            op.drop_column("plans", "max_warehouses")
        if "entitlements" in columns:
            op.drop_column("plans", "entitlements")
