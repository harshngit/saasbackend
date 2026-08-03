import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Lead(Base):
    __tablename__ = "leads"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    organization_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    lead_id: Mapped[str | None] = mapped_column(String(50), nullable=True, index=True)
    lead_source: Mapped[str | None] = mapped_column(String(50), nullable=True)
    customer_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("customers.id", ondelete="SET NULL"), nullable=True, index=True
    )
    mobile_number: Mapped[str | None] = mapped_column(String(20), nullable=True)
    assigned_salesperson_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    lead_status: Mapped[str | None] = mapped_column(String(30), default="new", nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, nullable=False
    )

    customer: Mapped["Customer | None"] = relationship(lazy="joined", primaryjoin="Lead.customer_id == Customer.id")  # noqa: F821
    assigned_salesperson: Mapped["User | None"] = relationship(lazy="joined", primaryjoin="Lead.assigned_salesperson_id == User.id")  # noqa: F821
