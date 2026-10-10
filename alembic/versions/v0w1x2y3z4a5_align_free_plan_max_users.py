"""align free plan max users and normalize profit loss key

Revision ID: v0w1x2y3z4a5
Revises: u9v0w1x2y3z4
Create Date: 2026-10-10 10:45:00.000000

"""
import json
import logging
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "v0w1x2y3z4a5"
down_revision: Union[str, Sequence[str], None] = "u9v0w1x2y3z4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

logger = logging.getLogger("alembic.runtime.migration")


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = inspector.get_table_names()

    # 1. Align Free / Basic plan max_users = 1 and normalize canonical keys in stored entitlements
    if "plans" in tables:
        plans_table = sa.table(
            "plans",
            sa.column("id", sa.String),
            sa.column("name", sa.String),
            sa.column("max_users", sa.Integer),
            sa.column("entitlements", sa.JSON),
            sa.column("is_default", sa.Boolean),
        )

        try:
            rows = bind.execute(sa.select(plans_table.c.id, plans_table.c.name, plans_table.c.max_users, plans_table.c.entitlements, plans_table.c.is_default)).fetchall()
            for r in rows:
                plan_id, name, max_users, entitlements, is_default = r[0], r[1], r[2], r[3], r[4]
                name_lower = (name or "").strip().lower()
                update_vals = {}

                # Strict Free/Basic max_users = 1 (no grandfathering)
                if ("free" in name_lower or "basic" in name_lower or is_default is True) and (max_users is None or max_users > 1):
                    update_vals["max_users"] = 1

                # Normalize stored entitlements dictionary key
                if isinstance(entitlements, dict) and "report.profit_and_loss" in entitlements:
                    normalized = dict(entitlements)
                    val = normalized.pop("report.profit_and_loss")
                    if "report.profit_loss" not in normalized:
                        normalized["report.profit_loss"] = val
                    update_vals["entitlements"] = normalized
                elif isinstance(entitlements, str) and "report.profit_and_loss" in entitlements:
                    try:
                        d = json.loads(entitlements)
                        if "report.profit_and_loss" in d:
                            val = d.pop("report.profit_and_loss")
                            if "report.profit_loss" not in d:
                                d["report.profit_loss"] = val
                            update_vals["entitlements"] = d
                    except Exception:
                        pass

                if update_vals:
                    bind.execute(
                        plans_table.update().where(plans_table.c.id == plan_id).values(**update_vals)
                    )
                    logger.info(f"Updated plan {name} ({plan_id}) with {update_vals.keys()}")
        except Exception as e:
            logger.warning(f"Could not align plan max_users or normalize entitlements: {e}")

    # 2. Normalize any existing organization feature overrides for report.profit_and_loss
    if "organization_feature_overrides" in tables:
        overrides_table = sa.table(
            "organization_feature_overrides",
            sa.column("id", sa.String),
            sa.column("organization_id", sa.String),
            sa.column("entitlement_key", sa.String),
        )
        try:
            bind.execute(
                overrides_table.update()
                .where(overrides_table.c.entitlement_key == "report.profit_and_loss")
                .values(entitlement_key="report.profit_loss")
            )
            logger.info("Normalized organization feature overrides for report.profit_loss")
        except Exception as e:
            logger.warning(f"Could not normalize feature overrides: {e}")


def downgrade() -> None:
    pass
