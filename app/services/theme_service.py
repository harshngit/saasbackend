"""Organization theme: read/write logic for app.models.organization_theme.OrganizationTheme.

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
THEME_DEFAULTS: dict[str, object] = {
    "custom_enabled": False,
    "mode": "light",
    "primary_color": None,
    "background_image_url": None,
    "overlay_opacity": 0.45,
}

THEME_MODES = ("light", "dark")


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
    """The effective theme configuration as a plain dict —
    THEME_DEFAULTS for any organization that has never been customized, with
    the stored row's values layered on top of it for one that has."""
    if theme is None:
        return dict(THEME_DEFAULTS)
    return {
        "custom_enabled": theme.custom_enabled,
        "mode": theme.mode,
        "primary_color": theme.primary_color,
        "background_image_url": theme.background_image_url,
        "overlay_opacity": theme.overlay_opacity,
    }
