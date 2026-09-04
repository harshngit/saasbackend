"""teams table and users.team_id

Team Management + Team Data Scope:

  - teams (new table): id, organization_id (FK organizations, CASCADE), name,
    manager_id (FK users, SET NULL), created_at, updated_at, with
    UNIQUE(organization_id, name) so a team name only has to be unique within
    one firm.
  - users.team_id (new column): FK -> teams.id, ON DELETE SET NULL, indexed.
    Nullable — membership is optional and, structurally, at most one Team per
    user (no join table).

Team Data Scope itself (app.core.scoping / app.core.permissions.DATA_SCOPES)
needs no schema change: Role.data_scope is a plain, unconstrained string
column, so "team" simply becomes a third accepted value at the Pydantic
validation layer.

Same inspector-gated, idempotent style as every other migration in this
project: safe against a fresh database (baseline create_all() may already
have created both) and safe to re-run. teams is a brand new table (SQLite
supports its inline FK constraints natively, no batch mode needed); users
already exists, so its new team_id column follows the same defensive
column-then-orphan-cleanup-then-FK-then-index sequence as every other
FK-onto-an-existing-table migration in this project.

Revision ID: 9699ffe936d6
Revises: 83489d0ea989
Create Date: 2026-09-04 18:00:00.000000

"""
import logging
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '9699ffe936d6'
down_revision: Union[str, Sequence[str], None] = '83489d0ea989'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

logger = logging.getLogger("alembic.runtime.migration")

_TEAMS = "teams"
_USERS = "users"
_TEAM_FK = "fk_users_team_id_teams"
_TEAM_INDEX = "ix_users_team_id"
_TEAM_UNIQUE = "uq_team_org_name"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if _TEAMS not in inspector.get_table_names():
        logger.info("Creating %s", _TEAMS)
        op.create_table(
            _TEAMS,
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column(
                "organization_id", sa.String(length=36),
                sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False,
            ),
            sa.Column("name", sa.String(length=150), nullable=False),
            sa.Column(
                "manager_id", sa.String(length=36),
                sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True,
            ),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("organization_id", "name", name=_TEAM_UNIQUE),
        )
    else:
        logger.info("%s already exists -- skipping table creation", _TEAMS)

    inspector = sa.inspect(bind)
    existing_indexes = {ix["name"] for ix in inspector.get_indexes(_TEAMS)} if _TEAMS in inspector.get_table_names() else set()
    if "ix_teams_organization_id" not in existing_indexes:
        op.create_index("ix_teams_organization_id", _TEAMS, ["organization_id"])
    if "ix_teams_manager_id" not in existing_indexes:
        op.create_index("ix_teams_manager_id", _TEAMS, ["manager_id"])

    # users.team_id -- same defensive sequence as every other FK-onto-an-
    # existing-table migration in this project (see e.g.
    # c71ef1545813_follow_ups_lead_id_and_visits_lead_id_.py).
    inspector = sa.inspect(bind)
    existing_columns = {c["name"] for c in inspector.get_columns(_USERS)}
    if "team_id" not in existing_columns:
        logger.info("Adding %s.team_id", _USERS)
        op.add_column(_USERS, sa.Column("team_id", sa.String(length=36), nullable=True))
        inspector = sa.inspect(bind)

    orphan_condition = '"team_id" IS NOT NULL AND "team_id" NOT IN (SELECT "id" FROM "teams")'
    orphan_count = bind.execute(sa.text(f'SELECT COUNT(*) FROM "{_USERS}" WHERE {orphan_condition}')).scalar()
    if orphan_count:
        bind.execute(sa.text(f'UPDATE "{_USERS}" SET "team_id" = NULL WHERE {orphan_condition}'))
        logger.warning("Cleaned up %d orphaned users.team_id value(s)", orphan_count)

    has_team_fk = any(
        fk["constrained_columns"] == ["team_id"] and fk["referred_table"] == _TEAMS
        for fk in inspector.get_foreign_keys(_USERS)
    )
    if not has_team_fk:
        logger.info("Adding FK %s (%s.team_id -> %s.id) ON DELETE SET NULL", _TEAM_FK, _USERS, _TEAMS)
        if bind.dialect.name == "sqlite":
            with op.batch_alter_table(_USERS, schema=None) as batch_op:
                batch_op.create_foreign_key(_TEAM_FK, _TEAMS, ["team_id"], ["id"], ondelete="SET NULL")
        else:
            op.create_foreign_key(_TEAM_FK, _USERS, _TEAMS, ["team_id"], ["id"], ondelete="SET NULL")

    inspector = sa.inspect(bind)
    existing_user_indexes = {ix["name"] for ix in inspector.get_indexes(_USERS)}
    if _TEAM_INDEX not in existing_user_indexes:
        logger.info("Adding index %s", _TEAM_INDEX)
        op.create_index(_TEAM_INDEX, _USERS, ["team_id"])


def downgrade() -> None:
    """Drops users.team_id's FK/index and the teams table.

    users.team_id itself is left in place (same reasoning as every other
    downgrade in this project: by the time anyone downgrades it may hold
    real, meaningful data) — but with `teams` gone, an orphaned team_id value
    is harmless (the FK is already dropped first) and simply stops being
    interpretable.
    """
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if _USERS in inspector.get_table_names():
        existing_user_indexes = {ix["name"] for ix in inspector.get_indexes(_USERS)}
        if _TEAM_INDEX in existing_user_indexes:
            op.drop_index(_TEAM_INDEX, table_name=_USERS)
        existing_fk_names = {fk["name"] for fk in inspector.get_foreign_keys(_USERS) if fk["name"]}
        if _TEAM_FK in existing_fk_names:
            if bind.dialect.name == "sqlite":
                with op.batch_alter_table(_USERS, schema=None) as batch_op:
                    batch_op.drop_constraint(_TEAM_FK, type_="foreignkey")
            else:
                op.drop_constraint(_TEAM_FK, _USERS, type_="foreignkey")

    if _TEAMS in inspector.get_table_names():
        op.drop_table(_TEAMS)
