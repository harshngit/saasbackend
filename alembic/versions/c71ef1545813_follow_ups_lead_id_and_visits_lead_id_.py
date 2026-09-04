"""follow_ups.lead_id (new) + FK/index; visits.lead_id FK/index hardening

Leads backend addendum: a Follow-up may now belong directly to a Lead (no
Visit required), matching the support Visit already has. Two things happen
here:

1. follow_ups.lead_id is genuinely new — added with its FK and index, same
   as any brand new nullable relationship column in this project.

2. visits.lead_id already exists as a model field (added in an earlier,
   pre-Alembic change), but per the exact lesson from
   8175f96a08e4_quotations_lead_id_fk.py, a column that reached an
   already-existing table only through
   app.core.database.auto_add_missing_columns()'s bare
   `ALTER TABLE ... ADD COLUMN` never got its FK constraint — that helper
   only ever adds the column. So this migration re-checks visits.lead_id's
   FK and index the same defensive way, and only does anything if they
   genuinely turn out to be missing on a given database (verified against
   this project's own dev database: already fully correct there, so this
   step is expected to no-op most places it runs — but production is not
   guaranteed to be in that state, which is exactly why this check exists
   rather than being skipped).

Both tables get the same idempotent, inspector-gated treatment as the
quotations.lead_id migration: safe to run against a fresh database (where
create_all() in the baseline revision already created everything), an
existing database missing the column entirely, or an existing database that
has the column but not the constraint — and safe to re-run.

ON DELETE SET NULL on both (not CASCADE): deleting an unconverted Lead must
not delete the Follow-ups/Visits recorded against it.

Revision ID: c71ef1545813
Revises: 39c2af6d9f58
Create Date: 2026-09-04 11:46:04.474682

"""
import logging
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'c71ef1545813'
down_revision: Union[str, Sequence[str], None] = '39c2af6d9f58'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

logger = logging.getLogger("alembic.runtime.migration")

_REFERENT = "leads"
_COLUMN = "lead_id"

# (table, FK constraint name, index name)
_TARGETS = [
    ("follow_ups", "fk_follow_ups_lead_id_leads", "ix_follow_ups_lead_id"),
    ("visits", "fk_visits_lead_id_leads", "ix_visits_lead_id"),
]


def _ensure_lead_id(bind, table: str, fk_name: str, index_name: str) -> None:
    inspector = sa.inspect(bind)
    if table not in inspector.get_table_names():
        logger.info("%s table does not exist yet -- skipping", table)
        return

    existing_columns = {c["name"] for c in inspector.get_columns(table)}
    if _COLUMN not in existing_columns:
        logger.info("Adding %s.%s", table, _COLUMN)
        op.add_column(table, sa.Column(_COLUMN, sa.String(length=36), nullable=True))
        inspector = sa.inspect(bind)

    # Orphan cleanup before adding the FK -- cheap no-op when there are none.
    orphan_condition = f'"{_COLUMN}" IS NOT NULL AND "{_COLUMN}" NOT IN (SELECT "id" FROM "{_REFERENT}")'
    orphan_count = bind.execute(sa.text(f'SELECT COUNT(*) FROM "{table}" WHERE {orphan_condition}')).scalar()
    if orphan_count:
        bind.execute(sa.text(f'UPDATE "{table}" SET "{_COLUMN}" = NULL WHERE {orphan_condition}'))
        logger.warning(
            "Cleaned up %d orphaned %s.%s value(s) referencing a deleted %s row "
            "(set to NULL, matching ON DELETE SET NULL behavior)",
            orphan_count, table, _COLUMN, _REFERENT,
        )

    has_lead_fk = any(
        fk["constrained_columns"] == [_COLUMN] and fk["referred_table"] == _REFERENT
        for fk in inspector.get_foreign_keys(table)
    )
    if not has_lead_fk:
        logger.info("Adding FK %s (%s.%s -> %s.id) ON DELETE SET NULL", fk_name, table, _COLUMN, _REFERENT)
        if bind.dialect.name == "sqlite":
            # SQLite cannot ALTER TABLE ... ADD CONSTRAINT on an existing
            # table -- batch mode recreates the table under the hood with
            # the constraint included, copies the data across, and swaps it in.
            with op.batch_alter_table(table, schema=None) as batch_op:
                batch_op.create_foreign_key(fk_name, _REFERENT, [_COLUMN], ["id"], ondelete="SET NULL")
        else:
            op.create_foreign_key(fk_name, table, _REFERENT, [_COLUMN], ["id"], ondelete="SET NULL")
    else:
        existing_fk_names = {fk["name"] for fk in inspector.get_foreign_keys(table) if fk["name"]}
        if fk_name not in existing_fk_names:
            logger.info(
                "A FK from %s.%s to %s already exists under a different name -- leaving it as-is",
                table, _COLUMN, _REFERENT,
            )

    inspector = sa.inspect(bind)
    existing_index_names = {ix["name"] for ix in inspector.get_indexes(table)}
    if index_name not in existing_index_names:
        logger.info("Adding index %s", index_name)
        op.create_index(index_name, table, [_COLUMN])


def upgrade() -> None:
    bind = op.get_bind()
    for table, fk_name, index_name in _TARGETS:
        _ensure_lead_id(bind, table, fk_name, index_name)


def downgrade() -> None:
    """Reverses only what this migration is guaranteed to have added: the
    two indexes and the two named FK constraints. Deliberately does NOT
    drop follow_ups.lead_id or visits.lead_id — visits.lead_id in
    particular may have pre-dated this migration on some databases, with
    real data in it; dropping it here would be silent, unrecoverable data
    loss, the same reasoning every other migration in this project's
    downgrade() follows.
    """
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    for table, fk_name, index_name in _TARGETS:
        if table not in inspector.get_table_names():
            continue
        existing_index_names = {ix["name"] for ix in inspector.get_indexes(table)}
        if index_name in existing_index_names:
            op.drop_index(index_name, table_name=table)
        existing_fk_names = {fk["name"] for fk in inspector.get_foreign_keys(table) if fk["name"]}
        if fk_name in existing_fk_names:
            if bind.dialect.name == "sqlite":
                with op.batch_alter_table(table, schema=None) as batch_op:
                    batch_op.drop_constraint(fk_name, type_="foreignkey")
            else:
                op.drop_constraint(fk_name, table, type_="foreignkey")
