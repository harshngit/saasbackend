import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Vehicle(Base):
    """A van or truck the firm delivers with."""

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
    # active | inactive | maintenance
    status: Mapped[str] = mapped_column(String(30), default="active", nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, nullable=False
    )

    default_driver: Mapped["User | None"] = relationship(foreign_keys=[default_driver_id], lazy="joined")  # noqa: F821


class VehicleAssignmentHistory(Base):
    """Immutable audit history of driver assignments to a vehicle."""

    __tablename__ = "vehicle_assignment_history"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    organization_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    vehicle_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("vehicles.id", ondelete="CASCADE"), nullable=False, index=True
    )
    delivery_partner_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    assigned_by_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    assigned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)
    unassigned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    notes: Mapped[str | None] = mapped_column(String(500), nullable=True)

    vehicle: Mapped["Vehicle"] = relationship(foreign_keys=[vehicle_id], lazy="joined")
    delivery_partner: Mapped["User | None"] = relationship(foreign_keys=[delivery_partner_id], lazy="joined")  # noqa: F821
    assigned_by: Mapped["User | None"] = relationship(foreign_keys=[assigned_by_id], lazy="joined")  # noqa: F821
