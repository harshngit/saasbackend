import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base

PURCHASE_STATUSES = {"pending", "approved", "cancelled"}
PAYMENT_STATUSES = {"unpaid", "partial", "paid"}


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


class PurchaseInvoice(Base):
    __tablename__ = "purchase_invoices"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    organization_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    invoice_number: Mapped[str] = mapped_column(String(60), nullable=False, index=True)  # supplier's invoice no
    supplier_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("suppliers.id", ondelete="SET NULL"), nullable=True, index=True
    )
    invoice_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)

    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False, index=True)
    payment_status: Mapped[str] = mapped_column(String(20), default="unpaid", nullable=False)

    subtotal: Mapped[float] = mapped_column(Float, default=0, nullable=False)
    discount: Mapped[float] = mapped_column(Float, default=0, nullable=False)
    tax: Mapped[float] = mapped_column(Float, default=0, nullable=False)   # GST
    total: Mapped[float] = mapped_column(Float, default=0, nullable=False)
    amount_paid: Mapped[float] = mapped_column(Float, default=0, nullable=False)

    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    attachment_url: Mapped[str | None] = mapped_column(Text, nullable=True)  # scanned invoice
    stock_added: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_by: Mapped[str | None] = mapped_column(String(36), nullable=True)

    # Extended Purchase Profile Fields
    # 1. Basic Information
    purchase_id: Mapped[str | None] = mapped_column(String(50), nullable=True, index=True)
    purchase_number: Mapped[str | None] = mapped_column(String(50), nullable=True, index=True)
    purchase_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    purchase_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    financial_year: Mapped[str | None] = mapped_column(String(20), nullable=True)
    purchase_status: Mapped[str | None] = mapped_column(String(30), nullable=True)
    reference_number: Mapped[str | None] = mapped_column(String(100), nullable=True)

    # 2. Supplier Details
    contact_person: Mapped[str | None] = mapped_column(String(150), nullable=True)
    mobile_number: Mapped[str | None] = mapped_column(String(30), nullable=True)
    email_address: Mapped[str | None] = mapped_column(String(255), nullable=True)
    payee_gstin: Mapped[str | None] = mapped_column(String(50), nullable=True)
    billing_address: Mapped[str | None] = mapped_column(Text, nullable=True)
    shipping_address: Mapped[str | None] = mapped_column(Text, nullable=True)

    # 4. Purchase Totals (Additional Charges)
    freight_charges: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    packing_charges: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    insurance_charges: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    other_charges: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    round_off: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)

    # 5. Goods Receipt
    grn_number: Mapped[str | None] = mapped_column(String(50), nullable=True, index=True)
    received_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    warehouse_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    received_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    receiving_status: Mapped[str | None] = mapped_column(String(30), nullable=True)

    # 6. Payment Details
    payment_method: Mapped[str | None] = mapped_column(String(30), nullable=True)
    payment_terms: Mapped[str | None] = mapped_column(String(50), nullable=True)
    due_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    payment_reference: Mapped[str | None] = mapped_column(String(150), nullable=True)

    # 7. Accounting
    purchase_account_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    tax_category: Mapped[str | None] = mapped_column(String(50), nullable=True)
    cost_center_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    project_id: Mapped[str | None] = mapped_column(String(36), nullable=True)

    # 8. Approval
    requested_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    approved_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    approval_status: Mapped[str | None] = mapped_column(String(30), nullable=True)
    approval_remarks: Mapped[str | None] = mapped_column(Text, nullable=True)

    # 9. Documents
    supplier_quotation_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    purchase_order_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    supplier_invoice_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    delivery_challan_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    supporting_documents: Mapped[list[dict] | None] = mapped_column(JSON, default=list, nullable=True)

    # 10. Additional Info
    terms_and_conditions: Mapped[str | None] = mapped_column(Text, nullable=True)
    internal_remarks: Mapped[str | None] = mapped_column(Text, nullable=True)
    tags: Mapped[list[str] | None] = mapped_column(JSON, default=list, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, nullable=False
    )

    items: Mapped[list["PurchaseInvoiceItem"]] = relationship(
        back_populates="invoice", cascade="all, delete-orphan", lazy="joined"
    )
    supplier: Mapped["Supplier | None"] = relationship(lazy="joined")  # noqa: F821

    @property
    def outstanding_balance(self) -> float:
        return round(max(self.total - (self.amount_paid or 0.0), 0.0), 2)


class PurchaseInvoiceItem(Base):
    __tablename__ = "purchase_invoice_items"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    invoice_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("purchase_invoices.id", ondelete="CASCADE"), nullable=False, index=True
    )
    product_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("products.id", ondelete="SET NULL"), nullable=True
    )
    variant_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("product_variants.id", ondelete="SET NULL"), nullable=True
    )
    product_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    barcode: Mapped[str | None] = mapped_column(String(100), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    warehouse_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("warehouses.id", ondelete="SET NULL"), nullable=True
    )
    product_name: Mapped[str] = mapped_column(String(200), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    purchase_price: Mapped[float] = mapped_column(Float, default=0, nullable=False)
    discount_percent: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    discount: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    tax_rate: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    tax: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    line_total: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    unit_of_measure_uom: Mapped[str | None] = mapped_column(String(30), nullable=True)
    batch_number: Mapped[str | None] = mapped_column(String(100), nullable=True)
    serial_numbers: Mapped[list[str] | None] = mapped_column(JSON, default=list, nullable=True)
    expiry_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    invoice: Mapped["PurchaseInvoice"] = relationship(back_populates="items")
