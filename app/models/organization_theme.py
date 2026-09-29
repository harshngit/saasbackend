import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


class OrganizationTheme(Base):
    """One organization's CRM appearance customization — strictly one row per
    organization (see the unique index on organization_id below).

    Deliberately a separate table, not a JSON column on Organization: every
    field here is a first-class, independently-validated setting (colors,
    fonts, mode), unlike sales_workflow_settings/invoice_template_settings,
    which are free-form config blocks. `custom_config` is the escape hatch
    for anything that doesn't warrant its own column.

    A row existing at all is not what turns customization on — `custom_enabled`
    is. The frontend applies the default CRM look whenever `custom_enabled` is
    false, whether or not a row exists yet: GET /organization/theme returns
    the full default configuration even before any row has ever been written
    (see app.services.theme_service.effective_theme), so nothing has to
    create a row just to read the defaults.
    """

    __tablename__ = "organization_themes"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    organization_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False, unique=True, index=True,
    )

    custom_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    theme_name: Mapped[str] = mapped_column(String(50), default="default", nullable=False)
    mode: Mapped[str] = mapped_column(String(20), default="light", nullable=False)

    # Theme-specific media. Deliberately separate from Organization.logo_url
    # (used by invoices/company branding/documents) — a theme logo change
    # must never change what appears on an invoice, and vice versa.
    background_image_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    logo_url: Mapped[str | None] = mapped_column(Text, nullable=True)

    # 6-digit hex only (#rrggbb) — validated at the schema layer, not here;
    # the column itself just needs to hold the string.
    primary_color: Mapped[str | None] = mapped_column(String(9), nullable=True)
    secondary_color: Mapped[str | None] = mapped_column(String(9), nullable=True)

    heading_font: Mapped[str] = mapped_column(String(100), default="DM Sans", nullable=False)
    body_font: Mapped[str] = mapped_column(String(100), default="Open Sans", nullable=False)

    card_style: Mapped[str] = mapped_column(String(30), default="solid", nullable=False)
    # A CSS length, not a bare number (e.g. "12px") — validated at the schema
    # layer against a small allowed set, same reasoning as card_style.
    border_radius: Mapped[str] = mapped_column(String(20), default="12px", nullable=False)
    # 0.0-1.0 — how strongly the background image is dimmed under UI content.
    overlay_opacity: Mapped[float] = mapped_column(Float, default=0.5, nullable=False)

    # Arbitrary additional theme configuration that doesn't warrant its own
    # column. Never a substitute for validating the first-class fields above.
    custom_config: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, nullable=False
    )

    organization: Mapped["Organization"] = relationship(back_populates="theme")  # noqa: F821
