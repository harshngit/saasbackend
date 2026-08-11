import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Role(Base):
    """An org-scoped role with a per-module permission matrix (stored as JSON).

    Every organization gets 3 default roles auto-seeded (Sales Officer,
    Delivery Partner, Accountant) and can add its own custom roles.
    """

    __tablename__ = "roles"
    __table_args__ = (UniqueConstraint("organization_id", "name", name="uq_role_org_name"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    organization_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # Which dashboard the frontend opens for this role after login
    # (sales / delivery / accounts / admin / …). Free text, not an enum, so a firm
    # can name its own workspaces.
    workspace: Mapped[str | None] = mapped_column(String(50), nullable=True)
    description: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # "all"  -> the role sees every record in the firm (back-office roles)
    # "own"  -> list endpoints return only records assigned to / created by the
    #           logged-in user (field roles). See app/core/scoping.py.
    data_scope: Mapped[str | None] = mapped_column(String(10), nullable=True)

    # { "<module>": { "view": bool, "create": bool, ... }, ... } — deny-by-default.
    permissions: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, nullable=False
    )
