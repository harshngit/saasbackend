import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Vehicle(Base):
    """A van or truck the firm delivers with.

    Minimal on purpose: a delivery needs to name which vehicle went out, and the
    Staff Detail page needs a registration number to show. Anything more (servicing,
    documents, fuel) belongs to a vehicle-management module, not here.
    """

    __tablename__ = "vehicles"
    __table_args__ = (
        UniqueConstraint("organization_id", "vehicle_number", name="uq_vehicle_org_number"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    organization_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    vehicle_number: Mapped[str] = mapped_column(String(30), nullable=False)
    vehicle_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    capacity_kg: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Who usually drives it. A delivery can still name anybody.
    default_driver_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, nullable=False
    )
