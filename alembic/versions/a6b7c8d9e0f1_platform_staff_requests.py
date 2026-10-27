"""platform staff requests: platform_upgrade_requests and platform_feature_requests

Platform Sales Managers (users.system_role = 'sales_manager', no organization) raise
plan-upgrade and feature-change requests for an organization; a Super Admin approves or
rejects them. The role needs no schema change (system_role is a String(20)).

Guarded throughout (alembic/README.md): safe on a database whose app startup already
created the tables (create_all).

Revision ID: a6b7c8d9e0f1
Revises: f5a6b7c8d9e0
Create Date: 2026-10-27 18:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a6b7c8d9e0f1"
down_revision: Union[str, Sequence[str], None] = "f5a6b7c8d9e0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_UPGRADE = "platform_upgrade_requests"
_FEATURE = "platform_feature_requests"


def _decision_columns() -> list[sa.Column]:
    return [
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("requested_by_user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("decided_by_user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decision_note", sa.String(500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    ]


_INDEXES = {
    _UPGRADE: ["organization_id", "status", "requested_by_user_id", "created_at"],
    _FEATURE: ["organization_id", "status", "requested_by_user_id", "created_at"],
}


def upgrade() -> None:
    bind = op.get_bind()
    tables = sa.inspect(bind).get_table_names()

    if _UPGRADE not in tables:
        op.create_table(
            _UPGRADE,
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("organization_id", sa.String(36),
                      sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
            sa.Column("current_plan_id", sa.String(36), sa.ForeignKey("plans.id", ondelete="SET NULL"), nullable=True),
            sa.Column("requested_plan_id", sa.String(36), sa.ForeignKey("plans.id", ondelete="SET NULL"), nullable=True),
            sa.Column("billing_cycle", sa.String(10), nullable=True),
            sa.Column("reason", sa.String(500), nullable=False),
            sa.Column("notes", sa.Text(), nullable=True),
            *_decision_columns(),
        )
    if _FEATURE not in tables:
        op.create_table(
            _FEATURE,
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("organization_id", sa.String(36),
                      sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
            sa.Column("entitlement_key", sa.String(100), nullable=False),
            sa.Column("action", sa.String(10), nullable=False),
            sa.Column("reason", sa.String(500), nullable=False),
            *_decision_columns(),
        )

    for table, columns in _INDEXES.items():
        existing = {ix["name"] for ix in sa.inspect(bind).get_indexes(table)}
        for column in columns:
            name = f"ix_{table}_{column}"
            if name not in existing:
                op.create_index(name, table, [column])


def downgrade() -> None:
    tables = sa.inspect(op.get_bind()).get_table_names()
    for table in (_FEATURE, _UPGRADE):
        if table in tables:
            op.drop_table(table)
