"""add payment_proof_url to delivery_collections

Revision ID: t8u9v0w1x2y3
Revises: s7t8u9v0w1x2
Create Date: 2026-10-09 12:00:00.000000

"""
import logging
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "t8u9v0w1x2y3"
down_revision: Union[str, Sequence[str], None] = "s7t8u9v0w1x2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

logger = logging.getLogger("alembic.runtime.migration")


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = inspector.get_table_names()

    if "delivery_collections" in tables:
        columns = [c["name"] for c in inspector.get_columns("delivery_collections")]
        if "payment_proof_url" not in columns:
            op.add_column(
                "delivery_collections",
                sa.Column("payment_proof_url", sa.String(length=500), nullable=True),
            )
            logger.info("Added payment_proof_url column to delivery_collections")


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = inspector.get_table_names()

    if "delivery_collections" in tables:
        columns = [c["name"] for c in inspector.get_columns("delivery_collections")]
        if "payment_proof_url" in columns:
            op.drop_column("delivery_collections", "payment_proof_url")
            logger.info("Dropped payment_proof_url column from delivery_collections")
