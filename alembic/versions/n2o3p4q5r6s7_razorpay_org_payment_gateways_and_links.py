"""razorpay part b: org_payment_gateways + invoice_payment_links

Adds database support for organization-specific Razorpay gateways and
invoice payment links.

1. org_payment_gateways — one row per organization storing encrypted Razorpay
   key_secret and webhook_secret, key_id, mode ('test'/'live'), and verified_at.

2. invoice_payment_links — records Razorpay Payment Links generated for invoices,
   tracking status ('created', 'partially_paid', 'paid', 'expired', 'cancelled'),
   short_url, paise amounts, expiration, and audit trail.

Revision ID: n2o3p4q5r6s7
Revises: m1n2o3p4q5r6
Create Date: 2026-10-01 01:00:00.000000

"""
import logging
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'n2o3p4q5r6s7'
down_revision: Union[str, Sequence[str], None] = 'm1n2o3p4q5r6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

logger = logging.getLogger("alembic.runtime.migration")

_GATEWAYS_TABLE = "org_payment_gateways"
_LINKS_TABLE = "invoice_payment_links"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = set(inspector.get_table_names())

    # 1. org_payment_gateways
    if _GATEWAYS_TABLE not in existing_tables:
        logger.info("Creating table %s", _GATEWAYS_TABLE)
        op.create_table(
            _GATEWAYS_TABLE,
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column(
                "organization_id",
                sa.String(36),
                sa.ForeignKey("organizations.id", ondelete="CASCADE"),
                nullable=False,
                unique=True,
            ),
            sa.Column("provider", sa.String(30), nullable=False, server_default="razorpay"),
            sa.Column("key_id", sa.String(100), nullable=False),
            sa.Column("key_secret_encrypted", sa.Text(), nullable=False),
            sa.Column("webhook_secret_encrypted", sa.Text(), nullable=False),
            sa.Column("mode", sa.String(10), nullable=False),
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index(
            f"ix_{_GATEWAYS_TABLE}_organization_id",
            _GATEWAYS_TABLE,
            ["organization_id"],
            unique=True,
        )
    else:
        logger.info("Table %s already exists -- skipping create", _GATEWAYS_TABLE)

    # 2. invoice_payment_links
    if _LINKS_TABLE not in existing_tables:
        logger.info("Creating table %s", _LINKS_TABLE)
        op.create_table(
            _LINKS_TABLE,
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column(
                "organization_id",
                sa.String(36),
                sa.ForeignKey("organizations.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "invoice_id",
                sa.String(36),
                sa.ForeignKey("invoices.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("razorpay_link_id", sa.String(100), nullable=False, unique=True),
            sa.Column("short_url", sa.String(255), nullable=False),
            sa.Column("amount_paise", sa.Integer(), nullable=False),
            sa.Column("amount_paid_paise", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("currency", sa.String(10), nullable=False, server_default="INR"),
            sa.Column("status", sa.String(30), nullable=False, server_default="created"),
            sa.Column("expire_by", sa.DateTime(timezone=True), nullable=True),
            sa.Column("notify_sms", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("notify_email", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column(
                "created_by_user_id",
                sa.String(36),
                sa.ForeignKey("users.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("raw_event", sa.JSON(), nullable=True),
        )
        op.create_index(
            f"ix_{_LINKS_TABLE}_organization_id",
            _LINKS_TABLE,
            ["organization_id"],
        )
        op.create_index(
            f"ix_{_LINKS_TABLE}_invoice_id",
            _LINKS_TABLE,
            ["invoice_id"],
        )
        op.create_index(
            f"ix_{_LINKS_TABLE}_razorpay_link_id",
            _LINKS_TABLE,
            ["razorpay_link_id"],
            unique=True,
        )
        op.create_index(
            f"ix_{_LINKS_TABLE}_status",
            _LINKS_TABLE,
            ["status"],
        )
    else:
        logger.info("Table %s already exists -- skipping create", _LINKS_TABLE)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = set(inspector.get_table_names())

    if _LINKS_TABLE in existing_tables:
        op.drop_table(_LINKS_TABLE)

    if _GATEWAYS_TABLE in existing_tables:
        op.drop_table(_GATEWAYS_TABLE)
