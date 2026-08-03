import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


class VehicleLoading(Base):
    __tablename__ = "vehicle_loadings"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    organization_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    delivery_partner_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    date: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="active", nullable=False, index=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, nullable=False
    )

    items: Mapped[list["VehicleLoadingItem"]] = relationship(
        back_populates="loading", cascade="all, delete-orphan", lazy="joined"
    )
    delivery_partner: Mapped["User"] = relationship(foreign_keys=[delivery_partner_id], lazy="joined")  # noqa: F821


class VehicleLoadingItem(Base):
    __tablename__ = "vehicle_loading_items"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    loading_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("vehicle_loadings.id", ondelete="CASCADE"), nullable=False, index=True
    )
    product_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("products.id", ondelete="SET NULL"), nullable=True
    )
    variant_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("product_variants.id", ondelete="SET NULL"), nullable=True
    )
    product_name: Mapped[str] = mapped_column(String(200), nullable=False)
    loaded_qty: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    extra_qty: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    returned_qty: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    delivered_qty: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    loading: Mapped["VehicleLoading"] = relationship(back_populates="items")
