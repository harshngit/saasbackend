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

    # ----------------------------- Product Profile -----------------------------
    # Everything below is optional except `inventory_tracking`, which always
    # carries a concrete value. Toggles are NOT NULL with a default so a row
    # never has to be read as "unknown"; the rest are nullable.

    product_id: Mapped[str | None] = mapped_column(String(50), nullable=True, index=True)

    # 1. Basic information (name / description / product_type / brand / sku above)
    barcode: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    short_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    sub_category: Mapped[str | None] = mapped_column(String(150), nullable=True)
    manufacturer: Mapped[str | None] = mapped_column(String(150), nullable=True)
    model_number: Mapped[str | None] = mapped_column(String(100), nullable=True)

    # 2. Images & media — data: URLs (or external URLs); see POST /products/{id}/files/{field}
    product_video: Mapped[str | None] = mapped_column(Text, nullable=True)
    product_catalog_brochure: Mapped[str | None] = mapped_column(Text, nullable=True)
    product_manual: Mapped[str | None] = mapped_column(Text, nullable=True)

    # 3. Pricing (`price` above is the selling price)
    msrp_mrp: Mapped[float | None] = mapped_column(Float, nullable=True)
    wholesale_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    dealer_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    discount: Mapped[float | None] = mapped_column(Float, nullable=True)          # percent
    tax_inclusive_price: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    currency: Mapped[str | None] = mapped_column(String(10), nullable=True)

    # 4. Inventory (these overlap the Inventory module — this is the catalog's copy)
    inventory_tracking: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    opening_stock: Mapped[int | None] = mapped_column(Integer, nullable=True)
    minimum_stock_level: Mapped[int | None] = mapped_column(Integer, nullable=True)
    maximum_stock_level: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reorder_level: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reorder_quantity: Mapped[int | None] = mapped_column(Integer, nullable=True)
    bin_shelf_location: Mapped[str | None] = mapped_column(String(100), nullable=True)
    batch_tracking: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    serial_number_tracking: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    expiry_tracking: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # 5. Tax
    # HSN (goods) / SAC (services) code — printed per line item on the GST invoice.
    hsn_code: Mapped[str | None] = mapped_column(String(20), nullable=True, index=True)
    tax_category: Mapped[str | None] = mapped_column(String(100), nullable=True)
    tax_inclusive: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # 6. Purchase
    preferred_supplier_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("suppliers.id", ondelete="SET NULL"), nullable=True, index=True
    )
    supplier_product_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    lead_time: Mapped[str | None] = mapped_column(String(50), nullable=True)      # free text, e.g. "7 days"
    minimum_order_quantity: Mapped[int | None] = mapped_column(Integer, nullable=True)
    purchase_unit: Mapped[str | None] = mapped_column(String(50), nullable=True)

    # 7. Sales
    sales_unit: Mapped[str | None] = mapped_column(String(50), nullable=True)
    commission_eligible: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    commission: Mapped[float | None] = mapped_column(Float, nullable=True)        # percent
    default_discount: Mapped[float | None] = mapped_column(Float, nullable=True)  # percent

    # 8. Physical specifications
    weight: Mapped[float | None] = mapped_column(Float, nullable=True)
    length: Mapped[float | None] = mapped_column(Float, nullable=True)
    width: Mapped[float | None] = mapped_column(Float, nullable=True)
    height: Mapped[float | None] = mapped_column(Float, nullable=True)
    volume: Mapped[float | None] = mapped_column(Float, nullable=True)
    color: Mapped[str | None] = mapped_column(String(50), nullable=True)
    material: Mapped[str | None] = mapped_column(String(100), nullable=True)
    # Physical size band (S/M/L). Deliberately NOT called `size`: the variant
    # "size" (250ml / 500ml / 1L) is a different thing and stays on the variant.
    physical_size: Mapped[str | None] = mapped_column(String(50), nullable=True)

    # 9. Variants & attributes (the variants themselves are ProductVariant rows)
    has_variants: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # Which attribute types the variants vary by, e.g. ["Color", "Size"].
    variant_attributes: Mapped[list | None] = mapped_column(JSON, default=list, nullable=True)

    # 10. Digital product
    downloadable_product: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    download_file: Mapped[str | None] = mapped_column(Text, nullable=True)
    license_key_required: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    download_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # 11. Additional information
    warranty_period: Mapped[str | None] = mapped_column(String(50), nullable=True)  # e.g. "1 year"
    shelf_life: Mapped[str | None] = mapped_column(String(50), nullable=True)       # e.g. "6 months"
    country_of_origin: Mapped[str | None] = mapped_column(String(100), nullable=True)
    launch_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    end_of_life_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    product_tags: Mapped[list | None] = mapped_column(JSON, default=list, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    # 12. Documents
    product_datasheet: Mapped[str | None] = mapped_column(Text, nullable=True)
    compliance_certificate: Mapped[str | None] = mapped_column(Text, nullable=True)
    warranty_document: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Many-file slot, managed by /products/{id}/attachments (list of file dicts).
    other_attachments: Mapped[list | None] = mapped_column(JSON, default=list, nullable=True)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, nullable=False
    )

    variations: Mapped[list["ProductVariant"]] = relationship(
        back_populates="product", cascade="all, delete-orphan", lazy="joined"
    )
    # Loaded with the product so responses can resolve the ids into names without
    # the caller making a second call.
    category: Mapped["Category | None"] = relationship(lazy="joined")  # noqa: F821
    supplier: Mapped["Supplier | None"] = relationship(
        foreign_keys=[preferred_supplier_id], lazy="joined"
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
    sku: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    barcode: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    length: Mapped[float | None] = mapped_column(Float, nullable=True)
    width: Mapped[float | None] = mapped_column(Float, nullable=True)
    height: Mapped[float | None] = mapped_column(Float, nullable=True)
    weight: Mapped[float | None] = mapped_column(Float, nullable=True)
    price: Mapped[float] = mapped_column(Float, default=0, nullable=False)
    inventory: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    product: Mapped["Product"] = relationship(back_populates="variations")
