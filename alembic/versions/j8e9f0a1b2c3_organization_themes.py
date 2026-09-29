"""organization_themes: one-row-per-organization appearance customization

Creates the organization_themes table. Schema-only — no existing data is
touched, and no row is created for any organization here; a row only comes
into existence on the first PATCH/upload to the theme endpoints (see
app.services.theme_service.get_or_create_theme). Every organization,
including ones that predate this migration, already reads as the documented
defaults (custom_enabled=false) via app.services.theme_service.theme_dict
when no row exists yet.

Revision ID: j8e9f0a1b2c3
Revises: i7d8e9f0a1b2
Create Date: 2026-09-30 00:00:00.000000

"""
import logging
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'j8e9f0a1b2c3'
down_revision: Union[str, Sequence[str], None] = 'i7d8e9f0a1b2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

logger = logging.getLogger("alembic.runtime.migration")

_TABLE = "organization_themes"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if _TABLE in inspector.get_table_names():
        logger.info("%s already exists -- skipping creation", _TABLE)
        return

    logger.info("Creating %s", _TABLE)
    op.create_table(
        _TABLE,
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "organization_id", sa.String(length=36),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("custom_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("theme_name", sa.String(length=50), nullable=False, server_default="default"),
        sa.Column("mode", sa.String(length=20), nullable=False, server_default="light"),
        sa.Column("background_image_url", sa.Text(), nullable=True),
        sa.Column("logo_url", sa.Text(), nullable=True),
        sa.Column("primary_color", sa.String(length=9), nullable=True),
        sa.Column("secondary_color", sa.String(length=9), nullable=True),
        sa.Column("heading_font", sa.String(length=100), nullable=False, server_default="DM Sans"),
        sa.Column("body_font", sa.String(length=100), nullable=False, server_default="Open Sans"),
        sa.Column("card_style", sa.String(length=30), nullable=False, server_default="solid"),
        sa.Column("border_radius", sa.String(length=20), nullable=False, server_default="12px"),
        sa.Column("overlay_opacity", sa.Float(), nullable=False, server_default="0.5"),
        sa.Column("custom_config", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )

    # A single unique index enforces one theme per organization at the
    # database level, not just in application code
    # (app.services.theme_service.get_or_create_theme relies on this to
    # resolve a concurrent-create race safely) — matches the model's own
    # organization_id column, which declares unique=True, index=True.
    existing_indexes = {ix["name"] for ix in sa.inspect(bind).get_indexes(_TABLE)}
    if "ix_organization_themes_organization_id" not in existing_indexes:
        op.create_index(
            "ix_organization_themes_organization_id", _TABLE, ["organization_id"], unique=True,
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if _TABLE in inspector.get_table_names():
        op.drop_table(_TABLE)
