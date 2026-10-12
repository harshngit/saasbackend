"""create cash_reconciliations and cash_reconciliation_details tables

Revision ID: x2y3z4a5b6c7
Revises: w1x2y3z4a5b6
Create Date: 2026-10-12 12:00:00.000000

"""
import logging
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "x2y3z4a5b6c7"
down_revision: Union[str, Sequence[str], None] = "w1x2y3z4a5b6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

logger = logging.getLogger("alembic.runtime.migration")


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = inspector.get_table_names()

    if "cash_reconciliations" not in tables:
        op.create_table(
            "cash_reconciliations",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column(
                "organization_id",
                sa.String(length=36),
                sa.ForeignKey("organizations.id", ondelete="CASCADE"),
                nullable=False,
                index=True,
            ),
            sa.Column("reconciliation_date", sa.Date(), nullable=False, index=True),
            sa.Column("status", sa.String(length=20), nullable=False, server_default="draft", index=True),
            sa.Column("total_expected_collections", sa.Numeric(precision=12, scale=2), nullable=False, server_default="0.00"),
            sa.Column("total_actual_collections", sa.Numeric(precision=12, scale=2), nullable=False, server_default="0.00"),
            sa.Column("collections_variance", sa.Numeric(precision=12, scale=2), nullable=False, server_default="0.00"),
            sa.Column("opening_balance", sa.Numeric(precision=12, scale=2), nullable=False, server_default="0.00"),
            sa.Column("cash_sales", sa.Numeric(precision=12, scale=2), nullable=False, server_default="0.00"),
            sa.Column("cash_received", sa.Numeric(precision=12, scale=2), nullable=False, server_default="0.00"),
            sa.Column("cash_paid", sa.Numeric(precision=12, scale=2), nullable=False, server_default="0.00"),
            sa.Column("expenses_paid", sa.Numeric(precision=12, scale=2), nullable=False, server_default="0.00"),
            sa.Column("expected_closing_cash", sa.Numeric(precision=12, scale=2), nullable=False, server_default="0.00"),
            sa.Column("actual_cash_on_hand", sa.Numeric(precision=12, scale=2), nullable=False, server_default="0.00"),
            sa.Column("cash_balance_variance", sa.Numeric(precision=12, scale=2), nullable=False, server_default="0.00"),
            sa.Column("variance_reason", sa.Text(), nullable=True),
            sa.Column("notes", sa.Text(), nullable=True),
            sa.Column(
                "created_by_id",
                sa.String(length=36),
                sa.ForeignKey("users.id", ondelete="SET NULL"),
                nullable=True,
                index=True,
            ),
            sa.Column(
                "closed_by_id",
                sa.String(length=36),
                sa.ForeignKey("users.id", ondelete="SET NULL"),
                nullable=True,
                index=True,
            ),
            sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("organization_id", "reconciliation_date", name="uq_cash_reconciliation_org_date"),
        )
        op.create_index(
            "ix_cash_reconciliation_org_date",
            "cash_reconciliations",
            ["organization_id", "reconciliation_date"],
        )
        op.create_index(
            "ix_cash_reconciliation_org_status_date",
            "cash_reconciliations",
            ["organization_id", "status", "reconciliation_date"],
        )
        logger.info("Created cash_reconciliations table")

    if "cash_reconciliation_details" not in tables:
        op.create_table(
            "cash_reconciliation_details",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column(
                "reconciliation_id",
                sa.String(length=36),
                sa.ForeignKey("cash_reconciliations.id", ondelete="CASCADE"),
                nullable=False,
                index=True,
            ),
            sa.Column(
                "organization_id",
                sa.String(length=36),
                sa.ForeignKey("organizations.id", ondelete="CASCADE"),
                nullable=False,
                index=True,
            ),
            sa.Column("payment_method", sa.String(length=30), nullable=False),
            sa.Column("expected_amount", sa.Numeric(precision=12, scale=2), nullable=False, server_default="0.00"),
            sa.Column("actual_amount", sa.Numeric(precision=12, scale=2), nullable=False, server_default="0.00"),
            sa.Column("variance_amount", sa.Numeric(precision=12, scale=2), nullable=False, server_default="0.00"),
            sa.Column("notes", sa.String(length=255), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("reconciliation_id", "payment_method", name="uq_cash_reconciliation_detail_method"),
        )
        op.create_index(
            "ix_cash_reconciliation_detail_org_method",
            "cash_reconciliation_details",
            ["organization_id", "payment_method"],
        )
        logger.info("Created cash_reconciliation_details table")


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = inspector.get_table_names()

    if "cash_reconciliation_details" in tables:
        op.drop_table("cash_reconciliation_details")
        logger.info("Dropped cash_reconciliation_details table")

    if "cash_reconciliations" in tables:
        op.drop_table("cash_reconciliations")
        logger.info("Dropped cash_reconciliations table")
