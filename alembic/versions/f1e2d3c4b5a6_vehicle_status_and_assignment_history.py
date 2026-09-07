"""vehicle status and assignment history

Adds status column to vehicles and creates vehicle_assignment_history table.

Revision ID: f1e2d3c4b5a6
Revises: d4e5f6a7b8c9
Create Date: 2026-09-07 16:00:00.000000

"""
import logging
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'f1e2d3c4b5a6'
down_revision: Union[str, Sequence[str], None] = 'd4e5f6a7b8c9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

logger = logging.getLogger("alembic.runtime.migration")

_VEHICLES_TABLE = "vehicles"
_HISTORY_TABLE = "vehicle_assignment_history"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    # 1. Ensure status column on vehicles
    if _VEHICLES_TABLE in inspector.get_table_names():
        existing_cols = {c["name"] for c in inspector.get_columns(_VEHICLES_TABLE)}
        if "status" not in existing_cols:
            logger.info("Adding status column to vehicles table")
            op.add_column(_VEHICLES_TABLE, sa.Column("status", sa.String(length=30), nullable=False, server_default="active"))
            # Backfill existing inactive vehicles
            bind.execute(
                sa.text(f"UPDATE {_VEHICLES_TABLE} SET status = 'inactive' WHERE is_active = 0 OR is_active = FALSE")
            )

    # 2. Ensure vehicle_assignment_history table
    if _HISTORY_TABLE in inspector.get_table_names():
        logger.info("%s already exists -- skipping table creation", _HISTORY_TABLE)
    else:
        logger.info("Creating %s", _HISTORY_TABLE)
        op.create_table(
            _HISTORY_TABLE,
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column(
                "organization_id", sa.String(length=36),
                sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False,
            ),
            sa.Column(
                "vehicle_id", sa.String(length=36),
                sa.ForeignKey("vehicles.id", ondelete="CASCADE"), nullable=False,
            ),
            sa.Column(
                "delivery_partner_id", sa.String(length=36),
                sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True,
            ),
            sa.Column(
                "assigned_by_id", sa.String(length=36),
                sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True,
            ),
            sa.Column("assigned_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("unassigned_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("notes", sa.String(length=500), nullable=True),
        )

    inspector = sa.inspect(bind)
    if _HISTORY_TABLE in inspector.get_table_names():
        existing_indexes = {ix["name"] for ix in inspector.get_indexes(_HISTORY_TABLE)}

        def _ensure_index(name: str, columns: list[str]) -> None:
            if name not in existing_indexes:
                logger.info("Adding index %s", name)
                op.create_index(name, _HISTORY_TABLE, columns)

        _ensure_index("ix_vehicle_assignment_history_organization_id", ["organization_id"])
        _ensure_index("ix_vehicle_assignment_history_vehicle_id", ["vehicle_id"])
        _ensure_index("ix_vehicle_assignment_history_delivery_partner_id", ["delivery_partner_id"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if _HISTORY_TABLE in inspector.get_table_names():
        op.drop_table(_HISTORY_TABLE)

    if _VEHICLES_TABLE in inspector.get_table_names():
        existing_cols = {c["name"] for c in inspector.get_columns(_VEHICLES_TABLE)}
        if "status" in existing_cols:
            with op.batch_alter_table(_VEHICLES_TABLE) as batch_op:
                batch_op.drop_column("status")
