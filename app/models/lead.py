import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Lead(Base):
    __tablename__ = "leads"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    organization_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    lead_id: Mapped[str | None] = mapped_column(String(50), nullable=True, index=True)
    name: Mapped[str | None] = mapped_column(String(150), nullable=True)
    contact_person: Mapped[str | None] = mapped_column(String(150), nullable=True)
    mobile_number: Mapped[str | None] = mapped_column(String(20), nullable=True)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    lead_source: Mapped[str | None] = mapped_column(String(50), nullable=True)
    # Legacy free-text field — kept fully functional (writable/readable) for
    # backward compatibility. interested_product_links (below) is the
    # normalized replacement; the two are independent, neither derives the
    # other.
    interested_product: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Frontend-defined categorization, not a fixed backend taxonomy — plain
    # nullable strings by design (see the change request: "the frontend's
    # actual allowed values are not guaranteed to be known"). Distinct from
    # lead_status (workflow state) and from Customer.customer_type (a
    # different, separately-validated concept applied only at conversion).
    lead_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    segment: Mapped[str | None] = mapped_column(String(50), nullable=True)
    notes: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    customer_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("customers.id", ondelete="SET NULL"), nullable=True, index=True
    )
    assigned_salesperson_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    lead_status: Mapped[str | None] = mapped_column(String(30), default="new", nullable=True)
    converted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, nullable=False
    )

    customer: Mapped["Customer | None"] = relationship(lazy="joined", primaryjoin="Lead.customer_id == Customer.id")  # noqa: F821
    assigned_salesperson: Mapped["User | None"] = relationship(lazy="joined", primaryjoin="Lead.assigned_salesperson_id == User.id")  # noqa: F821
    # Normalized multi-product interest — see LeadInterestedProduct below.
    # cascade="all, delete-orphan" so PATCH replace-semantics (clear + re-add,
    # matching QuotationUpdate.items' convention) and Lead deletion both work
    # by manipulating the Python-side collection, the same pattern already
    # used for Quotation.items.
    interested_product_links: Mapped[list["LeadInterestedProduct"]] = relationship(
        back_populates="lead", cascade="all, delete-orphan", lazy="joined"
    )

    @property
    def interested_product_ids(self) -> list[str]:
        return [link.product_id for link in self.interested_product_links]

    @property
    def interested_products(self) -> list["Product"]:  # noqa: F821
        return [link.product for link in self.interested_product_links]

    @property
    def converted_customer_id(self) -> str | None:
        return self.customer_id

    @converted_customer_id.setter
    def converted_customer_id(self, val: str | None) -> None:
        self.customer_id = val

    @property
    def assigned_sales_officer_id(self) -> str | None:
        return self.assigned_salesperson_id

    @assigned_sales_officer_id.setter
    def assigned_sales_officer_id(self, val: str | None) -> None:
        self.assigned_salesperson_id = val

    @property
    def mobile(self) -> str | None:
        return self.mobile_number

    @mobile.setter
    def mobile(self, val: str | None) -> None:
        self.mobile_number = val

    @property
    def source(self) -> str | None:
        return self.lead_source

    @source.setter
    def source(self, val: str | None) -> None:
        self.lead_source = val

    @property
    def status(self) -> str | None:
        return self.lead_status

    @status.setter
    def status(self, val: str | None) -> None:
        self.lead_status = val


class LeadInterestedProduct(Base):
    """One Product a Lead has expressed interest in — CRM interest/history,
    never automatically turned into Quotation lines (see quotation_service,
    which only ever builds items from a quotation's own explicit payload).

    ON DELETE CASCADE on both sides: this row is pure link data with no
    independent meaning once either the Lead or the Product it points at is
    gone.
    """

    __tablename__ = "lead_interested_products"
    __table_args__ = (
        UniqueConstraint("lead_id", "product_id", name="uq_lead_interested_product"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    organization_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    lead_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("leads.id", ondelete="CASCADE"), nullable=False, index=True
    )
    product_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("products.id", ondelete="CASCADE"), nullable=False, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)

    lead: Mapped["Lead"] = relationship(back_populates="interested_product_links")
    product: Mapped["Product"] = relationship(lazy="joined")  # noqa: F821
