"""razorpay phase 1: subscription_payments + organizations.plan_expires_at

Adds the database support for online (Razorpay, TEST MODE) SaaS plan payments,
additive to — not a replacement for — the existing manual Super Admin
upgrade-approval flow (POST /organizations/upgrade-request +
PATCH /superadmin/organizations/{id}/approve-upgrade), which keeps working
unchanged.

1. organizations.plan_expires_at — when the current *paid* plan period ends.
   Nullable; None for every existing organization and for any org that has
   never had a paid plan activated online.

2. subscription_payments — one row per Razorpay payment attempt. Created as
   status="created" when POST /billing/razorpay/order makes the Razorpay
   order, then moved to "paid" (by app.services.billing_service.
   activate_subscription, shared by the verify endpoint and the webhook) or
   "failed" (by the webhook's payment.failed handler).

Revision ID: m1n2o3p4q5r6
Revises: l0a1b2c3d4e5
Create Date: 2026-09-30 18:00:00.000000

"""
import logging
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'm1n2o3p4q5r6'
down_revision: Union[str, Sequence[str], None] = 'l0a1b2c3d4e5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

logger = logging.getLogger("alembic.runtime.migration")

_ORG_TABLE = "organizations"
_ORG_COLUMN = "plan_expires_at"
_PAYMENTS_TABLE = "subscription_payments"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    # 1. organizations.plan_expires_at
    if _ORG_TABLE in inspector.get_table_names():
        existing_org_columns = {c["name"] for c in inspector.get_columns(_ORG_TABLE)}
        if _ORG_COLUMN not in existing_org_columns:
            logger.info("Adding %s.%s", _ORG_TABLE, _ORG_COLUMN)
            with op.batch_alter_table(_ORG_TABLE) as batch_op:
                batch_op.add_column(
                    sa.Column(_ORG_COLUMN, sa.DateTime(timezone=True), nullable=True)
                )
        else:
            logger.info("%s.%s already exists -- skipping", _ORG_TABLE, _ORG_COLUMN)
    else:
        logger.info("%s table does not exist -- skipping plan_expires_at add", _ORG_TABLE)

    # 2. subscription_payments
    if _PAYMENTS_TABLE in inspector.get_table_names():
        logger.info("%s already exists -- skipping creation", _PAYMENTS_TABLE)
        return

    logger.info("Creating %s", _PAYMENTS_TABLE)
    op.create_table(
        _PAYMENTS_TABLE,
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "organization_id", sa.String(length=36),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("plan_id", sa.String(length=36), sa.ForeignKey("plans.id"), nullable=False),
        sa.Column("billing_cycle", sa.String(length=10), nullable=False),
        sa.Column("amount_paise", sa.Integer(), nullable=False),
        sa.Column("currency", sa.String(length=10), nullable=False, server_default="INR"),
        sa.Column("razorpay_order_id", sa.String(length=64), nullable=False),
        sa.Column("razorpay_payment_id", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="created"),
        sa.Column("failure_reason", sa.String(length=500), nullable=True),
        sa.Column("created_by_user_id", sa.String(length=36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("raw_event", sa.JSON(), nullable=True),
        sa.UniqueConstraint("razorpay_order_id", name="uq_subscription_payments_razorpay_order_id"),
        sa.UniqueConstraint("razorpay_payment_id", name="uq_subscription_payments_razorpay_payment_id"),
    )

    existing_indexes = {ix["name"] for ix in sa.inspect(bind).get_indexes(_PAYMENTS_TABLE)}
    if "ix_subscription_payments_organization_id" not in existing_indexes:
        op.create_index(
            "ix_subscription_payments_organization_id", _PAYMENTS_TABLE, ["organization_id"],
        )
    if "ix_subscription_payments_razorpay_order_id" not in existing_indexes:
        op.create_index(
            "ix_subscription_payments_razorpay_order_id", _PAYMENTS_TABLE, ["razorpay_order_id"], unique=True,
        )
    if "ix_subscription_payments_razorpay_payment_id" not in existing_indexes:
        op.create_index(
            "ix_subscription_payments_razorpay_payment_id", _PAYMENTS_TABLE, ["razorpay_payment_id"], unique=True,
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if _PAYMENTS_TABLE in inspector.get_table_names():
        op.drop_table(_PAYMENTS_TABLE)

    if _ORG_TABLE in inspector.get_table_names():
        existing_org_columns = {c["name"] for c in sa.inspect(bind).get_columns(_ORG_TABLE)}
        if _ORG_COLUMN in existing_org_columns:
            with op.batch_alter_table(_ORG_TABLE) as batch_op:
                batch_op.drop_column(_ORG_COLUMN)
