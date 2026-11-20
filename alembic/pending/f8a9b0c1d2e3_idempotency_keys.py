"""idempotency_keys: stored results of POST requests sent with an Idempotency-Key header

NOT LIVE YET. This file sits in alembic/pending/, which Alembic does not scan, so
`alembic upgrade head` (run by the container on every boot) does not apply it. To release
it, after the database backup: move it to alembic/versions/ and check that down_revision
below is still the current head (`alembic heads`); update it if newer revisions landed.

Table idempotency_keys (new, empty; creating it locks nothing existing):
  id               VARCHAR(36)  PK
  organization_id  VARCHAR(36)  FK organizations ON DELETE CASCADE
  user_id          VARCHAR(36)  FK users ON DELETE CASCADE
  endpoint         VARCHAR(200) "METHOD route-template", e.g. "POST /orders"
  idempotency_key  VARCHAR(255) the client's header value
  request_hash     CHAR(64)     sha256 of the canonical JSON body
  state            VARCHAR(20)  'in_progress' | 'completed'
  response_status  INTEGER      null while in_progress
  response_body    TEXT         JSON text of the stored response, null while in_progress
  created_at       TIMESTAMPTZ  default now()
  expires_at       TIMESTAMPTZ  created_at + 24 h; expired rows are ignored and purged
UNIQUE (user_id, endpoint, idempotency_key)  -- "key + user + endpoint"
INDEX  (expires_at)                          -- for the purge job

Downgrade drops the table. Only stored replay results are lost; no business data lives
here, and with the header optional every client keeps working without it.

Revision ID: f8a9b0c1d2e3
Revises: e7f1a2b3c4d5
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "f8a9b0c1d2e3"
down_revision: Union[str, None] = "e7f1a2b3c4d5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = "idempotency_keys"


def _exists() -> bool:
    return sa.inspect(op.get_bind()).has_table(TABLE)


def upgrade() -> None:
    if _exists():  # re-runnable after a partial run
        return
    op.create_table(
        TABLE,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("organization_id", sa.String(36),
                  sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("endpoint", sa.String(200), nullable=False),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("state", sa.String(20), nullable=False, server_default="in_progress"),
        sa.Column("response_status", sa.Integer(), nullable=True),
        sa.Column("response_body", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("user_id", "endpoint", "idempotency_key", name="uq_idempotency_user_endpoint_key"),
        sa.CheckConstraint("state IN ('in_progress', 'completed')", name="ck_idempotency_state"),
    )
    op.create_index("ix_idempotency_keys_expires_at", TABLE, ["expires_at"])


def downgrade() -> None:
    if not _exists():
        return
    op.drop_index("ix_idempotency_keys_expires_at", table_name=TABLE)
    op.drop_table(TABLE)
