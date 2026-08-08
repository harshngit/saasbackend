import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Customer(Base):
    """A customer belonging to one organization (tenant-scoped CRM record)."""

    __tablename__ = "customers"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    organization_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )

    name: Mapped[str] = mapped_column(String(150), nullable=False)          # contact person
    business_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(20), nullable=True, index=True)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    gst_number: Mapped[str | None] = mapped_column(String(20), nullable=True)

    billing_address: Mapped[str | None] = mapped_column(Text, nullable=True)
    delivery_address: Mapped[str | None] = mapped_column(Text, nullable=True)

    assigned_sales_officer_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )

    credit_limit: Mapped[float] = mapped_column(Float, default=0, nullable=False)

    # Receivables (kept in sync). outstanding = opening + billed - received.
    opening_balance: Mapped[float] = mapped_column(Float, default=0, nullable=False)
    total_billed: Mapped[float] = mapped_column(Float, default=0, nullable=False)   # from confirmed orders
    total_received: Mapped[float] = mapped_column(Float, default=0, nullable=False)  # from customer payments
    outstanding_balance: Mapped[float] = mapped_column(Float, default=0, nullable=False)  # maintained

    category: Mapped[str | None] = mapped_column(String(100), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Customer Profile Fields
    customer_id: Mapped[str | None] = mapped_column(String(50), nullable=True, index=True)
    customer_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    primary_contact_person: Mapped[str | None] = mapped_column(String(150), nullable=True)
    maps_latitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    maps_longitude: Mapped[float | None] = mapped_column(Float, nullable=True)

    # ------------------------- Full Customer Profile -------------------------
    # Grouped the way the API exposes them (basic / contact / address / tax /
    # payment / CRM / social / additional / preferences). The `documents`
    # section is not stored here — it is derived from customer_documents.

    # 1. Basic
    customer_type: Mapped[str | None] = mapped_column(String(30), nullable=True, index=True)
    legal_business_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    display_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    industry: Mapped[str | None] = mapped_column(String(100), nullable=True)
    profile_image_id: Mapped[str | None] = mapped_column(Text, nullable=True)

    # 2. Contact
    designation: Mapped[str | None] = mapped_column(String(100), nullable=True)
    alternate_mobile_number: Mapped[str | None] = mapped_column(String(20), nullable=True)
    website: Mapped[str | None] = mapped_column(String(200), nullable=True)

    # 3. Address (billing_address / delivery_address above)
    city: Mapped[str | None] = mapped_column(String(100), nullable=True)
    state: Mapped[str | None] = mapped_column(String(100), nullable=True)
    country: Mapped[str | None] = mapped_column(String(100), nullable=True)
    pin_zip_code: Mapped[str | None] = mapped_column(String(10), nullable=True)

    # 4. Business & tax (gst_number above is the GSTIN)
    pan_business_registration_no: Mapped[str | None] = mapped_column(String(30), nullable=True)
    tax_exempt: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    tax_category: Mapped[str | None] = mapped_column(String(50), nullable=True)
    currency: Mapped[str | None] = mapped_column(String(10), nullable=True)

    # 5. Payment (credit_limit / opening_balance above)
    payment_terms: Mapped[str | None] = mapped_column(String(50), nullable=True)
    preferred_payment_method: Mapped[str | None] = mapped_column(String(50), nullable=True)
    upi_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    bank_name: Mapped[str | None] = mapped_column(String(150), nullable=True)
    account_number: Mapped[str | None] = mapped_column(String(50), nullable=True)
    ifsc_swift_code: Mapped[str | None] = mapped_column(String(30), nullable=True)

    # 6. Sales / CRM (assigned_sales_officer_id above is the sales representative)
    lead_source: Mapped[str | None] = mapped_column(String(50), nullable=True)
    territory: Mapped[str | None] = mapped_column(String(100), nullable=True)
    customer_priority: Mapped[str | None] = mapped_column(String(20), nullable=True)
    preferred_communication: Mapped[list | None] = mapped_column(JSON, default=list, nullable=True)
    customer_tags: Mapped[list | None] = mapped_column(JSON, default=list, nullable=True)

    # 7. Social presence
    facebook_url: Mapped[str | None] = mapped_column(String(200), nullable=True)
    instagram_url: Mapped[str | None] = mapped_column(String(200), nullable=True)
    linkedin_url: Mapped[str | None] = mapped_column(String(200), nullable=True)
    x_twitter_url: Mapped[str | None] = mapped_column(String(200), nullable=True)
    youtube_url: Mapped[str | None] = mapped_column(String(200), nullable=True)

    # 9. Additional (notes above)
    date_of_birth: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    anniversary_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    referral_customer_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    loyalty_number: Mapped[str | None] = mapped_column(String(50), nullable=True)

    # 10. Preferences
    portal_access_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    favorite_product_ids: Mapped[list | None] = mapped_column(JSON, default=list, nullable=True)
    default_price_list_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    default_tax_profile_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    default_warehouse_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    preferred_invoice_delivery: Mapped[list | None] = mapped_column(JSON, default=list, nullable=True)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, nullable=False
    )

    documents: Mapped[list["CustomerDocument"]] = relationship(
        back_populates="customer", cascade="all, delete-orphan", lazy="joined"
    )
    assigned_sales_officer: Mapped["User | None"] = relationship(  # noqa: F821
        foreign_keys=[assigned_sales_officer_id], lazy="joined"
    )

    def recompute_outstanding(self) -> None:
        self.outstanding_balance = round(
            (self.opening_balance or 0) + (self.total_billed or 0) - (self.total_received or 0), 2
        )


class CustomerPayment(Base):
    """A payment received from a customer (reduces their outstanding balance)."""

    __tablename__ = "customer_payments"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    customer_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("customers.id", ondelete="CASCADE"), nullable=False, index=True
    )
    organization_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    order_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("sales_orders.id", ondelete="SET NULL"), nullable=True
    )
    # Which invoice this payment settles. Null means it is an advance / on-account
    # payment that only moves the customer's running balance.
    invoice_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("invoices.id", ondelete="SET NULL"), nullable=True, index=True
    )
    amount: Mapped[float] = mapped_column(Float, nullable=False)
    payment_mode: Mapped[str] = mapped_column(String(30), default="cash", nullable=False)
    reference: Mapped[str | None] = mapped_column(String(150), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    received_on: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)

    # Loaded with the payment so history rows carry the invoice number, not just its id.
    invoice: Mapped["Invoice | None"] = relationship(lazy="joined")  # noqa: F821


class CustomerDocument(Base):
    """One uploaded document on a customer (GST certificate, PAN card, …).

    A row per file rather than a column per slot: the sheet's "Other Documents"
    holds many files, and a table makes listing, downloading and deleting one of
    them uniform with the named slots.
    """

    __tablename__ = "customer_documents"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    customer_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("customers.id", ondelete="CASCADE"), nullable=False, index=True
    )
    organization_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )

    document_type: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    content_type: Mapped[str] = mapped_column(String(120), nullable=False)
    size: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # Link into stored_files — the bytes are never inlined in a response.
    url: Mapped[str] = mapped_column(Text, nullable=False)
    file_id: Mapped[str | None] = mapped_column(String(36), nullable=True)

    uploaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)

    customer: Mapped["Customer"] = relationship(back_populates="documents")
