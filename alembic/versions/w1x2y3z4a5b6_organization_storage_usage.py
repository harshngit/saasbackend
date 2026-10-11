"""create organization_storage_usage table

Revision ID: w1x2y3z4a5b6
Revises: v0w1x2y3z4a5
Create Date: 2026-10-11 12:30:00.000000

"""
import logging
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "w1x2y3z4a5b6"
down_revision: Union[str, Sequence[str], None] = "v0w1x2y3z4a5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

logger = logging.getLogger("alembic.runtime.migration")


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = inspector.get_table_names()

    if "organization_storage_usage" not in tables:
        op.create_table(
            "organization_storage_usage",
            sa.Column("organization_id", sa.String(length=36), sa.ForeignKey("organizations.id", ondelete="CASCADE"), primary_key=True),
            sa.Column("files_documents_bytes", sa.BigInteger(), nullable=False, server_default="0"),
            sa.Column("stored_reports_bytes", sa.BigInteger(), nullable=False, server_default="0"),
            sa.Column("application_data_bytes", sa.BigInteger(), nullable=False, server_default="0"),
            sa.Column("total_used_bytes", sa.BigInteger(), nullable=False, server_default="0"),
            sa.Column("calculated_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )
        logger.info("Created organization_storage_usage table")


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = inspector.get_table_names()

    if "organization_storage_usage" in tables:
        op.drop_table("organization_storage_usage")
        logger.info("Dropped organization_storage_usage table")
