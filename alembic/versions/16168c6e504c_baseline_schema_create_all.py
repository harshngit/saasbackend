"""baseline schema (create_all)

This project's schema was, until now, entirely created and evolved by
app.core.database.Base.metadata.create_all() plus a set of hand-written
forward-only compatibility functions in app/core/database.py
(auto_add_missing_columns, extend_pg_enum_types, relax_not_null_columns,
widen_columns_to_text, add_columns_with_default, drop_legacy_columns,
enforce_unique_email, enforce_unique_google_id — see that module's updated
module docstring for the full policy going forward).

This revision is the adoption point for Alembic as the project's official
migration tool. It deliberately does NOT hand-author a from-scratch schema:
create_all() only ever creates tables that do not already exist and never
alters an existing one, so calling it here is exactly as safe run against an
already-populated production database (every table already exists -> pure
no-op) as it is against a brand new one (every table is created in one shot,
matching the models exactly). This lets `alembic upgrade head` be the very
first Alembic command ever run against this project's existing databases,
with no separate `alembic stamp` step required.

Everything from here forward is a real, hand-written, reviewed Alembic
migration — see 6f1c2b9a1d3e_quotations_lead_id_fk.py for the first one.

Revision ID: 16168c6e504c
Revises:
Create Date: 2026-09-03 11:45:56.393958

"""
from typing import Sequence, Union

from alembic import op

from app.core.database import Base


# revision identifiers, used by Alembic.
revision: str = '16168c6e504c'
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create any table the models declare that the database doesn't already
    have. A no-op for every table that already exists."""
    Base.metadata.create_all(bind=op.get_bind())


def downgrade() -> None:
    """Deliberately a no-op. This revision never has enough information to
    downgrade safely: on a database that already had these tables before
    Alembic was ever introduced, "downgrading" this revision would mean
    dropping every application table -- an unrecoverable data-loss operation
    this project's own migration policy (see app/core/database.py) explicitly
    forbids being silent about. If you actually need to tear down a schema,
    do it deliberately and explicitly, not via `alembic downgrade`.
    """
    pass
