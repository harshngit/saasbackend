import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


class OrganizationFeatureOverride(Base):
    """An organization-specific entitlement override allowing or blocking a specific feature key."""

    __tablename__ = "organization_feature_overrides"
    __table_args__ = (
        UniqueConstraint("organization_id", "entitlement_key", name="uq_org_feature_override"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    organization_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    entitlement_key: Mapped[str] = mapped_column(String(100), nullable=False, index=True)

    # Explicit ALLOW or BLOCK effect.
    # Stored as effect string ("ALLOW", "BLOCK") and is_allowed boolean for fast querying.
    effect: Mapped[str] = mapped_column(String(10), default="ALLOW", nullable=False)
    is_allowed: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_by_user_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, nullable=False
    )

    organization: Mapped["Organization"] = relationship(  # noqa: F821
        "Organization", back_populates="feature_overrides"
    )
    created_by: Mapped["User | None"] = relationship(  # noqa: F821
        "User", foreign_keys=[created_by_user_id]
    )

    @property
    def is_active(self) -> bool:
        """Returns True if the override has not expired."""
        if self.expires_at is None:
            return True
        exp = self.expires_at
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        return exp > datetime.now(timezone.utc)


class OrganizationLimitOverride(Base):
    """An organization-specific limit override for numeric plan limits (e.g., max_users, max_warehouses)."""

    __tablename__ = "organization_limit_overrides"
    __table_args__ = (
        UniqueConstraint("organization_id", "limit_key", name="uq_org_limit_override"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    organization_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    limit_key: Mapped[str] = mapped_column(String(100), nullable=False, index=True)

    # Value: None = unlimited, or integer value >= 0
    value: Mapped[int | None] = mapped_column(Integer, nullable=True)

    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_by_user_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, nullable=False
    )

    organization: Mapped["Organization"] = relationship(  # noqa: F821
        "Organization", back_populates="limit_overrides"
    )
    created_by: Mapped["User | None"] = relationship(  # noqa: F821
        "User", foreign_keys=[created_by_user_id]
    )

    @property
    def is_active(self) -> bool:
        """Returns True if the override has not expired."""
        if self.expires_at is None:
            return True
        exp = self.expires_at
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        return exp > datetime.now(timezone.utc)
