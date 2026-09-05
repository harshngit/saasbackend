"""backfill: remove sales_orders permission from existing Delivery Partner roles

Finalized business rule: Delivery Partners are operational users and must
never be able to create Sales Orders of any kind — including the legacy
"Van Sales" / source=delivery_vehicle path this role previously had a
create-only grant for (see app.core.permissions.default_role_matrices,
which no longer grants it to newly-seeded orgs as of this revision).

That default-matrix change only affects orgs seeded *after* this change
ships — every organization's actual "Delivery Partner" Role row already has
its permissions JSON persisted from whenever it was seeded, and
role_service.seed_default_roles() only ever fills in *missing* settings, it
never overwrites an existing permissions matrix. Without this backfill,
every already-existing organization's Delivery Partners would keep their
sales_orders:create grant indefinitely — a real, exploitable gap, not just
a cosmetic one, given the explicit "must return 403, do not rely on the
frontend" requirement this closes.

Pure data correction, no schema change: for every Role row where
name == 'Delivery Partner' and is_default is true (the seeded default,
never a firm's own custom role of the same name they may have deliberately
configured differently), the 'sales_orders' key is removed from its
`permissions` JSON if present. A role an Admin has already customized away
from the default (is_default now false, e.g. after editing it once) is left
alone — this migration only touches rows that are still exactly the
platform-seeded default.

Idempotent: does nothing to a row whose permissions no longer contain
'sales_orders'. Safe to re-run.

Revision ID: 75fa492c339a
Revises: 9699ffe936d6
Create Date: 2026-09-04 20:00:00.000000

"""
import logging
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '75fa492c339a'
down_revision: Union[str, Sequence[str], None] = '9699ffe936d6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

logger = logging.getLogger("alembic.runtime.migration")


def _roles_table() -> sa.Table:
    meta = sa.MetaData()
    return sa.Table(
        "roles",
        meta,
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("name", sa.String(length=100)),
        sa.Column("is_default", sa.Boolean),
        sa.Column("permissions", sa.JSON),
    )


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "roles" not in inspector.get_table_names():
        logger.info("roles table does not exist yet -- skipping")
        return

    roles = _roles_table()
    rows = bind.execute(
        sa.select(roles.c.id, roles.c.permissions).where(
            roles.c.name == "Delivery Partner", roles.c.is_default.is_(True)
        )
    ).fetchall()

    updated = 0
    for row in rows:
        perms = row.permissions or {}
        if "sales_orders" not in perms:
            continue
        new_perms = {k: v for k, v in perms.items() if k != "sales_orders"}
        bind.execute(roles.update().where(roles.c.id == row.id).values(permissions=new_perms))
        updated += 1

    if updated:
        logger.warning(
            "Removed 'sales_orders' permission from %d existing default 'Delivery Partner' role(s)", updated
        )
    else:
        logger.info("No default 'Delivery Partner' role had a 'sales_orders' permission to remove")


def downgrade() -> None:
    """Deliberately a no-op: re-granting sales_orders:create to Delivery
    Partner roles would silently re-open the exact gap this migration closes.
    If this ever needs reverting, do it explicitly via the Roles screen for
    the specific organizations that need it, not a blanket migration.
    """
