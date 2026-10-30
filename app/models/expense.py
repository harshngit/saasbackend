import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base

EXPENSE_STATUSES = {"pending", "approved", "rejected", "clarification_requested"}
# The approved workflow, as clients see it (`Expense.lifecycle_status`):
#   pending_approval -> approved | rejected ; approved -> reimbursed
# Stored `status` keeps its original vocabulary (reports, dashboards, the GL and cash
# reconciliation all read status == "approved"): a reimbursed expense stays
# status "approved" with reimbursed_at set, and clarification_requested is a
# sub-state of pending_approval.
EXPENSE_LIFECYCLE_STATUSES = ("pending_approval", "approved", "rejected", "reimbursed")
# Canonical reimbursement payment methods (same keys as the rest of the payments code).
EXPENSE_REIMBURSEMENT_METHODS = ("cash", "bank_transfer", "upi", "cheque")
# Suggested categories (free-form; frontend can offer these).
EXPENSE_CATEGORIES = [
    "Petrol/Diesel", "Food and Travel", "Office Expenses", "Rent", "Utilities",
    "Vehicle Maintenance", "Staff Expenses", "Delivery Expenses", "Parking", "Toll", "Other",
]


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Expense(Base):
    __tablename__ = "expenses"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    organization_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    category: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    amount: Mapped[float] = mapped_column(Float, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    expense_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)
    payment_mode: Mapped[str | None] = mapped_column(String(30), nullable=True)
    receipt_url: Mapped[str | None] = mapped_column(Text, nullable=True)  # bill/receipt image

    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False, index=True)
    submitted_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    approved_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    reject_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)

    # Approval / rejection / reimbursement audit (set only by the lifecycle actions)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    approval_note: Mapped[str | None] = mapped_column(String(500), nullable=True)
    rejected_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    rejected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reimbursed_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    reimbursed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reimbursement_method: Mapped[str | None] = mapped_column(String(30), nullable=True)
    reimbursement_reference: Mapped[str | None] = mapped_column(String(150), nullable=True)
    reimbursement_notes: Mapped[str | None] = mapped_column(String(500), nullable=True)

    # Basic Information (Ext)
    expense_id: Mapped[str | None] = mapped_column(String(50), nullable=True, index=True)
    expense_number: Mapped[str | None] = mapped_column(String(50), nullable=True, index=True)
    expense_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    expense_status: Mapped[str | None] = mapped_column(String(30), nullable=True)
    financial_year: Mapped[str | None] = mapped_column(String(20), nullable=True)
    branch_id: Mapped[str | None] = mapped_column(String(50), nullable=True, index=True)
    department_id: Mapped[str | None] = mapped_column(String(50), nullable=True, index=True)

    # Payee Details
    vendor_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("suppliers.id", ondelete="SET NULL"), nullable=True, index=True
    )
    payee_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    contact_person: Mapped[str | None] = mapped_column(String(150), nullable=True)
    mobile_number: Mapped[str | None] = mapped_column(String(20), nullable=True)
    email_address: Mapped[str | None] = mapped_column(String(255), nullable=True)
    payee_gstin: Mapped[str | None] = mapped_column(String(20), nullable=True)

    # Expense Details & Taxes
    subtotal: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    tax_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    tax_amount: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    currency: Mapped[str] = mapped_column(String(10), default="INR", nullable=False)

    # Payment Details
    paid_from_account_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    payment_reference: Mapped[str | None] = mapped_column(String(150), nullable=True)
    payment_status: Mapped[str | None] = mapped_column(String(30), nullable=True)
    approval_status: Mapped[str | None] = mapped_column(String(30), nullable=True)

    # Accounting
    expense_account_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    cost_center_id: Mapped[str | None] = mapped_column(String(50), nullable=True)
    project_id: Mapped[str | None] = mapped_column(String(50), nullable=True)
    tax_category: Mapped[str | None] = mapped_column(String(50), nullable=True)
    tds_applicable: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    tds_amount: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)

    # Tags
    tags: Mapped[list | None] = mapped_column(JSON, default=list, nullable=True)

    # Documents & Supporting Attachments
    vendor_invoice_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    supporting_documents: Mapped[list | None] = mapped_column(JSON, default=list, nullable=True)

    # Recurring Expense
    is_recurring: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    recurrence_frequency: Mapped[str | None] = mapped_column(String(20), nullable=True)  # daily, weekly, monthly, yearly
    next_due_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, nullable=False
    )

    items: Mapped[list["ExpenseItem"]] = relationship(
        back_populates="expense", cascade="all, delete-orphan", lazy="joined"
    )
    vendor: Mapped["Supplier | None"] = relationship(lazy="joined", primaryjoin="Expense.vendor_id == Supplier.id")  # noqa: F821

    @property
    def lifecycle_status(self) -> str:
        if self.status == "approved":
            return "reimbursed" if self.reimbursed_at is not None else "approved"
        if self.status == "rejected":
            return "rejected"
        return "pending_approval"

    @property
    def rejection_reason(self) -> str | None:
        return self.reject_reason if self.status == "rejected" else None

    @property
    def reimbursed_amount(self) -> float | None:
        return self.net_payable if self.reimbursed_at is not None else None

    @property
    def net_payable(self) -> float:
        return round(max((self.amount or 0.0) - (self.tds_amount or 0.0), 0.0), 2)


class ExpenseItem(Base):
    __tablename__ = "expense_items"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    expense_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("expenses.id", ondelete="CASCADE"), nullable=False, index=True
    )
    description: Mapped[str] = mapped_column(String(255), nullable=False)
    quantity: Mapped[float] = mapped_column(Float, default=1.0, nullable=False)
    unit_price: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    tax_rate: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    tax_amount: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    line_total: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)

    expense: Mapped["Expense"] = relationship(back_populates="items")
