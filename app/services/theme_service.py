"""Organization theme: read/write logic for app.models.organization_theme.OrganizationTheme.

Mirrors the shape of app.core.workflow's sales_settings()/invoice_settings()
(defaults filled in, nothing forces a row to exist before GET), but backed by
a real one-row-per-organization table (organization_themes) rather than a
JSON column — the table shape was an explicit product requirement, distinct
from the JSON-column pattern those two other settings use.

A row existing is NOT what turns customization on; `custom_enabled` is.
GET returns a complete, valid theme (the defaults below) whether or not a
row has ever been written for the organization, so the frontend never has
to special-case "no theme yet".
"""

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.organization import Organization
from app.models.organization_theme import OrganizationTheme

# The exact defaults a brand-new (or never-customized) organization sees.
# Kept as a single source of truth so the schema layer, the get-or-create
# path, and tests all agree on the same values.
THEME_DEFAULTS: dict[str, object] = {
    "custom_enabled": False,
    "theme_name": "default",
    "mode": "light",
    "background_image_url": None,
    "logo_url": None,
    "primary_color": None,
    "secondary_color": None,
    "heading_font": "DM Sans",
    "body_font": "Open Sans",
    "card_style": "solid",
    "border_radius": "12px",
    "overlay_opacity": 0.5,
    "custom_config": {},
}

THEME_MODES = ("light", "dark")
THEME_NAMES = ("default", "professional", "dark", "custom")
CARD_STYLES = ("solid", "glass", "outline", "minimal")
# A small, explicit allow-list rather than an open-ended CSS-length regex —
# matches this project's existing preference for controlled vocabularies
# over free-form strings wherever the value ends up in generated CSS.
BORDER_RADIUS_VALUES = ("0px", "4px", "8px", "12px", "16px", "24px", "9999px")


def get_theme(db: Session, org: Organization) -> OrganizationTheme | None:
    """The organization's theme row, or None if it has never been customized."""
    return db.query(OrganizationTheme).filter(OrganizationTheme.organization_id == org.id).first()


def get_or_create_theme(db: Session, org: Organization) -> OrganizationTheme:
    """The organization's theme row, creating one on defaults if this is the
    first write. Safe under concurrent requests: if two requests race to
    create the first row, the UNIQUE constraint on organization_id rejects
    the loser, which then simply re-reads the winner's row instead of
    erroring — never two rows for the same organization.
    """
    existing = get_theme(db, org)
    if existing is not None:
        return existing

    theme = OrganizationTheme(organization_id=org.id)
    db.add(theme)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        existing = get_theme(db, org)
        if existing is not None:
            return existing
        raise  # a real error, not a concurrent-create race
    return theme


def theme_dict(theme: OrganizationTheme | None) -> dict[str, object]:
    """The effective, fully-populated theme configuration as a plain dict —
    THEME_DEFAULTS for any organization that has never been customized, with
    the stored row's values layered on top of it for one that has."""
    if theme is None:
        return dict(THEME_DEFAULTS)
    return {
        "custom_enabled": theme.custom_enabled,
        "theme_name": theme.theme_name,
        "mode": theme.mode,
        "background_image_url": theme.background_image_url,
        "logo_url": theme.logo_url,
        "primary_color": theme.primary_color,
        "secondary_color": theme.secondary_color,
        "heading_font": theme.heading_font,
        "body_font": theme.body_font,
        "card_style": theme.card_style,
        "border_radius": theme.border_radius,
        "overlay_opacity": theme.overlay_opacity,
        "custom_config": dict(theme.custom_config or {}),
    }
