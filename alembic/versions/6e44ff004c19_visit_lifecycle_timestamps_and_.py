"""visits lifecycle timestamps and cancellation_reason

Adds five nullable columns to visits, all backend-managed lifecycle
metadata for the new planned -> in_progress -> completed (or -> cancelled)
Visit lifecycle (see app.core.workflow.VISIT_TRANSITIONS and
visit_service.update_visit):

  - checked_in_at        set when status first moves to "in_progress"
  - checked_out_at       set when status first moves to "completed"
  - completed_at         set when status first moves to "completed"
  - cancelled_at         set when status first moves to "cancelled"
  - cancellation_reason  optional free text, client-supplied

No FK/index is needed -- these are plain scalar columns, not relationships.
None of the existing statuses ("planned", "completed", "cancelled") change
meaning or value; "in_progress" is simply a new allowed value alongside
them, and existing rows are unaffected (all five new columns default to
NULL, which is exactly the "not yet reached that lifecycle stage" state for
every already-existing Visit row).

Same inspector-gated, idempotent style as every other migration in this
project (see c71ef1545813_follow_ups_lead_id_and_visits_lead_id_.py): safe
against a fresh database (baseline create_all() may already have these),
an existing database missing some or all of the columns, and safe to
re-run.

Revision ID: 6e44ff004c19
Revises: c71ef1545813
Create Date: 2026-09-04 00:00:00.000000

"""
import logging
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '6e44ff004c19'
down_revision: Union[str, Sequence[str], None] = 'c71ef1545813'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

logger = logging.getLogger("alembic.runtime.migration")

_TABLE = "visits"

_NEW_COLUMNS = [
    sa.Column("checked_in_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("checked_out_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("cancellation_reason", sa.Text(), nullable=True),
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
    """Deliberately a no-op: by the time anyone downgrades, these columns
    may already hold real check-in/check-out/completion/cancellation data
    for live Visits, and dropping them would be silent, unrecoverable data
    loss -- the same reasoning the baseline revision and
    39c2af6d9f58 (leads.lead_type / leads.segment) document for not
    dropping columns.
    """
