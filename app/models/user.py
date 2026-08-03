import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.models.enums import UserRole


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


class User(Base):
    """A platform or firm user. Email is unique per organization.

    SUPER_ADMIN rows have organization_id = NULL (platform-level, no tenant).
    """

    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("organization_id", "email", name="uq_user_org_email"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    organization_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=True, index=True
    )

    name: Mapped[str] = mapped_column(String(150), nullable=False)
    email: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    # Unique platform-wide login/identifier for staff (in addition to email). Nullable
    # so existing users / admins created before this field remain valid.
    username: Mapped[str | None] = mapped_column(String(50), nullable=True, unique=True, index=True)
    phone: Mapped[str | None] = mapped_column(String(20), nullable=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)

    # Top-level role for routing/access control (super_admin / admin / staff).
    system_role: Mapped[str | None] = mapped_column(String(20), nullable=True, index=True)
    # Staff's detailed org-scoped role (permission matrix). Null for admin/super_admin.
    role_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("roles.id"), nullable=True, index=True
    )
    # Legacy fixed role enum — kept (nullable) for backward-compat in responses.
    role: Mapped[UserRole | None] = mapped_column(Enum(UserRole), nullable=True)

    # Employee Profile Fields
    employee_id: Mapped[str | None] = mapped_column(String(50), nullable=True, index=True)
    first_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    last_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    designation: Mapped[str | None] = mapped_column(String(100), nullable=True)
    employment_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    date_of_joining: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    employee_status: Mapped[str | None] = mapped_column(String(30), nullable=True)
    identify_proofs: Mapped[str | None] = mapped_column(String(100), nullable=True)
    status: Mapped[str | None] = mapped_column(String(20), nullable=True)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)

    organization: Mapped["Organization | None"] = relationship(back_populates="users")  # noqa: F821
    role_detail: Mapped["Role | None"] = relationship(foreign_keys=[role_id], lazy="joined")  # noqa: F821

    @property
    def effective_system_role(self) -> str:
        """system_role if set, otherwise derived from the legacy role (transition safety)."""
        if self.system_role:
            return self.system_role
        from app.models.enums import system_role_for

        return system_role_for(self.role)
