"""notifications: dedupe_key + unique (user_id, dedupe_key)

Adds one nullable column to the existing `notifications` table -- nothing is dropped or
rewritten -- and a unique index so a retried request, a redelivered webhook or overlapping
reminder sweeps can never store the same state-transition notification twice for a user:
  ux_notifications_user_id_dedupe_key   (user_id, dedupe_key) UNIQUE

Existing rows get NULL, and NULLs never collide in a unique index (PostgreSQL and SQLite),
so the index builds on any existing data.

Every step is guarded, so it is safe on a database whose app startup already added the
column (auto_add_missing_columns) or created the index (create_all on a fresh database).

Revision ID: e1f2a3b4c5d6
Revises: d9e0f1a2b3c4
Create Date: 2026-11-02 12:00:00.000000

"""
import logging
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e1f2a3b4c5d6"
down_revision: Union[str, Sequence[str], None] = "d9e0f1a2b3c4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

logger = logging.getLogger("alembic.runtime.migration")

_TABLE = "notifications"
_INDEX = "ux_notifications_user_id_dedupe_key"


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if _TABLE not in inspector.get_table_names():
        return
    columns = {c["name"] for c in inspector.get_columns(_TABLE)}
    if "dedupe_key" not in columns:
        op.add_column(_TABLE, sa.Column("dedupe_key", sa.String(200), nullable=True))
        logger.info("Added %s.dedupe_key", _TABLE)

    existing = {ix["name"] for ix in sa.inspect(op.get_bind()).get_indexes(_TABLE)}
    if _INDEX not in existing:
        op.create_index(_INDEX, _TABLE, ["user_id", "dedupe_key"], unique=True)
        logger.info("Created index %s", _INDEX)


def downgrade() -> None:
    # Drops only the index. The column stays (alembic/README rule 5): app startup may
    # already have added it, and it may hold data by then.
    inspector = sa.inspect(op.get_bind())
    if _TABLE not in inspector.get_table_names():
        return
    if _INDEX in {ix["name"] for ix in inspector.get_indexes(_TABLE)}:
        op.drop_index(_INDEX, table_name=_TABLE)
