"""push_devices: FCM registration tokens for web and Android push notifications

One row per FCM registration token (unique), owned by a user and that user's organization.
No credentials are stored here -- the Firebase service-account key is a runtime secret.

Revision ID: b6c7d8e9f0a1
Revises: a5b6c7d8e9f0
Create Date: 2026-10-22 12:00:00.000000

"""
import logging
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b6c7d8e9f0a1"
down_revision: Union[str, Sequence[str], None] = "a5b6c7d8e9f0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

logger = logging.getLogger("alembic.runtime.migration")

_TABLE = "push_devices"


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if _TABLE in inspector.get_table_names():
        logger.info("%s already exists -- skipping table creation", _TABLE)
    else:
        op.create_table(
            _TABLE,
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("user_id", sa.String(length=36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
            sa.Column(
                "organization_id", sa.String(length=36),
                sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=True,
            ),
            sa.Column("token", sa.String(length=1024), nullable=False),
            sa.Column("platform", sa.String(length=10), nullable=False),
            sa.Column("label", sa.String(length=100), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("token", name="uq_push_devices_token"),
        )

    existing = {ix["name"] for ix in sa.inspect(op.get_bind()).get_indexes(_TABLE)}
    for name, column in (
        ("ix_push_devices_user_id", "user_id"),
        ("ix_push_devices_organization_id", "organization_id"),
    ):
        if name not in existing:
            op.create_index(name, _TABLE, [column])


def downgrade() -> None:
    if _TABLE in sa.inspect(op.get_bind()).get_table_names():
        op.drop_table(_TABLE)
