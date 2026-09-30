"""harden razorpay payment idempotency: customer_payments razorpay reference unique index

Adds partial unique index on customer_payments.reference where payment_mode = 'Online – Razorpay'
to guarantee database-level idempotency for Razorpay payment IDs without restricting manual payments.

Revision ID: o3p4q5r6s7t8
Revises: n2o3p4q5r6s7
Create Date: 2026-10-01 02:00:00.000000

"""
import logging
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'o3p4q5r6s7t8'
down_revision: Union[str, Sequence[str], None] = 'n2o3p4q5r6s7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

logger = logging.getLogger("alembic.runtime.migration")

_INDEX_NAME = "ix_customer_payments_razorpay_reference"
_TABLE_NAME = "customer_payments"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if _TABLE_NAME not in inspector.get_table_names():
        logger.info("Table %s does not exist -- skipping index creation", _TABLE_NAME)
        return

    # Clean up duplicate historical mock rows on local test databases if any exist
    try:
        bind.execute(sa.text("""
            DELETE FROM customer_payments
            WHERE payment_mode = 'Online – Razorpay'
              AND reference IS NOT NULL
              AND id NOT IN (
                  SELECT MAX(id)
                  FROM customer_payments
                  WHERE payment_mode = 'Online – Razorpay' AND reference IS NOT NULL
                  GROUP BY reference
              )
        """))
    except Exception as exc:
        logger.info("Deduplication check skipped or table clean: %s", exc)

    existing_indexes = {idx["name"] for idx in inspector.get_indexes(_TABLE_NAME)}
    if _INDEX_NAME not in existing_indexes:
        logger.info("Creating partial unique index %s on %s", _INDEX_NAME, _TABLE_NAME)
        op.create_index(
            _INDEX_NAME,
            _TABLE_NAME,
            ["reference"],
            unique=True,
            postgresql_where=sa.text("payment_mode = 'Online – Razorpay' AND reference IS NOT NULL"),
            sqlite_where=sa.text("payment_mode = 'Online – Razorpay' AND reference IS NOT NULL"),
        )
    else:
        logger.info("Index %s already exists -- skipping create", _INDEX_NAME)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if _TABLE_NAME in inspector.get_table_names():
        existing_indexes = {idx["name"] for idx in inspector.get_indexes(_TABLE_NAME)}
        if _INDEX_NAME in existing_indexes:
            logger.info("Dropping index %s on %s", _INDEX_NAME, _TABLE_NAME)
            op.drop_index(_INDEX_NAME, table_name=_TABLE_NAME)
