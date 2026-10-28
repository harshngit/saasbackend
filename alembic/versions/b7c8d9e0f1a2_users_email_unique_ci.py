"""users: one login email per account platform-wide, ignoring case (ux_users_email_ci)

Login emails are unique across every firm and the platform, case-insensitively. Until
now only uq_user_org_email (same firm, exact spelling) was a real migration; the global
index came from the startup helper app.core.database.enforce_unique_email, which skips
it — leaving the rule unenforced — whenever conflicting rows exist.

This revision makes the rule a reviewed, deterministic step:
- refuses (raises, changing nothing) while any two accounts share an email ignoring case
  or surrounding spaces, and says how to list them (masked, read-only):
  `python -m app.scripts.email_conflicts`. It never merges, renames, deactivates or
  deletes accounts — resolving conflicts is an authorized owner's decision;
- creates UNIQUE INDEX ux_users_email_ci ON users (LOWER(email)) — the same name and
  expression the startup helper and the User model use, so an index already built by
  either is accepted as is (an index of that name with another definition is refused);
- plain, transactional CREATE INDEX (not CONCURRENTLY): `users` is small, the write lock
  lasts as long as the build, and a failure leaves nothing behind — whereas a failed
  CONCURRENTLY build leaves an INVALID index that IF NOT EXISTS would then accept.
uq_user_org_email stays.

Downgrade drops the index. Note the startup helper recreates it on the next boot when
the data is clean.

Revision ID: b7c8d9e0f1a2
Revises: a6b7c8d9e0f1
Create Date: 2026-10-28 12:00:00.000000
"""
import re
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b7c8d9e0f1a2"
down_revision: Union[str, Sequence[str], None] = "a6b7c8d9e0f1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_INDEX = "ux_users_email_ci"

# Conflicts as the application sees them: trimmed and lowercased. Counts only.
_CONFLICTS = sa.text(
    "SELECT COUNT(*), COALESCE(SUM(c), 0) FROM ("
    "SELECT COUNT(*) AS c FROM users GROUP BY LOWER(TRIM(email)) HAVING COUNT(*) > 1) d"
)


def _existing_definition(bind) -> str | None:  # noqa: ANN001
    if bind.dialect.name == "postgresql":
        row = bind.execute(
            sa.text("SELECT indexdef FROM pg_indexes "
                    "WHERE schemaname = current_schema() AND tablename = 'users' AND indexname = :n"),
            {"n": _INDEX},
        ).first()
    else:
        row = bind.execute(
            sa.text("SELECT sql FROM sqlite_master WHERE type = 'index' AND name = :n"), {"n": _INDEX}
        ).first()
    return row[0] if row else None


def upgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table("users"):
        return

    groups, accounts = bind.execute(_CONFLICTS).one()
    if groups:
        raise RuntimeError(
            f"{_INDEX} not created: {groups} email(s) are shared by {accounts} accounts "
            "(ignoring case and surrounding spaces). Nothing was changed. List them, masked and "
            "read-only, with `python -m app.scripts.email_conflicts`; an authorized owner must "
            "resolve each one (change an address or remove the stray account) before re-running "
            "`alembic upgrade head`. This migration never merges or edits accounts itself."
        )

    existing = _existing_definition(bind)
    if existing is not None:
        # Postgres spells it `... USING btree (lower((email)::text))`, SQLite as written.
        compact = re.sub(r'[\s()"]|::text', "", existing.lower())
        if "createuniqueindex" not in compact or not compact.endswith("loweremail"):
            raise RuntimeError(
                f"An index named {_INDEX} already exists with a different definition "
                f"({existing!r}); drop or rename it, then re-run `alembic upgrade head`."
            )
        if bind.dialect.name == "postgresql":
            valid = bind.execute(
                sa.text("SELECT i.indisvalid FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
                        "JOIN pg_namespace n ON n.oid = c.relnamespace "
                        "WHERE c.relname = :n AND n.nspname = current_schema()"),
                {"n": _INDEX},
            ).scalar()
            if valid is False:
                op.execute(f'DROP INDEX "{_INDEX}"')  # a failed CONCURRENTLY build elsewhere
                existing = None
        if existing is not None:
            return

    op.execute(f'CREATE UNIQUE INDEX "{_INDEX}" ON users (LOWER(email))')


def downgrade() -> None:
    op.execute(f'DROP INDEX IF EXISTS "{_INDEX}"')
