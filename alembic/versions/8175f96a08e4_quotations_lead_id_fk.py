"""quotations.lead_id: column + FK(leads.id) ON DELETE SET NULL + index

Phase 2 added Quotation.lead_id to the model. On a brand new database,
create_all() (see the baseline revision this one follows) already creates the
column, the foreign key and the index together, correctly. On an
already-existing database, the column was — until this migration — only ever
added via app.core.database.auto_add_missing_columns()'s bare
`ALTER TABLE ... ADD COLUMN`, which never added the constraint (SQLite cannot
add a foreign key to an existing table via ALTER TABLE at all; the legacy
helper never attempted the constraint on any engine — see that module's
docstring). This migration is the actual fix: it brings any such database
up to the same end state a fresh one already has, safely and idempotently,
without altering `app/core/database.py`'s existing forward-compatibility
behavior (which stays exactly as-is — see that module for the deprecation
note).

Every step below is guarded by an explicit existence check via the live
inspector, so this migration is safe to run against all four scenarios in
one code path:
  1. Fresh database          -> everything already exists (from create_all
                                 in the baseline revision) -> every step
                                 no-ops.
  2. Existing DB, no lead_id -> column, index and FK are all added.
  3. Existing DB, lead_id
     present but no FK       -> column/index steps no-op; orphan cleanup
                                 runs; FK is added.
  4. Already fully migrated  -> every step no-ops. Re-running this migration
                                 (e.g. `alembic upgrade head` a second time)
                                 is always safe.

Orphan cleanup: before adding the FK, any quotations.lead_id that points at a
Lead row that no longer exists is set to NULL — the same outcome
ON DELETE SET NULL would have produced had the constraint existed when that
Lead was deleted. This is the only data-modifying step in this migration, is
run unconditionally (cheap, and a correct no-op when there are no orphans),
and is logged with the exact count affected.

Revision ID: 8175f96a08e4
Revises: 16168c6e504c
Create Date: 2026-09-03 11:46:18.407533

"""
import logging
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '8175f96a08e4'
down_revision: Union[str, Sequence[str], None] = '16168c6e504c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

logger = logging.getLogger("alembic.runtime.migration")

_TABLE = "quotations"
_COLUMN = "lead_id"
_REFERENT = "leads"
_FK_NAME = "fk_quotations_lead_id_leads"
_INDEX_NAME = "ix_quotations_lead_id"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if _TABLE not in inspector.get_table_names():
        # This database doesn't have the quotations table at all yet (should
        # not happen after the baseline revision, but never assume) --
        # nothing for this migration to do.
        logger.info("%s table does not exist yet -- skipping", _TABLE)
        return

    existing_columns = {c["name"] for c in inspector.get_columns(_TABLE)}
    if _COLUMN not in existing_columns:
        logger.info("Adding %s.%s", _TABLE, _COLUMN)
        op.add_column(_TABLE, sa.Column(_COLUMN, sa.String(length=36), nullable=True))
        inspector = sa.inspect(bind)  # refresh after the DDL change

    # Orphan cleanup: a lead_id pointing at a Lead row that no longer exists
    # (possible on a database that has had this column for a while without
    # the FK enforcing referential integrity). Safe, cheap no-op when there
    # are none.
    # Counted with a SELECT first rather than trusting the UPDATE's own
    # rowcount: that comes back unreliable (0/-1) for a bare text() UPDATE on
    # some driver/execution-context combinations, even when rows genuinely
    # changed — this way the log accurately reflects what happened either way.
    orphan_condition = (
        f'"{_COLUMN}" IS NOT NULL AND "{_COLUMN}" NOT IN (SELECT "id" FROM "{_REFERENT}")'
    )
    orphan_count = bind.execute(
        sa.text(f'SELECT COUNT(*) FROM "{_TABLE}" WHERE {orphan_condition}')
    ).scalar()
    if orphan_count:
        bind.execute(sa.text(f'UPDATE "{_TABLE}" SET "{_COLUMN}" = NULL WHERE {orphan_condition}'))
        logger.warning(
            "Cleaned up %d orphaned %s.%s value(s) referencing a deleted %s row "
            "(set to NULL, matching ON DELETE SET NULL behavior)",
            orphan_count, _TABLE, _COLUMN, _REFERENT,
        )

    existing_fk_names = {fk["name"] for fk in inspector.get_foreign_keys(_TABLE) if fk["name"]}
    has_lead_fk = any(
        fk["constrained_columns"] == [_COLUMN] and fk["referred_table"] == _REFERENT
        for fk in inspector.get_foreign_keys(_TABLE)
    )
    if not has_lead_fk:
        logger.info("Adding FK %s (%s.%s -> %s.id) ON DELETE SET NULL", _FK_NAME, _TABLE, _COLUMN, _REFERENT)
        if bind.dialect.name == "sqlite":
            # SQLite cannot ALTER TABLE ... ADD CONSTRAINT on an existing
            # table -- batch mode recreates the table under the hood with the
            # constraint included, copies the data across, and swaps it in.
            with op.batch_alter_table(_TABLE, schema=None) as batch_op:
                batch_op.create_foreign_key(
                    _FK_NAME, _REFERENT, [_COLUMN], ["id"], ondelete="SET NULL"
                )
        else:
            op.create_foreign_key(
                _FK_NAME, _TABLE, _REFERENT, [_COLUMN], ["id"], ondelete="SET NULL"
            )
    elif _FK_NAME not in existing_fk_names:
        logger.info(
            "A FK from %s.%s to %s already exists under a different name -- leaving it as-is",
            _TABLE, _COLUMN, _REFERENT,
        )

    existing_index_names = {ix["name"] for ix in inspector.get_indexes(_TABLE)}
    if _INDEX_NAME not in existing_index_names:
        logger.info("Adding index %s", _INDEX_NAME)
        op.create_index(_INDEX_NAME, _TABLE, [_COLUMN])


def downgrade() -> None:
    """Reverses only what this migration is guaranteed to have added: the
    index and the named FK constraint. Deliberately does NOT drop the
    lead_id column itself -- on a database where the column pre-dated this
    migration (added by the legacy auto_add_missing_columns() path), dropping
    it here would destroy real data this migration never created, which is
    exactly the kind of silent destructive operation this project's
    migration policy forbids.
    """
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if _TABLE not in inspector.get_table_names():
        return

    existing_index_names = {ix["name"] for ix in inspector.get_indexes(_TABLE)}
    if _INDEX_NAME in existing_index_names:
        op.drop_index(_INDEX_NAME, table_name=_TABLE)

    existing_fk_names = {fk["name"] for fk in inspector.get_foreign_keys(_TABLE) if fk["name"]}
    if _FK_NAME in existing_fk_names:
        if bind.dialect.name == "sqlite":
            with op.batch_alter_table(_TABLE, schema=None) as batch_op:
                batch_op.drop_constraint(_FK_NAME, type_="foreignkey")
        else:
            op.drop_constraint(_FK_NAME, _TABLE, type_="foreignkey")
