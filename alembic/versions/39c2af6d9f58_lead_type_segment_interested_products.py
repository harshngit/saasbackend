"""leads.lead_type, leads.segment, and the lead_interested_products table

Adds the new Lead ↔ Product multi-select functionality:
  - leads.lead_type   (nullable varchar — frontend-defined categorization,
                        not a fixed backend taxonomy)
  - leads.segment     (nullable varchar — commercial-size classification)
  - lead_interested_products (new table): the normalized replacement for the
    single legacy `leads.interested_product` text field, which is left
    completely untouched for backward compatibility.

Follows the same inspector-gated, idempotent style as
8175f96a08e4_quotations_lead_id_fk.py: every operation checks the live
schema first, so this migration is safe to run against a fresh database
(where create_all() in the baseline revision may already have created all of
this) and safe to re-run.

lead_interested_products.product_id -> products.id and .lead_id -> leads.id
are both ON DELETE CASCADE (a link row has no independent meaning once
either side is gone), with a UNIQUE(lead_id, product_id) constraint so the
same product can never be attached to a lead twice, and indexes on both FK
columns. Unlike the quotations.lead_id migration, this table is entirely new
— SQLite supports inline FK constraints in CREATE TABLE natively (the
ALTER-TABLE-on-an-existing-table limitation doesn't apply here), so no batch
mode is needed.

Revision ID: 39c2af6d9f58
Revises: 8175f96a08e4
Create Date: 2026-09-03 14:46:23.822914

"""
import logging
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '39c2af6d9f58'
down_revision: Union[str, Sequence[str], None] = '8175f96a08e4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

logger = logging.getLogger("alembic.runtime.migration")

_LEADS_TABLE = "leads"
_LINK_TABLE = "lead_interested_products"


def _add_column_if_missing(inspector: sa.Inspector, table: str, column: sa.Column) -> None:
    existing = {c["name"] for c in inspector.get_columns(table)}
    if column.name in existing:
        return
    logger.info("Adding %s.%s", table, column.name)
    op.add_column(table, column)


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if _LEADS_TABLE in inspector.get_table_names():
        _add_column_if_missing(inspector, _LEADS_TABLE, sa.Column("lead_type", sa.String(length=50), nullable=True))
        _add_column_if_missing(inspector, _LEADS_TABLE, sa.Column("segment", sa.String(length=50), nullable=True))
    else:
        logger.info("%s table does not exist yet -- skipping column additions", _LEADS_TABLE)

    inspector = sa.inspect(bind)  # refresh after any column additions
    if _LINK_TABLE in inspector.get_table_names():
        logger.info("%s already exists -- skipping table creation", _LINK_TABLE)
        return

    logger.info("Creating table %s", _LINK_TABLE)
    op.create_table(
        _LINK_TABLE,
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("organization_id", sa.String(length=36), sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("lead_id", sa.String(length=36), sa.ForeignKey("leads.id", ondelete="CASCADE"), nullable=False),
        sa.Column("product_id", sa.String(length=36), sa.ForeignKey("products.id", ondelete="CASCADE"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("lead_id", "product_id", name="uq_lead_interested_product"),
    )
    op.create_index("ix_lead_interested_products_organization_id", _LINK_TABLE, ["organization_id"])
    op.create_index("ix_lead_interested_products_lead_id", _LINK_TABLE, ["lead_id"])
    op.create_index("ix_lead_interested_products_product_id", _LINK_TABLE, ["product_id"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if _LINK_TABLE in inspector.get_table_names():
        op.drop_table(_LINK_TABLE)

    # leads.lead_type / leads.segment are deliberately NOT dropped here: on a
    # database where these columns already had real data by the time anyone
    # downgrades, dropping them would be silent, unrecoverable data loss —
    # the same reasoning the baseline revision's downgrade() documents for
    # not dropping tables.
