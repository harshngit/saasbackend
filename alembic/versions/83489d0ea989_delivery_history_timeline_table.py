"""delivery_history timeline table

Adds the persistent Delivery timeline/history table backing
app.models.delivery.DeliveryHistory / delivery_service.record_history — one
row per meaningful Delivery lifecycle event (created, assigned, reassigned,
accepted, rejected, picking_started, picking_completed, loaded, dispatched,
delivered, partially_delivered, failed, cancelled, ready), written in the
same transaction as the business action it describes.

Entirely new table, so — unlike the FK-hardening migrations elsewhere in
this project — there is no existing data to worry about: SQLite supports
inline FK constraints in CREATE TABLE natively, no batch mode needed.

delivery_id -> deliveries.id is ON DELETE CASCADE: a Delivery's history has
no independent meaning once the Delivery itself is gone. actor_id ->
users.id is ON DELETE SET NULL: the row's own denormalized actor_name (see
the model docstring) keeps the timeline readable even then.

Same inspector-gated, idempotent style as every other migration in this
project: safe against a fresh database (baseline create_all() may already
have created this) and safe to re-run.

Revision ID: 83489d0ea989
Revises: a7b8c9d0e1f2
Create Date: 2026-09-04 17:00:00.000000

"""
import logging
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '83489d0ea989'
down_revision: Union[str, Sequence[str], None] = 'a7b8c9d0e1f2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

logger = logging.getLogger("alembic.runtime.migration")

_TABLE = "delivery_history"
_INDEX = "ix_delivery_history_org_delivery"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if _TABLE in inspector.get_table_names():
        logger.info("%s already exists -- skipping table creation", _TABLE)
    else:
        logger.info("Creating %s", _TABLE)
        op.create_table(
            _TABLE,
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column(
                "organization_id", sa.String(length=36),
                sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False,
            ),
            sa.Column(
                "delivery_id", sa.String(length=36),
                sa.ForeignKey("deliveries.id", ondelete="CASCADE"), nullable=False,
            ),
            sa.Column("event_type", sa.String(length=50), nullable=False),
            sa.Column("previous_status", sa.String(length=30), nullable=True),
            sa.Column("new_status", sa.String(length=30), nullable=True),
            sa.Column(
                "actor_id", sa.String(length=36),
                sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True,
            ),
            sa.Column("actor_name", sa.String(length=150), nullable=True),
            sa.Column("notes", sa.Text(), nullable=True),
            sa.Column("event_metadata", sa.JSON(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )

    inspector = sa.inspect(bind)
    existing_indexes = {ix["name"] for ix in inspector.get_indexes(_TABLE)}

    def _ensure_index(name: str, columns: list[str]) -> None:
        if name not in existing_indexes:
            logger.info("Adding index %s", name)
            op.create_index(name, _TABLE, columns)

    _ensure_index("ix_delivery_history_organization_id", ["organization_id"])
    _ensure_index("ix_delivery_history_delivery_id", ["delivery_id"])
    _ensure_index("ix_delivery_history_event_type", ["event_type"])
    _ensure_index("ix_delivery_history_created_at", ["created_at"])
    _ensure_index(_INDEX, ["organization_id", "delivery_id"])


def downgrade() -> None:
    """Drops the table outright: it is new as of this migration, purely
    additive timeline data with no other table depending on it, and (unlike
    a column added to an existing table) nothing else in this schema
    references it — the same reasoning the baseline revision's downgrade()
    uses for tables it alone created.
    """
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if _TABLE in inspector.get_table_names():
        op.drop_table(_TABLE)
