import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, DateTime, Float, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Plan(Base):
    """A subscription plan in the platform catalog (managed by the Super Admin)."""

    __tablename__ = "plans"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(100), nullable=False)

    price_monthly: Mapped[float] = mapped_column(Float, default=0, nullable=False)
    price_yearly: Mapped[float] = mapped_column(Float, default=0, nullable=False)
    original_price_monthly: Mapped[float | None] = mapped_column(Float, nullable=True)
    original_price_yearly: Mapped[float | None] = mapped_column(Float, nullable=True)

    max_users: Mapped[int | None] = mapped_column(Integer, nullable=True)   # None = unlimited
    max_orders: Mapped[int | None] = mapped_column(Integer, nullable=True)  # None = unlimited

    # List of feature bullet strings shown on the plan card. JSON works on SQLite + Postgres.
    features: Mapped[list] = mapped_column(JSON, default=list, nullable=False)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # The plan new trial organizations are placed on by default (usually the free plan).
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, nullable=False
    )
