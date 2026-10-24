import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


def name_key(name: str) -> str:
    """What two variant names are compared by: `Shoe` and ` shoe ` are the same variant."""
    return name.strip().lower()


class SubcategoryVariant(Base):
    """One option a sub category offers as a product's Product Type (e.g. Casual Footwear
    -> Shoe / Cheppal / Sandal). Category master data -- unrelated to ProductVariant,
    which is a sellable variant of one product with its own SKU, price and stock.

    Never hard-deleted through the API: deactivating one keeps every product that already
    points at it (and the name it shows), while new products can no longer choose it.
    """

    __tablename__ = "subcategory_variants"
    __table_args__ = (
        UniqueConstraint("organization_id", "subcategory_id", "name_key", name="uq_subcategory_variant_name"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    organization_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # SET NULL, not CASCADE: deleting a sub category must not take the Product Type that
    # existing products point at (and its name) with it.
    subcategory_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("categories.id", ondelete="SET NULL"), nullable=True, index=True
    )
    name: Mapped[str] = mapped_column(String(150), nullable=False)
    name_key: Mapped[str] = mapped_column(String(150), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, nullable=False
    )
