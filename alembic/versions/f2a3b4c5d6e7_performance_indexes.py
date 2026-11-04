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


def _invalid_on_postgres(name: str) -> bool:
    """A CREATE INDEX CONCURRENTLY that was interrupted leaves an INVALID index behind:
    it exists, but PostgreSQL never uses it. Such an index is dropped and built again."""
    row = op.get_bind().execute(
        sa.text("SELECT NOT i.indisvalid FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
                "WHERE c.relname = :name"),
        {"name": name},
    ).first()
    return bool(row and row[0])


def upgrade() -> None:
    postgres = op.get_bind().dialect.name == "postgresql"
    for name, table, columns in _INDEXES:
        if postgres:
            if name in _existing(table) and not _invalid_on_postgres(name):
                continue
            with op.get_context().autocommit_block():
                op.execute(sa.text(f'DROP INDEX CONCURRENTLY IF EXISTS "{name}"'))
                op.create_index(name, table, columns, postgresql_concurrently=True)
        elif name not in _existing(table):
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
