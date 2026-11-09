"""categories: names unique per parent (trimmed, case-insensitive) instead of per firm

Until now uq_category_org_name made a name unique across ALL of a firm's categories and
sub categories, so Electronics > Accessories and Footwear > Accessories could not both
exist. The confirmed rule:

  - a top-level name is unique in its organization;
  - a sub category name is unique only under its own parent;
  - both compared trimmed and case-insensitively ("Accessories" = " accessories ").

Two partial unique indexes enforce it:

  ux_categories_org_top_name     (organization_id, lower(trim(name)))            WHERE parent_id IS NULL
  ux_categories_org_parent_name  (organization_id, parent_id, lower(trim(name))) WHERE parent_id IS NOT NULL

Not one unique constraint over (organization_id, parent_id, name): PostgreSQL treats the
NULL parent_id of top-level rows as distinct, so duplicate top-level names would get
through. Partial and expression indexes work on every supported PostgreSQL version;
NULLS NOT DISTINCT (PostgreSQL 15+) is not needed.

Upgrade
  1. Audits the existing rows against the new rule first. Names that differ only by case or
     surrounding spaces were allowed before ("Shoes" and "shoes "), and would make the new
     index fail. If any exist, the upgrade stops with a list of them and changes nothing:
     no category is ever renamed, merged or deleted here.
  2. Creates the two indexes (skipping one that already exists), then drops the old
     constraint (skipping it if already gone), so a partly applied run can be re-run.
  On PostgreSQL all of it runs in the migration's transaction (DDL is transactional), so a
  failure leaves the old rule in place. The categories table is small; a plain CREATE INDEX
  briefly blocks writes to it and needs no CONCURRENTLY.

  SQLite (local dev) keeps the old rule inside the table definition, so dropping it means
  rebuilding the table. Foreign keys are switched off for the rebuild: with them on,
  dropping the old table would fire ON DELETE SET NULL on products.category_id and lose
  every product's category. A foreign-key check runs before they are switched back on.

Downgrade
  Restores uq_category_org_name only if the data allows it: when the same name (trimmed,
  case-insensitive) is used by more than one category of a firm -- now possible under
  different parents -- it refuses and lists them. Nothing is renamed or deleted to make it
  fit.

Revision ID: c4a7e9b2d1f3
Revises: g3b4c5d6e7f8
Create Date: 2026-11-09 12:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c4a7e9b2d1f3"
down_revision: Union[str, Sequence[str], None] = "g3b4c5d6e7f8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "categories"
_OLD = "uq_category_org_name"
_TOP = "ux_categories_org_top_name"
_SUB = "ux_categories_org_parent_name"
_NORM = "lower(trim(name))"

_CREATE_TOP = (
    f'CREATE UNIQUE INDEX IF NOT EXISTS "{_TOP}" ON "{_TABLE}" (organization_id, {_NORM}) '
    "WHERE parent_id IS NULL"
)
_CREATE_SUB = (
    f'CREATE UNIQUE INDEX IF NOT EXISTS "{_SUB}" ON "{_TABLE}" (organization_id, parent_id, {_NORM}) '
    "WHERE parent_id IS NOT NULL"
)

# Groups that break the new rule: same firm, same parent (or both top-level), same
# trimmed lower-case name.
_NEW_RULE_DUPLICATES = f"""
    SELECT organization_id, parent_id, {_NORM} AS norm_name, COUNT(*) AS n
    FROM {_TABLE}
    GROUP BY organization_id, parent_id, {_NORM}
    HAVING COUNT(*) > 1
    ORDER BY organization_id, parent_id, norm_name
"""
# Groups that break the old rule once restored: same firm, same name, any parent.
_OLD_RULE_DUPLICATES = f"""
    SELECT organization_id, {_NORM} AS norm_name, COUNT(*) AS n
    FROM {_TABLE}
    GROUP BY organization_id, {_NORM}
    HAVING COUNT(*) > 1
    ORDER BY organization_id, norm_name
"""


def _bind():
    return op.get_bind()


def _is_sqlite() -> bool:
    return _bind().dialect.name == "sqlite"


def _index_names() -> set[str]:
    return {ix["name"] for ix in sa.inspect(_bind()).get_indexes(_TABLE)}


def _old_rule_present() -> bool:
    """The old rule as a unique constraint (create_all / SQLite) or a bare unique index."""
    inspector = sa.inspect(_bind())
    constraints = {uc["name"] for uc in inspector.get_unique_constraints(_TABLE)}
    return _OLD in constraints or _OLD in _index_names()


def _describe(rows, with_parent: bool) -> str:
    lines = []
    for row in rows[:50]:
        where = f" parent_id={row.parent_id or 'NULL (top level)'}" if with_parent else ""
        sql = f"SELECT id, name FROM {_TABLE} WHERE organization_id = :org AND {_NORM} = :norm"
        params = {"org": row.organization_id, "norm": row.norm_name}
        if with_parent and row.parent_id is None:
            sql += " AND parent_id IS NULL"
        elif with_parent:
            sql += " AND parent_id = :parent"
            params["parent"] = row.parent_id
        names = _bind().execute(sa.text(sql + " ORDER BY id"), params).fetchall()
        listed = ", ".join(f"{r.id} {r.name!r}" for r in names)
        lines.append(f"  organization_id={row.organization_id}{where}: {row.n} rows -> {listed}")
    if len(rows) > 50:
        lines.append(f"  ... and {len(rows) - 50} more groups")
    return "\n".join(lines)


def _drop_old_rule() -> None:
    if not _old_rule_present():
        return
    if _OLD in _index_names() and _OLD not in {
        uc["name"] for uc in sa.inspect(_bind()).get_unique_constraints(_TABLE)
    }:
        op.drop_index(_OLD, table_name=_TABLE)
        return
    if _is_sqlite():
        _sqlite_rebuild(lambda batch: batch.drop_constraint(_OLD, type_="unique"))
    else:
        op.drop_constraint(_OLD, _TABLE, type_="unique")


def _sqlite_rebuild(change) -> None:  # noqa: ANN001
    """Rebuild the SQLite table with foreign keys off (see the module docstring), then
    verify every foreign key still resolves before switching them back on."""
    with op.get_context().autocommit_block():
        op.execute("PRAGMA foreign_keys=OFF")
    try:
        # Expression indexes are not reflected on SQLite, so the rebuilt table would lose
        # them: drop ours first and recreate them after.
        for name in (_TOP, _SUB):
            if name in _index_names():
                op.drop_index(name, table_name=_TABLE)
        with op.batch_alter_table(_TABLE, recreate="always") as batch:
            change(batch)
        broken = _bind().execute(sa.text("PRAGMA foreign_key_check")).fetchall()
        if broken:
            raise RuntimeError(f"Foreign key check failed after rebuilding {_TABLE}: {broken[:10]}")
    finally:
        with op.get_context().autocommit_block():
            op.execute("PRAGMA foreign_keys=ON")


def upgrade() -> None:
    duplicates = _bind().execute(sa.text(_NEW_RULE_DUPLICATES)).fetchall()
    if duplicates:
        raise RuntimeError(
            "Cannot apply the per-parent, case-insensitive category name rule: these categories "
            "share a name (ignoring case and surrounding spaces) with a sibling. Rename them in the "
            "app, then run the migration again. Nothing was changed.\n" + _describe(duplicates, True)
        )
    if _is_sqlite():
        # Rebuilding drops expression indexes, so drop the old rule first.
        _drop_old_rule()
        op.execute(_CREATE_TOP)
        op.execute(_CREATE_SUB)
    else:
        op.execute(_CREATE_TOP)
        op.execute(_CREATE_SUB)
        _drop_old_rule()


def downgrade() -> None:
    duplicates = _bind().execute(sa.text(_OLD_RULE_DUPLICATES)).fetchall()
    if duplicates:
        raise RuntimeError(
            "Cannot restore uq_category_org_name (one name per organization): these names are used "
            "by more than one category of the same organization, under different parents. Rename "
            "them in the app first; nothing was changed.\n" + _describe(duplicates, False)
        )
    if not _old_rule_present():
        if _is_sqlite():
            _sqlite_rebuild(lambda batch: batch.create_unique_constraint(_OLD, ["organization_id", "name"]))
        else:
            op.create_unique_constraint(_OLD, _TABLE, ["organization_id", "name"])
    for name in (_TOP, _SUB):
        if name in _index_names():
            op.drop_index(name, table_name=_TABLE)
