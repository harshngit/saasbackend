"""voided_customer_payments: keep voided receipts (number and all) instead of losing them

NOT LIVE YET. This file sits in alembic/pending/, which Alembic does not scan, so
`alembic upgrade head` does not apply it. To release it, after the database backup: move it
to alembic/versions/ and set down_revision below to the current head (`alembic heads`). If
f8a9b0c1d2e3_idempotency_keys.py is released too, chain one after the other.

Until it is released the app behaves exactly as before: voiding deletes the payment and the
copy is skipped (payment_service.voided_archive_available() is False). After it is released,
every void copies the payment here first; GET /payment-receipts?include_voided=true lists it
as receipt_status "void". Nothing that computes totals or balances reads this table.

New, empty table; creating it locks nothing existing. Downgrade drops it (only the voided
copies are lost; balances are unaffected).

Revision ID: a9b8c7d6e5f4
Revises: e7f1a2b3c4d5
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a9b8c7d6e5f4"
down_revision: Union[str, None] = "e7f1a2b3c4d5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "voided_customer_payments",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("organization_id", sa.String(36), sa.ForeignKey("organizations.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("customer_id", sa.String(36), nullable=True),
        sa.Column("receipt_number", sa.String(50), nullable=True),
        sa.Column("amount", sa.Float(), nullable=False),
        sa.Column("payment_mode", sa.String(30), nullable=True),
        sa.Column("reference", sa.String(150), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("invoice_id", sa.String(36), nullable=True),
        sa.Column("order_id", sa.String(36), nullable=True),
        sa.Column("received_on", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("voided_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("voided_by_id", sa.String(36), nullable=True),
        sa.Column("void_source", sa.String(30), nullable=True),
        sa.Column("snapshot", sa.JSON(), nullable=True),
    )
    op.create_index("ix_voided_customer_payments_organization_id", "voided_customer_payments", ["organization_id"])
    op.create_index("ix_voided_customer_payments_customer_id", "voided_customer_payments", ["customer_id"])
    op.create_index("ix_voided_customer_payments_receipt_number", "voided_customer_payments", ["receipt_number"])


def downgrade() -> None:
    op.drop_table("voided_customer_payments")
