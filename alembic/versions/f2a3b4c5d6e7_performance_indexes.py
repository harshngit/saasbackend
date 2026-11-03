"""performance indexes: sales orders by firm + date / status, reservations by item

Adds three indexes and changes nothing else:
  ix_sales_orders_org_created_at          sales_orders (organization_id, created_at)
  ix_sales_orders_org_status              sales_orders (organization_id, status)
  ix_stock_reservations_wh_product_status stock_reservations (warehouse_id, product_id, status)

On PostgreSQL they are built CONCURRENTLY (outside a transaction), so the tables stay
writable while they build. Each step is guarded, so it is safe on a database where
create_all already made them.

Revision ID: f2a3b4c5d6e7
Revises: e1f2a3b4c5d6
Create Date: 2026-11-03 12:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f2a3b4c5d6e7"
down_revision: Union[str, Sequence[str], None] = "e1f2a3b4c5d6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_INDEXES = (
    ("ix_sales_orders_org_created_at", "sales_orders", ["organization_id", "created_at"]),
    ("ix_sales_orders_org_status", "sales_orders", ["organization_id", "status"]),
    ("ix_stock_reservations_wh_product_status", "stock_reservations", ["warehouse_id", "product_id", "status"]),
)


def _existing(table: str) -> set[str]:
    return {ix["name"] for ix in sa.inspect(op.get_bind()).get_indexes(table)}


def upgrade() -> None:
    postgres = op.get_bind().dialect.name == "postgresql"
    for name, table, columns in _INDEXES:
        if name in _existing(table):
            continue
        if postgres:
            with op.get_context().autocommit_block():
                op.create_index(name, table, columns, postgresql_concurrently=True)
        else:
            op.create_index(name, table, columns)


def downgrade() -> None:
    postgres = op.get_bind().dialect.name == "postgresql"
    for name, table, _columns in reversed(_INDEXES):
        if name not in _existing(table):
            continue
        if postgres:
            with op.get_context().autocommit_block():
                op.drop_index(name, table_name=table, postgresql_concurrently=True)
        else:
            op.drop_index(name, table_name=table)
