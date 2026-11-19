import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Invoice(Base):
    __tablename__ = "invoices"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    organization_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    invoice_number: Mapped[str] = mapped_column(String(60), nullable=False, index=True)
    sales_id: Mapped[str | None] = mapped_column(String(50), nullable=True, index=True)
    sales_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    sales_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    sales_status: Mapped[str | None] = mapped_column(String(30), nullable=True)
    invoice_status: Mapped[str | None] = mapped_column(String(30), nullable=True, index=True)
    payment_status: Mapped[str | None] = mapped_column(String(30), nullable=True)

    order_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("sales_orders.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # Null on a walk-in counter sale. The name and number the buyer gave, if any, are
    # snapshotted below rather than creating a customer record nobody will use again.
    customer_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("customers.id", ondelete="SET NULL"), nullable=True, index=True
    )
    walk_in_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    walk_in_phone: Mapped[str | None] = mapped_column(String(20), nullable=True)
    # Which delivery this bills. A partial delivery is billed for what was actually
    # handed over, so one order can carry several invoices.
    delivery_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("deliveries.id", ondelete="SET NULL"), nullable=True, index=True
    )
    invoice_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)
    # When payment is due, from the order's payment terms. Drives the ageing buckets.
    due_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Copied from the customer when the invoice is raised — a bill must keep the
    # address it was issued to, even if the customer later moves.
    billing_address: Mapped[str | None] = mapped_column(Text, nullable=True)

    status: Mapped[str] = mapped_column(String(20), default="unpaid", nullable=False, index=True)

    subtotal: Mapped[float] = mapped_column(Float, default=0, nullable=False)
    discount: Mapped[float] = mapped_column(Float, default=0, nullable=False)
    tax: Mapped[float] = mapped_column(Float, default=0, nullable=False)
    # Freight, packing, or a delivery fee added to the bill, and the paise rounding
    # that makes the total a whole rupee. Both print on the detailed invoice.
    additional_charges: Mapped[float | None] = mapped_column(Float, nullable=True)
    round_off: Mapped[float | None] = mapped_column(Float, nullable=True)
    total: Mapped[float] = mapped_column(Float, default=0, nullable=False)
    amount_paid: Mapped[float] = mapped_column(Float, default=0, nullable=False)

    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    # A credit note is its own document, raised against the invoice it credits — the
    # original bill stays on the account as the debit it always was.
    is_credit_note: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    credit_note_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    credit_note_for_invoice_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("invoices.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # A plain column rather than a foreign key: sales_returns already points at
    # invoices, and a constraint both ways is a cycle the schema cannot be created in.
    sales_return_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    created_by: Mapped[str | None] = mapped_column(String(36), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, nullable=False
    )

    eway_bill_number: Mapped[str | None] = mapped_column(String(50), nullable=True)
    vehicle_number: Mapped[str | None] = mapped_column(String(50), nullable=True)

    items: Mapped[list["InvoiceItem"]] = relationship(
        back_populates="invoice", cascade="all, delete-orphan", lazy="joined"
    )
    customer: Mapped["Customer | None"] = relationship(lazy="joined")  # noqa: F821
    order: Mapped["SalesOrder | None"] = relationship(lazy="joined")  # noqa: F821
    delivery: Mapped["Delivery | None"] = relationship(lazy="joined")  # noqa: F821

    # ---------------------------------------------------------------- GST breakdown
    # Derived, never stored: the taxable value of each line (its value before tax less its
    # share of the bill-level discount, split as gst_calc splits it) and the CGST + SGST /
    # IGST split, from the seller's and the buyer's state (GSTIN state code first, else the
    # state on the record). Unknown supply type -> the split fields are null.

    def _seller(self):  # noqa: ANN202
        from sqlalchemy.orm import object_session

        from app.models.organization import Organization

        session = object_session(self)
        return session.get(Organization, self.organization_id) if session is not None else None

    @property
    def customer_gstin(self) -> str | None:
        return self.customer.gst_number if self.customer is not None else None

    @property
    def seller_gstin(self) -> str | None:
        org = self._seller()
        return org.gst_number if org is not None else None

    @property
    def place_of_supply(self) -> str | None:
        """The buyer's state (where the goods are supplied), lower-case."""
        from app.services import gst_calc

        c = self.customer
        return gst_calc.state_of(c.gst_number if c else None, c.state if c else None)

    @property
    def supply_type(self) -> str | None:
        """intra_state | inter_state | null when either side's state is unknown."""
        intra = self._intra_state()
        return None if intra is None else ("intra_state" if intra else "inter_state")

    def _intra_state(self) -> bool | None:
        from app.services import gst_calc

        org, c = self._seller(), self.customer
        return gst_calc.is_intra_state(
            org.gst_number if org else None, org.state if org else None,
            c.gst_number if c else None, c.state if c else None,
        )

    def gst_lines(self) -> dict[str, dict]:
        """Per item id: taxable_value + the CGST / SGST / IGST split."""
        from app.services import gst_calc

        items = list(self.items or [])
        shares = gst_calc.split_discount([gst_calc.money(i.line_total) for i in items], self.discount or 0)
        intra = self._intra_state()
        out = {}
        odd_lines = 0
        for item, share in zip(items, shares):
            tax = item.tax_amount if item.tax_amount is not None else item.tax
            # A line whose GST is an odd number of paise gives the extra paisa to CGST and
            # SGST in turn (1st such line CGST, 2nd SGST, ...), so the invoice's CGST and
            # SGST totals are equal, or differ by 0.01 when the count of such lines is odd.
            odd = gst_calc.money(tax or 0) % gst_calc.Decimal("0.02") != 0
            out[item.id] = {
                "taxable_value": float(gst_calc.money(item.line_total) - share),
                **gst_calc.gst_split(tax or 0, item.tax_rate or 0, intra, odd_paisa_to_sgst=odd and odd_lines % 2 == 1),
            }
            if odd:
                odd_lines += 1
        return out

    @property
    def taxable_total(self) -> float:
        return round(sum(v["taxable_value"] for v in self.gst_lines().values()), 2)

    @property
    def tax_amount(self) -> float:
        """The same figure as `tax`, under the name the lines use."""
        return self.tax

    def _tax_total(self, key: str) -> float | None:
        values = [v[key] for v in self.gst_lines().values()]
        if any(v is None for v in values):
            return None
        return round(sum(values), 2)

    @property
    def cgst_total(self) -> float | None:
        return self._tax_total("cgst_amount")

    @property
    def sgst_total(self) -> float | None:
        return self._tax_total("sgst_amount")

    @property
    def igst_total(self) -> float | None:
        return self._tax_total("igst_amount")


class InvoiceItem(Base):
    __tablename__ = "invoice_items"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    invoice_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("invoices.id", ondelete="CASCADE"), nullable=False, index=True
    )
    product_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("products.id", ondelete="SET NULL"), nullable=True
    )
    variant_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("product_variants.id", ondelete="SET NULL"), nullable=True
    )
    product_name: Mapped[str] = mapped_column(String(200), nullable=False)
    # Copied from the product at invoicing time — a GST invoice must show the HSN
    # that applied then, even if the product's code is corrected later.
    hsn_code: Mapped[str | None] = mapped_column(String(20), nullable=True)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    unit_price: Mapped[float] = mapped_column(Float, default=0, nullable=False)
    discount: Mapped[float] = mapped_column(Float, default=0, nullable=False)
    tax: Mapped[float] = mapped_column(Float, default=0, nullable=False)
    line_total: Mapped[float] = mapped_column(Float, default=0, nullable=False)
    uom: Mapped[str | None] = mapped_column(String(50), nullable=True)  # = Product.uom (alembic d5b8f0c3e2a4)
    tax_amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    tax_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    # The delivery line this bills, so an invoice traces back to what was handed over.
    delivery_item_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("delivery_items.id", ondelete="SET NULL"), nullable=True, index=True
    )
    order_item_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("sales_order_items.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # Which lot and which units this line was for. Written when the goods actually
    # move, so a bill or a challan can be traced to the batch that left the shelf.
    batch_number: Mapped[str | None] = mapped_column(String(60), nullable=True)
    expiry_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    serial_numbers: Mapped[list | None] = mapped_column(JSON, nullable=True)

    invoice: Mapped["Invoice"] = relationship(back_populates="items")
    product: Mapped["Product | None"] = relationship(foreign_keys=[product_id], lazy="joined")  # noqa: F821
    variant: Mapped["ProductVariant | None"] = relationship(foreign_keys=[variant_id], lazy="joined")  # noqa: F821

    def _gst(self) -> dict:
        if self.invoice is None:
            return {}
        return self.invoice.gst_lines().get(self.id, {})

    @property
    def taxable_value(self) -> float | None:
        return self._gst().get("taxable_value")

    @property
    def cgst_rate(self) -> float | None:
        return self._gst().get("cgst_rate")

    @property
    def cgst_amount(self) -> float | None:
        return self._gst().get("cgst_amount")

    @property
    def sgst_rate(self) -> float | None:
        return self._gst().get("sgst_rate")

    @property
    def sgst_amount(self) -> float | None:
        return self._gst().get("sgst_amount")

    @property
    def igst_rate(self) -> float | None:
        return self._gst().get("igst_rate")

    @property
    def igst_amount(self) -> float | None:
        return self._gst().get("igst_amount")

    @property
    def product_image_url(self) -> str | None:
        from app.core.files import normalize_file_url

        if self.variant and self.variant.image_url:
            return normalize_file_url(self.variant.image_url)
        if self.product:
            if self.product.cover_image:
                return normalize_file_url(self.product.cover_image)
            if self.product.images and isinstance(self.product.images, list) and len(self.product.images) > 0:
                return normalize_file_url(self.product.images[0])
        return None

