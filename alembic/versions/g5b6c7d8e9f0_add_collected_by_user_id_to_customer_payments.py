"""add collected_by_user_id to customer_payments

Revision ID: g5b6c7d8e9f0
Revises: f4a5b6c7d8e9
Create Date: 2026-09-10 23:20:00.000000

"""
import logging
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'g5b6c7d8e9f0'
down_revision: Union[str, Sequence[str], None] = 'f4a5b6c7d8e9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

logger = logging.getLogger("alembic.runtime.migration")

_PAYMENT_TABLE = "customer_payments"
_COLL_TABLE = "delivery_collections"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if _PAYMENT_TABLE in inspector.get_table_names():
        columns = [c["name"] for c in inspector.get_columns(_PAYMENT_TABLE)]
        if "collected_by_user_id" not in columns:
            logger.info("Adding collected_by_user_id column to %s", _PAYMENT_TABLE)
            with op.batch_alter_table(_PAYMENT_TABLE) as batch_op:
                batch_op.add_column(
                    sa.Column("collected_by_user_id", sa.String(length=36), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
                )

            # Safely backfill historical rows from linked delivery collections where collector is known
            if _COLL_TABLE in inspector.get_table_names():
                logger.info("Backfilling collected_by_user_id from %s where applicable", _COLL_TABLE)
                if bind.dialect.name == "sqlite":
                    op.execute(
                        f"""
                        UPDATE {_PAYMENT_TABLE}
                        SET collected_by_user_id = (
                            SELECT delivery_partner_id
                            FROM {_COLL_TABLE}
                            WHERE {_COLL_TABLE}.customer_payment_id = {_PAYMENT_TABLE}.id
                              AND {_COLL_TABLE}.delivery_partner_id IS NOT NULL
                            LIMIT 1
                        )
                        WHERE EXISTS (
                            SELECT 1 FROM {_COLL_TABLE}
                            WHERE {_COLL_TABLE}.customer_payment_id = {_PAYMENT_TABLE}.id
                              AND {_COLL_TABLE}.delivery_partner_id IS NOT NULL
                        )
                        """
                    )
                else:
                    op.execute(
                        f"""
                        UPDATE {_PAYMENT_TABLE}
                        SET collected_by_user_id = dc.delivery_partner_id
                        FROM {_COLL_TABLE} dc
                        WHERE dc.customer_payment_id = {_PAYMENT_TABLE}.id
                          AND dc.delivery_partner_id IS NOT NULL
                        """
                    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if _PAYMENT_TABLE in inspector.get_table_names():
        columns = [c["name"] for c in inspector.get_columns(_PAYMENT_TABLE)]
        if "collected_by_user_id" in columns:
            logger.info("Removing collected_by_user_id column from %s", _PAYMENT_TABLE)
            with op.batch_alter_table(_PAYMENT_TABLE) as batch_op:
                batch_op.drop_column("collected_by_user_id")
