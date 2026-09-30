"""simplify organization_themes: drop deprecated design-system fields

Simplifies the organization_themes table to the canonical 9 columns:
- id
- organization_id
- custom_enabled
- mode
- background_image_url
- overlay_opacity
- primary_color
- created_at
- updated_at

Drops the deprecated design-system columns:
- theme_name
- logo_url
- secondary_color
- heading_font
- body_font
- card_style
- border_radius
- custom_config

Revision ID: k9f0a1b2c3d4
Revises: j8e9f0a1b2c3
Create Date: 2026-09-30 12:00:00.000000

"""
import logging
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'k9f0a1b2c3d4'
down_revision: Union[str, Sequence[str], None] = 'j8e9f0a1b2c3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

logger = logging.getLogger("alembic.runtime.migration")

_TABLE = "organization_themes"

_DROPPED_COLUMNS = [
    "theme_name",
    "logo_url",
    "secondary_color",
    "heading_font",
    "body_font",
    "card_style",
    "border_radius",
    "custom_config",
]


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if _TABLE not in inspector.get_table_names():
        logger.info("%s table does not exist -- skipping column drops", _TABLE)
        return

    existing_columns = {c["name"] for c in inspector.get_columns(_TABLE)}
    cols_to_drop = [col for col in _DROPPED_COLUMNS if col in existing_columns]

    if not cols_to_drop:
        logger.info("No deprecated columns found in %s -- skipping", _TABLE)
        return

    logger.info("Dropping deprecated columns from %s: %s", _TABLE, cols_to_drop)
    with op.batch_alter_table(_TABLE) as batch_op:
        for col_name in cols_to_drop:
            batch_op.drop_column(col_name)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if _TABLE not in inspector.get_table_names():
        return

    existing_columns = {c["name"] for c in inspector.get_columns(_TABLE)}

    with op.batch_alter_table(_TABLE) as batch_op:
        if "theme_name" not in existing_columns:
            batch_op.add_column(
                sa.Column("theme_name", sa.String(length=50), nullable=False, server_default="default")
            )
        if "logo_url" not in existing_columns:
            batch_op.add_column(
                sa.Column("logo_url", sa.Text(), nullable=True)
            )
        if "secondary_color" not in existing_columns:
            batch_op.add_column(
                sa.Column("secondary_color", sa.String(length=9), nullable=True)
            )
        if "heading_font" not in existing_columns:
            batch_op.add_column(
                sa.Column("heading_font", sa.String(length=100), nullable=False, server_default="DM Sans")
            )
        if "body_font" not in existing_columns:
            batch_op.add_column(
                sa.Column("body_font", sa.String(length=100), nullable=False, server_default="Open Sans")
            )
        if "card_style" not in existing_columns:
            batch_op.add_column(
                sa.Column("card_style", sa.String(length=30), nullable=False, server_default="solid")
            )
        if "border_radius" not in existing_columns:
            batch_op.add_column(
                sa.Column("border_radius", sa.String(length=20), nullable=False, server_default="12px")
            )
        if "custom_config" not in existing_columns:
            batch_op.add_column(
                sa.Column("custom_config", sa.JSON(), nullable=False, server_default="{}")
            )
