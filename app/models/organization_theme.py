import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


class OrganizationTheme(Base):
    """One organization's CRM appearance customization — strictly one row per
    organization (see the unique index on organization_id below).

    A row existing at all is not what turns customization on — `custom_enabled`
    is. The frontend applies the default CRM look whenever `custom_enabled` is
    false, whether or not a row exists yet: GET /organization/theme returns
    the full default configuration even before any row has ever been written.
    """

    __tablename__ = "organization_themes"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    organization_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )

    custom_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    mode: Mapped[str] = mapped_column(String(20), default="light", nullable=False)

    # Relative file reference (/files/{id}) — host-independent in DB.
    background_image_url: Mapped[str | None] = mapped_column(Text, nullable=True)

    # 0.0-0.9 — how strongly the background image is dimmed under UI content.
    overlay_opacity: Mapped[float] = mapped_column(Float, default=0.45, nullable=False)

    # 6-digit hex (#rrggbb) or None. Validated at the schema layer.
    primary_color: Mapped[str | None] = mapped_column(String(9), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, nullable=False
    )

    organization: Mapped["Organization"] = relationship(back_populates="theme")  # noqa: F821
