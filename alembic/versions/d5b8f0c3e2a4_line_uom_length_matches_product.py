"""document lines: unit of measure up to 50 characters, the same as the product's

A product's uom is VARCHAR(50), but the line tables that copy it held only VARCHAR(30).
An order, GRN or purchase line takes the product's unit when the client sends none, so a
product with a unit of 31-50 characters made the whole save fail with "value too long
for type character varying(30)" (a 500) on PostgreSQL. SQLite never checks the length.

Widened to VARCHAR(50):
  quotation_items.uom, sales_order_items.uom, invoice_items.uom,
  goods_receipt_note_items.unit_of_measure_uom,
  purchase_invoice_items.unit_of_measure_uom,
  purchase_return_items.unit_of_measure_uom

Widening a VARCHAR on PostgreSQL only changes the catalog: no table rewrite, no data
change. Each column is checked first, so the revision is safe to re-run. SQLite does
nothing (it does not enforce VARCHAR lengths).

The downgrade narrows them back only when every stored value still fits in 30 characters;
otherwise it refuses and says where, and changes nothing.

Revision ID: d5b8f0c3e2a4
Revises: c4a7e9b2d1f3
Create Date: 2026-11-09 15:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d5b8f0c3e2a4"
down_revision: Union[str, Sequence[str], None] = "c4a7e9b2d1f3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_COLUMNS = (
    ("quotation_items", "uom"),
    ("sales_order_items", "uom"),
    ("invoice_items", "uom"),
    ("goods_receipt_note_items", "unit_of_measure_uom"),
    ("purchase_invoice_items", "unit_of_measure_uom"),
    ("purchase_return_items", "unit_of_measure_uom"),
)
_WIDE, _NARROW = 50, 30


def _length(table: str, column: str) -> int | None:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table(table):
        return None
    for col in inspector.get_columns(table):
        if col["name"] == column:
            return getattr(col["type"], "length", None)
    return None


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    for table, column in _COLUMNS:
        length = _length(table, column)
        if length is not None and length < _WIDE:
            op.alter_column(table, column, type_=sa.String(length=_WIDE),
                            existing_type=sa.String(length=length), existing_nullable=True)


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    too_long = []
    for table, column in _COLUMNS:
        if _length(table, column) is None:
            continue
        n = op.get_bind().execute(
            sa.text(f'SELECT count(*) FROM "{table}" WHERE length("{column}") > {_NARROW}')
        ).scalar()
        if n:
            too_long.append(f"{table}.{column}: {n} rows")
    if too_long:
        raise RuntimeError(
            "Cannot narrow unit-of-measure columns back to VARCHAR(30): these rows hold longer "
            "units. Shorten them in the app first; nothing was changed.\n  " + "\n  ".join(too_long)
        )
    for table, column in _COLUMNS:
        length = _length(table, column)
        if length is not None and length > _NARROW:
            op.alter_column(table, column, type_=sa.String(length=_NARROW),
                            existing_type=sa.String(length=length), existing_nullable=True)
