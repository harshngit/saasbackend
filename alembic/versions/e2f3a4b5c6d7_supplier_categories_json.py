"""supplier categories json column and backfill

Adds categories JSON column to suppliers table for multi-select supplier categories,
and backfills categories from category string if present.

Revision ID: e2f3a4b5c6d7
Revises: b1c2d3e4f5a6
Create Date: 2026-09-07 19:20:00.000000

"""
import logging
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'e2f3a4b5c6d7'
down_revision: Union[str, Sequence[str], None] = 'b1c2d3e4f5a6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

logger = logging.getLogger("alembic.runtime.migration")

_SUPPLIERS_TABLE = "suppliers"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if _SUPPLIERS_TABLE in inspector.get_table_names():
        existing_cols = {c["name"] for c in inspector.get_columns(_SUPPLIERS_TABLE)}
        if "categories" not in existing_cols:
            logger.info("Adding categories JSON column to %s table", _SUPPLIERS_TABLE)
            op.add_column(_SUPPLIERS_TABLE, sa.Column("categories", sa.JSON(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if _SUPPLIERS_TABLE in inspector.get_table_names():
        existing_cols = {c["name"] for c in inspector.get_columns(_SUPPLIERS_TABLE)}
        if "categories" in existing_cols:
            with op.batch_alter_table(_SUPPLIERS_TABLE) as batch_op:
                batch_op.drop_column("categories")
