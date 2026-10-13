"""create accounts, journal_entries, and journal_lines tables

Revision ID: z4a5b6c7d8e9
Revises: y3z4a5b6c7d8
Create Date: 2026-10-13 14:00:00.000000

"""
import logging
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "z4a5b6c7d8e9"
down_revision: Union[str, Sequence[str], None] = "y3z4a5b6c7d8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

logger = logging.getLogger("alembic.runtime.migration")


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = inspector.get_table_names()

    if "accounts" not in tables:
        op.create_table(
            "accounts",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column(
                "organization_id",
                sa.String(length=36),
                sa.ForeignKey("organizations.id", ondelete="CASCADE"),
                nullable=False,
                index=True,
            ),
            sa.Column("account_code", sa.String(length=32), nullable=False, index=True),
            sa.Column("name", sa.String(length=150), nullable=False, index=True),
            sa.Column("account_type", sa.String(length=32), nullable=False, index=True),
            sa.Column("classification", sa.String(length=50), nullable=True, index=True),
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
            sa.Column("is_system", sa.Boolean(), nullable=False, server_default=sa.text("false")),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("organization_id", "account_code", name="uq_accounts_org_code"),
        )
        logger.info("Created accounts table")

    if "journal_entries" not in tables:
        op.create_table(
            "journal_entries",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column(
                "organization_id",
                sa.String(length=36),
                sa.ForeignKey("organizations.id", ondelete="CASCADE"),
                nullable=False,
                index=True,
            ),
            sa.Column("entry_number", sa.String(length=50), nullable=False, index=True),
            sa.Column("entry_type", sa.String(length=32), nullable=False, server_default="manual", index=True),
            sa.Column("effective_date", sa.DateTime(timezone=True), nullable=False, index=True),
            sa.Column("reference", sa.String(length=100), nullable=True, index=True),
            sa.Column("source_type", sa.String(length=50), nullable=True, index=True),
            sa.Column("source_id", sa.String(length=36), nullable=True, index=True),
            sa.Column("status", sa.String(length=20), nullable=False, server_default="posted", index=True),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("is_reversal", sa.Boolean(), nullable=False, server_default=sa.text("false")),
            sa.Column(
                "reversed_entry_id",
                sa.String(length=36),
                sa.ForeignKey("journal_entries.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column(
                "created_by_user_id",
                sa.String(length=36),
                sa.ForeignKey("users.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("organization_id", "entry_number", name="uq_journal_entries_org_number"),
            sa.UniqueConstraint("organization_id", "source_type", "source_id", name="uq_journal_entries_org_source"),
        )
        logger.info("Created journal_entries table")

    if "journal_lines" not in tables:
        op.create_table(
            "journal_lines",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column(
                "organization_id",
                sa.String(length=36),
                sa.ForeignKey("organizations.id", ondelete="CASCADE"),
                nullable=False,
                index=True,
            ),
            sa.Column(
                "journal_entry_id",
                sa.String(length=36),
                sa.ForeignKey("journal_entries.id", ondelete="CASCADE"),
                nullable=False,
                index=True,
            ),
            sa.Column(
                "account_id",
                sa.String(length=36),
                sa.ForeignKey("accounts.id", ondelete="RESTRICT"),
                nullable=False,
                index=True,
            ),
            sa.Column("line_number", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("debit", sa.Numeric(precision=15, scale=2), nullable=False, server_default="0.00"),
            sa.Column("credit", sa.Numeric(precision=15, scale=2), nullable=False, server_default="0.00"),
            sa.Column("description", sa.String(length=255), nullable=True),
        )
        logger.info("Created journal_lines table")


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = inspector.get_table_names()

    if "journal_lines" in tables:
        op.drop_table("journal_lines")
        logger.info("Dropped journal_lines table")

    if "journal_entries" in tables:
        op.drop_table("journal_entries")
        logger.info("Dropped journal_entries table")

    if "accounts" in tables:
        op.drop_table("accounts")
        logger.info("Dropped accounts table")
