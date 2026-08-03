import math
import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, Enum, ForeignKey, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.models.enums import OrganizationStatus


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Organization(Base):
    """A firm/shop/business — the tenant boundary. All firm data is scoped to it."""

    __tablename__ = "organizations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(200), nullable=False)

    # Business profile.
    business_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    gst_number: Mapped[str | None] = mapped_column(String(20), nullable=True)
    pan_number: Mapped[str | None] = mapped_column(String(20), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(20), nullable=True)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    address: Mapped[str | None] = mapped_column(String(500), nullable=True)
    financial_year: Mapped[str | None] = mapped_column(String(20), nullable=True)
    # logo/signature can hold a data: URL (base64) or an external URL — use Text (no length cap).
    logo_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    signature_url: Mapped[str | None] = mapped_column(Text, nullable=True)  # used on invoices

    # Basic Info (Ext)
    legal_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    industry: Mapped[str | None] = mapped_column(String(100), nullable=True)
    date_of_incorporation: Mapped[str | None] = mapped_column(String(50), nullable=True)
    cin_number: Mapped[str | None] = mapped_column(String(50), nullable=True)
    gstin_pan: Mapped[str | None] = mapped_column(String(50), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Contact Info (Ext)
    primary_mobile: Mapped[str | None] = mapped_column(String(20), nullable=True)
    alternate_mobile: Mapped[str | None] = mapped_column(String(20), nullable=True)
    landline: Mapped[str | None] = mapped_column(String(20), nullable=True)
    website: Mapped[str | None] = mapped_column(String(200), nullable=True)
    customer_support_number: Mapped[str | None] = mapped_column(String(20), nullable=True)

    # Address Info (Ext)
    registered_address: Mapped[str | None] = mapped_column(Text, nullable=True)
    branch_address: Mapped[str | None] = mapped_column(Text, nullable=True)
    city: Mapped[str | None] = mapped_column(String(100), nullable=True)
    state: Mapped[str | None] = mapped_column(String(100), nullable=True)
    country: Mapped[str | None] = mapped_column(String(100), nullable=True)
    pin_code: Mapped[str | None] = mapped_column(String(20), nullable=True)

    # Branding (Ext)
    stamp_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    letterhead_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    banner_url: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Digital Payments (Ext)
    payment_qr_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    upi_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    bank_account_details: Mapped[str | None] = mapped_column(Text, nullable=True)
    bank_account_holder: Mapped[str | None] = mapped_column(String(200), nullable=True)
    bank_ifsc: Mapped[str | None] = mapped_column(String(20), nullable=True)
    bank_name: Mapped[str | None] = mapped_column(String(200), nullable=True)

    # Social Presence (Ext)
    facebook_url: Mapped[str | None] = mapped_column(String(200), nullable=True)
    instagram_url: Mapped[str | None] = mapped_column(String(200), nullable=True)
    linkedin_url: Mapped[str | None] = mapped_column(String(200), nullable=True)
    twitter_url: Mapped[str | None] = mapped_column(String(200), nullable=True)
    youtube_url: Mapped[str | None] = mapped_column(String(200), nullable=True)
    whatsapp_number: Mapped[str | None] = mapped_column(String(20), nullable=True)

    # Business Settings (Ext)
    currency: Mapped[str | None] = mapped_column(String(10), nullable=True)
    timezone: Mapped[str | None] = mapped_column(String(50), nullable=True)
    language: Mapped[str | None] = mapped_column(String(50), nullable=True)
    tax_configuration: Mapped[str | None] = mapped_column(Text, nullable=True)
    invoice_settings: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Documents (Ext)
    doc_gst_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    doc_pan_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    doc_coi_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    doc_trade_license_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    doc_msme_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    doc_fssai_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    doc_other_url: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Authorized Person (Ext)
    auth_person_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    auth_person_designation: Mapped[str | None] = mapped_column(String(100), nullable=True)
    auth_person_mobile: Mapped[str | None] = mapped_column(String(20), nullable=True)
    auth_person_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    auth_person_photo_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    auth_person_signature_url: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Additional Info (Ext)
    employee_count: Mapped[int | None] = mapped_column(nullable=True)
    business_hours: Mapped[str | None] = mapped_column(String(100), nullable=True)
    mission_vision: Mapped[str | None] = mapped_column(Text, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    status: Mapped[OrganizationStatus] = mapped_column(
        Enum(OrganizationStatus), default=OrganizationStatus.TRIAL, nullable=False
    )

    # --- Subscription & trial lifecycle (all nullable so they auto-migrate on the live DB) ---
    plan_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("plans.id"), nullable=True, index=True
    )
    requested_plan_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("plans.id"), nullable=True
    )
    # "monthly" / "yearly" (BillingCycle value); which cycle the org is/wants to be billed on.
    billing_cycle: Mapped[str | None] = mapped_column(String(10), nullable=True)

    trial_ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    upgrade_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # UpgradeStatus value ("none"/"pending"/"approved"/"rejected").
    upgrade_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    upgrade_reject_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)

    field_settings: Mapped[dict | None] = mapped_column(JSON, default=dict, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)

    users: Mapped[list["User"]] = relationship(  # noqa: F821
        back_populates="organization", cascade="all, delete-orphan"
    )
    plan: Mapped["Plan | None"] = relationship(  # noqa: F821
        foreign_keys=[plan_id], lazy="joined"
    )
    requested_plan: Mapped["Plan | None"] = relationship(  # noqa: F821
        foreign_keys=[requested_plan_id], lazy="joined"
    )

    @property
    def trial_days_left(self) -> int | None:
        """Whole days remaining in the trial (0 if past), or None if no trial set."""
        if self.trial_ends_at is None:
            return None
        end = self.trial_ends_at
        if end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)
        seconds = (end - datetime.now(timezone.utc)).total_seconds()
        return max(0, math.ceil(seconds / 86400))
