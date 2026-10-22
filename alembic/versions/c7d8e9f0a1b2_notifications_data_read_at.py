"""notifications: data (JSON), read_at, and composite indexes for the in-app feed

Adds two nullable columns to the existing `notifications` table -- nothing is dropped or
rewritten -- and three composite indexes for the per-user feed and unread queries:
  ix_notifications_user_id_created_at       (user_id, created_at)
  ix_notifications_user_id_is_read          (user_id, is_read)
  ix_notifications_organization_id_created_at (organization_id, created_at)

No foreign key is added on organization_id: existing production rows could not be checked,
and a constraint that fails on legacy data would block the release.

Every step is guarded, so it is safe on a database whose app startup already added the
columns (auto_add_missing_columns) or created the indexes (create_all on a fresh database).

Revision ID: c7d8e9f0a1b2
Revises: b6c7d8e9f0a1
Create Date: 2026-10-23 12:00:00.000000

"""
import logging
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c7d8e9f0a1b2"
down_revision: Union[str, Sequence[str], None] = "b6c7d8e9f0a1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

logger = logging.getLogger("alembic.runtime.migration")

_TABLE = "notifications"
_INDEXES = (
    ("ix_notifications_user_id_created_at", ["user_id", "created_at"]),
    ("ix_notifications_user_id_is_read", ["user_id", "is_read"]),
    ("ix_notifications_organization_id_created_at", ["organization_id", "created_at"]),
)


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if _TABLE not in inspector.get_table_names():
        return
    columns = {c["name"] for c in inspector.get_columns(_TABLE)}
    if "data" not in columns:
        op.add_column(_TABLE, sa.Column("data", sa.JSON(), nullable=True))
        logger.info("Added %s.data", _TABLE)
    if "read_at" not in columns:
        op.add_column(_TABLE, sa.Column("read_at", sa.DateTime(timezone=True), nullable=True))
        logger.info("Added %s.read_at", _TABLE)

    existing = {ix["name"] for ix in sa.inspect(op.get_bind()).get_indexes(_TABLE)}
    for name, cols in _INDEXES:
        if name not in existing:
            op.create_index(name, _TABLE, cols)
            logger.info("Created index %s", name)


def downgrade() -> None:
    # Drops only the indexes this revision adds. The columns stay (alembic/README rule 5):
    # app startup may already have added them, and they may hold data by then.
    inspector = sa.inspect(op.get_bind())
    if _TABLE not in inspector.get_table_names():
        return
    existing = {ix["name"] for ix in inspector.get_indexes(_TABLE)}
    for name, _cols in _INDEXES:
        if name in existing:
            op.drop_index(name, table_name=_TABLE)
