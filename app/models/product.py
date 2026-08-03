import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Product(Base):
    """A product in an organization's catalog. May have multiple trackable variants."""

    __tablename__ = "products"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    organization_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )

    name: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    price: Mapped[float] = mapped_column(Float, default=0, nullable=False)

    cover_image: Mapped[str | None] = mapped_column(Text, nullable=True)          # single image
    images: Mapped[list] = mapped_column(JSON, default=list, nullable=False)      # list of image URLs

    product_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    vendor: Mapped[str | None] = mapped_column(String(150), nullable=True)
    brand: Mapped[str | None] = mapped_column(String(150), nullable=True)
    sku: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)

    category_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("categories.id", ondelete="SET NULL"), nullable=True, index=True
    )

    # Used only when the product has no variants; otherwise stock is per-variant.
    total_inventory: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # Product Profile Fields
    product_id: Mapped[str | None] = mapped_column(String(50), nullable=True, index=True)
    inventory_tracking: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, nullable=False
    )

    variations: Mapped[list["ProductVariant"]] = relationship(
        back_populates="product", cascade="all, delete-orphan", lazy="joined"
    )

    @property
    def total_stock(self) -> int:
        """Effective stock: sum of variant inventory if variants exist, else total_inventory."""
        if self.variations:
            return sum(v.inventory or 0 for v in self.variations)
        return self.total_inventory or 0


class ProductVariant(Base):
    """An individually stock-trackable variant of a product (size/spec)."""

    __tablename__ = "product_variants"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    product_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("products.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(150), nullable=False)
    length: Mapped[float | None] = mapped_column(Float, nullable=True)
    width: Mapped[float | None] = mapped_column(Float, nullable=True)
    height: Mapped[float | None] = mapped_column(Float, nullable=True)
    weight: Mapped[float | None] = mapped_column(Float, nullable=True)
    price: Mapped[float] = mapped_column(Float, default=0, nullable=False)
    inventory: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    product: Mapped["Product"] = relationship(back_populates="variations")
