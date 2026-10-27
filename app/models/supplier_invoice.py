import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, Float, ForeignKey, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, object_session, relationship

from app.core.database import Base

SUPPLIER_INVOICE_STATUSES = {"draft", "recorded", "cancelled"}
VERIFICATION_STATUSES = {"pending", "matched", "mismatched"}
PAYMENT_STATUSES = {"unpaid", "partially_paid", "paid"}


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


class SupplierInvoice(Base):
    __tablename__ = "supplier_invoices"
    __table_args__ = (
        UniqueConstraint("organization_id", "supplier_id", "supplier_invoice_number", name="uq_org_supplier_invoice_num"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    organization_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    supplier_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("suppliers.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    purchase_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("purchase_invoices.id", ondelete="RESTRICT"), nullable=False, index=True
    )

    supplier_invoice_number: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    supplier_invoice_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)
    due_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    status: Mapped[str] = mapped_column(String(20), default="draft", nullable=False, index=True)
    verification_status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False, index=True)
    payment_status: Mapped[str] = mapped_column(String(20), default="unpaid", nullable=False, index=True)

    subtotal: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    tax_amount: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    discount_amount: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    grand_total: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)

    amount_paid: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    return_amount: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)

    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    attachment_url: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    recorded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    recorded_by: Mapped[str | None] = mapped_column(String(36), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, nullable=False
    )

    items: Mapped[list["SupplierInvoiceItem"]] = relationship(
        back_populates="supplier_invoice", cascade="all, delete-orphan", lazy="joined"
    )
    supplier: Mapped["Supplier"] = relationship(foreign_keys=[supplier_id], lazy="joined")  # noqa: F821
    purchase: Mapped["PurchaseInvoice"] = relationship(foreign_keys=[purchase_id], lazy="joined")  # noqa: F821

    @property
    def outstanding_amount(self) -> float:
        return round(max(self.grand_total - (self.return_amount or 0.0) - (self.amount_paid or 0.0), 0.0), 2)

    # Read from the linked records, so the API can show who and what this invoice is for.
    @property
    def supplier_name(self) -> str | None:
        return self.supplier.name if self.supplier is not None else None

    @property
    def purchase_number(self) -> str | None:
        if self.purchase is None:
            return None
        return self.purchase.purchase_number or self.purchase.purchase_id

    @property
    def invoice_date(self) -> datetime:
        """Same value as supplier_invoice_date (the supplier's invoice date)."""
        return self.supplier_invoice_date

    # The component checks behind verification_status (3-way match: PO price, GRN quantity).
    @property
    def quantity_match(self) -> bool | None:
        checks = [item.quantity_match for item in self.items]
        return None if not checks or None in checks else all(checks)

    @property
    def price_match(self) -> bool | None:
        checks = [item.price_match for item in self.items]
        return None if not checks or None in checks else all(checks)


class SupplierInvoiceItem(Base):
    __tablename__ = "supplier_invoice_items"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    supplier_invoice_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("supplier_invoices.id", ondelete="CASCADE"), nullable=False, index=True
    )
    purchase_item_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("purchase_invoice_items.id", ondelete="CASCADE"), nullable=False, index=True
    )
    product_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("products.id", ondelete="SET NULL"), nullable=True, index=True
    )
    variant_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("product_variants.id", ondelete="SET NULL"), nullable=True, index=True
    )

    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    billed_qty: Mapped[int] = mapped_column(Integer, nullable=False)
    unit_price: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    tax_rate: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    tax_amount: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    discount_amount: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    line_total: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)

    supplier_invoice: Mapped["SupplierInvoice"] = relationship(back_populates="items")
    purchase_item: Mapped["PurchaseInvoiceItem"] = relationship(lazy="joined")  # noqa: F821
    product: Mapped["Product | None"] = relationship(lazy="joined")  # noqa: F821

    # Read from the linked purchase item (and its GRN roll-up) -- never copied or guessed.
    @property
    def product_name(self) -> str | None:
        if self.purchase_item is not None and self.purchase_item.product_name:
            return self.purchase_item.product_name
        return self.product.name if self.product is not None else self.description

    @property
    def sku(self) -> str | None:
        if self.purchase_item is not None and self.purchase_item.product_code:
            return self.purchase_item.product_code
        return self.product.sku if self.product is not None else None

    @property
    def ordered_qty(self) -> int | None:
        return self.purchase_item.quantity if self.purchase_item is not None else None

    @property
    def received_qty(self) -> int | None:
        """Cumulative accepted quantity from confirmed GRNs for the purchase line."""
        return (self.purchase_item.received_qty or 0) if self.purchase_item is not None else None

    @property
    def purchase_price(self) -> float | None:
        return self.purchase_item.purchase_price if self.purchase_item is not None else None

    @property
    def billed_on_other_invoices(self) -> int:
        """Quantity of this purchase line billed on the purchase's OTHER recorded / disputed
        invoices. Pre-filled for whole pages by supplier_invoice_service.prefetch_verification."""
        cached = self.__dict__.get("_billed_elsewhere")
        if cached is not None:
            return cached
        from app.services.supplier_invoice_service import billed_elsewhere_map

        return billed_elsewhere_map(object_session(self), [self]).get(self.id, 0)

    @property
    def available_qty(self) -> int | None:
        """Confirmed received quantity not yet billed on another invoice."""
        if self.received_qty is None:
            return None
        return max(self.received_qty - self.billed_on_other_invoices, 0)

    @property
    def quantity_match(self) -> bool | None:
        return None if self.available_qty is None else self.billed_qty <= self.available_qty

    @property
    def price_match(self) -> bool | None:
        if self.purchase_price is None:
            return None
        return abs((self.unit_price or 0.0) - self.purchase_price) < 0.01
