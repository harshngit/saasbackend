import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


class SupplierBrand(Base):
    """Many-to-many relationship linking a Supplier to a Brand within an organization."""

    __tablename__ = "supplier_brands"
    __table_args__ = (
        UniqueConstraint("supplier_id", "brand_id", name="uq_supplier_brand"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    organization_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    supplier_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("suppliers.id", ondelete="CASCADE"), nullable=False, index=True
    )
    brand_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("brands.id", ondelete="CASCADE"), nullable=False, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)

    supplier: Mapped["Supplier"] = relationship(lazy="joined")  # noqa: F821
    brand: Mapped["Brand"] = relationship(lazy="joined")  # noqa: F821


class BrandCategory(Base):
    """Many-to-many relationship linking a Brand to a Category within an organization."""

    __tablename__ = "brand_categories"
    __table_args__ = (
        UniqueConstraint("brand_id", "category_id", name="uq_brand_category"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    organization_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    brand_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("brands.id", ondelete="CASCADE"), nullable=False, index=True
    )
    category_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("categories.id", ondelete="CASCADE"), nullable=False, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)

    brand: Mapped["Brand"] = relationship(lazy="joined")  # noqa: F821
    category: Mapped["Category"] = relationship(lazy="joined")  # noqa: F821
