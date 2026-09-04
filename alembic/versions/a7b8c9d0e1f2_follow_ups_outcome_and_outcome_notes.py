"""follow_ups.outcome and follow_ups.outcome_notes columns

Adds two nullable columns to follow_ups table:
  - outcome        optional outcome (String 255), e.g. "interested", "follow_up_required"
  - outcome_notes  optional completion notes (Text)

Existing follow-up rows remain valid (both columns default to NULL).
Idempotent, inspector-gated style matching other project migrations.

Revision ID: a7b8c9d0e1f2
Revises: 6e44ff004c19
Create Date: 2026-09-04 16:25:00.000000

"""
import logging
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'a7b8c9d0e1f2'
down_revision: Union[str, Sequence[str], None] = '6e44ff004c19'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

logger = logging.getLogger("alembic.runtime.migration")

_TABLE = "follow_ups"

_NEW_COLUMNS = [
    sa.Column("outcome", sa.String(length=255), nullable=True),
    sa.Column("outcome_notes", sa.Text(), nullable=True),
]


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if _TABLE not in inspector.get_table_names():
        logger.info("%s table does not exist yet -- skipping", _TABLE)
        return

    existing_columns = {c["name"] for c in inspector.get_columns(_TABLE)}
    for column in _NEW_COLUMNS:
        if column.name in existing_columns:
            logger.info("%s.%s already exists -- skipping", _TABLE, column.name)
            continue
        logger.info("Adding %s.%s", _TABLE, column.name)
        op.add_column(_TABLE, column.copy())


def downgrade() -> None:
    """Deliberately a no-op to prevent unrecoverable data loss."""
    pass
