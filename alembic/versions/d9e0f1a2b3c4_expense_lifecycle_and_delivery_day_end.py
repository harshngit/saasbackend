"""expense approval/reimbursement audit, record-linked activity history, delivery Day-End

- expenses: approved_at, approval_note, rejected_by, rejected_at, reimbursed_by,
  reimbursed_at, reimbursement_method, reimbursement_reference, reimbursement_notes
  (all nullable; existing rows keep status/approved_by/reject_reason as they are).
- activity_logs: entity_type, entity_id, action (nullable) + ix_activity_logs_org_entity,
  so a record's own lifecycle history is read from the existing activity feed.
- delivery_day_ends: the delivery partner's per-date settlement (unique per
  organization + partner + business_date).

Additive only. Downgrade drops exactly what upgrade adds.

Revision ID: d9e0f1a2b3c4
Revises: c8d9e0f1a2b3
Create Date: 2026-10-30 18:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d9e0f1a2b3c4"
down_revision: Union[str, Sequence[str], None] = "c8d9e0f1a2b3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_EXPENSE_COLUMNS = [
    sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("approval_note", sa.String(500), nullable=True),
    sa.Column("rejected_by", sa.String(36), nullable=True),
    sa.Column("rejected_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("reimbursed_by", sa.String(36), nullable=True),
    sa.Column("reimbursed_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("reimbursement_method", sa.String(30), nullable=True),
    sa.Column("reimbursement_reference", sa.String(150), nullable=True),
    sa.Column("reimbursement_notes", sa.String(500), nullable=True),
]
_ACTIVITY_COLUMNS = [
    sa.Column("entity_type", sa.String(50), nullable=True),
    sa.Column("entity_id", sa.String(36), nullable=True),
    sa.Column("action", sa.String(50), nullable=True),
]


def _existing_columns(table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table)}


def _money(name: str) -> sa.Column:
    return sa.Column(name, sa.Numeric(12, 2), nullable=False, server_default="0")


def upgrade() -> None:
    # The app's startup helper may already have added these columns on a running
    # database; add only what is missing.
    for table, columns in (("expenses", _EXPENSE_COLUMNS), ("activity_logs", _ACTIVITY_COLUMNS)):
        have = _existing_columns(table)
        with op.batch_alter_table(table) as batch:
            for column in columns:
                if column.name not in have:
                    batch.add_column(column.copy())

    inspector = sa.inspect(op.get_bind())
    if "ix_activity_logs_org_entity" not in {i["name"] for i in inspector.get_indexes("activity_logs")}:
        op.create_index("ix_activity_logs_org_entity", "activity_logs", ["organization_id", "entity_type", "entity_id"])

    if "delivery_day_ends" not in inspector.get_table_names():
        op.create_table(
            "delivery_day_ends",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("organization_id", sa.String(36), sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
            sa.Column("delivery_partner_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
            sa.Column("business_date", sa.Date(), nullable=False),
            sa.Column("status", sa.String(20), nullable=False, server_default="open"),
            *[_money(f"expected_{m}") for m in ("cash", "bank", "upi", "cheque", "other", "total")],
            sa.Column("collection_ids", sa.JSON(), nullable=True),
            *[_money(f"handed_{m}") for m in ("cash", "bank", "upi", "cheque", "other", "total")],
            _money("variance_total"),
            sa.Column("notes", sa.Text(), nullable=True),
            sa.Column("submission_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("submitted_by_id", sa.String(36), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
            sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("reviewed_by_id", sa.String(36), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
            sa.Column("review_note", sa.Text(), nullable=True),
            sa.Column("rejection_reason", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("organization_id", "delivery_partner_id", "business_date", name="uq_day_end_org_partner_date"),
        )
        op.create_index("ix_delivery_day_ends_organization_id", "delivery_day_ends", ["organization_id"])
        op.create_index("ix_delivery_day_ends_delivery_partner_id", "delivery_day_ends", ["delivery_partner_id"])
        op.create_index("ix_delivery_day_ends_business_date", "delivery_day_ends", ["business_date"])
        op.create_index("ix_delivery_day_ends_status", "delivery_day_ends", ["status"])
        op.create_index("ix_day_end_org_status_date", "delivery_day_ends", ["organization_id", "status", "business_date"])


def downgrade() -> None:
    op.drop_table("delivery_day_ends")
    op.drop_index("ix_activity_logs_org_entity", table_name="activity_logs")
    with op.batch_alter_table("activity_logs") as batch:
        for column in _ACTIVITY_COLUMNS:
            batch.drop_column(column.name)
    with op.batch_alter_table("expenses") as batch:
        for column in _EXPENSE_COLUMNS:
            batch.drop_column(column.name)
