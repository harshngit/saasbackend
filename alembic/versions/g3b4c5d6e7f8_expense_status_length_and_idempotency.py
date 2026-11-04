"""expenses: status VARCHAR(20) -> VARCHAR(30); idempotency key for POST /expenses

1. expenses.status grows to VARCHAR(30). The stored value "clarification_requested" is
   23 characters, so on Postgres PATCH /expenses/{id}/request-clarification failed with
   "value too long for type character varying(20)" (SQLite never checks the length).
   Widening a VARCHAR on Postgres only changes the catalog: no table rewrite.
2. expenses.idempotency_key (VARCHAR(100), nullable) and a unique index on
   (organization_id, submitted_by, idempotency_key): a retried create returns the first
   expense instead of making a duplicate. Existing rows keep NULL, which never collides.

Every step checks first, so the revision is safe on a database where create_all or the
startup column step already added the column.

Revision ID: g3b4c5d6e7f8
Revises: f2a3b4c5d6e7
Create Date: 2026-11-04 18:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "g3b4c5d6e7f8"
down_revision: Union[str, Sequence[str], None] = "f2a3b4c5d6e7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_INDEX = "ux_expenses_idempotency"


def _columns() -> dict[str, dict]:
    return {c["name"]: c for c in sa.inspect(op.get_bind()).get_columns("expenses")}


def _indexes() -> set[str]:
    return {ix["name"] for ix in sa.inspect(op.get_bind()).get_indexes("expenses")}


def upgrade() -> None:
    is_postgres = op.get_bind().dialect.name == "postgresql"
    status = _columns()["status"]
    if is_postgres and (getattr(status["type"], "length", None) or 0) < 30:
        op.alter_column("expenses", "status", type_=sa.String(length=30),
                        existing_type=status["type"], existing_nullable=False)

    if "idempotency_key" not in _columns():
        op.add_column("expenses", sa.Column("idempotency_key", sa.String(length=100), nullable=True))
    if _INDEX not in _indexes():
        op.create_index(_INDEX, "expenses", ["organization_id", "submitted_by", "idempotency_key"], unique=True)


def downgrade() -> None:
    if _INDEX in _indexes():
        op.drop_index(_INDEX, table_name="expenses")
    if "idempotency_key" in _columns():
        op.drop_column("expenses", "idempotency_key")
    if op.get_bind().dialect.name == "postgresql":
        too_long = op.get_bind().execute(sa.text("SELECT count(*) FROM expenses WHERE length(status) > 20")).scalar()
        if too_long:
            raise RuntimeError(
                f"{too_long} expenses have status 'clarification_requested', which does not fit VARCHAR(20). "
                "Resolve them (approve / reject) before downgrading."
            )
        op.alter_column("expenses", "status", type_=sa.String(length=20),
                        existing_type=sa.String(length=30), existing_nullable=False)
