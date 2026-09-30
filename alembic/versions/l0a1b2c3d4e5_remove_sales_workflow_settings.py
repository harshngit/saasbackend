"""remove sales_workflow_settings from organizations

Drops the configurable sales_workflow_settings JSON column from organizations.
Sales workflow behavior (Draft order creation, stock reservation on confirmation,
shortage rejection, partial delivery, delivery collections, and non-blocking credit
warnings) is now standardized as canonical ERP logic rather than per-org settings.

Revision ID: l0a1b2c3d4e5
Revises: k9f0a1b2c3d4
Create Date: 2026-09-30 15:00:00.000000

"""
import logging
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'l0a1b2c3d4e5'
down_revision: Union[str, Sequence[str], None] = 'k9f0a1b2c3d4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

logger = logging.getLogger("alembic.runtime.migration")

_TABLE = "organizations"
_COLUMN = "sales_workflow_settings"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if _TABLE not in inspector.get_table_names():
        logger.info("%s table does not exist -- skipping", _TABLE)
        return

    existing_columns = {c["name"] for c in inspector.get_columns(_TABLE)}
    if _COLUMN not in existing_columns:
        logger.info("%s.%s column does not exist -- skipping drop", _TABLE, _COLUMN)
        return

    logger.info("Dropping %s from %s", _COLUMN, _TABLE)
    with op.batch_alter_table(_TABLE) as batch_op:
        batch_op.drop_column(_COLUMN)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if _TABLE not in inspector.get_table_names():
        return

    existing_columns = {c["name"] for c in inspector.get_columns(_TABLE)}
    if _COLUMN in existing_columns:
        return

    logger.info("Restoring %s on %s", _COLUMN, _TABLE)
    with op.batch_alter_table(_TABLE) as batch_op:
        batch_op.add_column(
            sa.Column(_COLUMN, sa.JSON(), nullable=True, server_default="{}")
        )
