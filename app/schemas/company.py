from datetime import datetime
from enum import Enum
from typing import Annotated

from pydantic import (
    AliasChoices,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    field_validator,
)


class CompanyStatus(str, Enum):
    """Company Master "Status" — is this company operating? (Not the subscription state.)"""

    ACTIVE = "active"
    INACTIVE = "inactive"


def _normalize_status(value: object) -> object:
    """Accept "Active" / "INACTIVE" from a toggle or dropdown."""
    if isinstance(value, str):
        return value.strip().lower()
    return value


CompanyStatusIn = Annotated[CompanyStatus, BeforeValidator(_normalize_status)]


class BusinessDocumentSlot(str, Enum):
    """The Company Master's single-file document slots, uploadable via
    POST /organizations/settings/documents/{slot}. "Other Business Documents"
    is deliberately not here — it holds many files and has its own endpoints."""

    gst_certificate = "gst_certificate"
    pan_card = "pan_card"
    certificate_of_incorporation = "certificate_of_incorporation"
    trade_license = "trade_license"
    msme_certificate = "msme_certificate"
    fssai_license = "fssai_license"

    @property
    def column(self) -> str:
        """The organization column this slot writes to."""
        return {
            BusinessDocumentSlot.gst_certificate: "doc_gst_url",
            BusinessDocumentSlot.pan_card: "doc_pan_url",
            BusinessDocumentSlot.certificate_of_incorporation: "doc_coi_url",
            BusinessDocumentSlot.trade_license: "doc_trade_license_url",
            BusinessDocumentSlot.msme_certificate: "doc_msme_url",
            BusinessDocumentSlot.fssai_license: "doc_fssai_url",
        }[self]


class OtherDocument(BaseModel):
    """One file in the "Other Business Documents" multi-upload slot."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    url: str
    content_type: str | None = None
    size: int | None = None
    uploaded_at: datetime | None = None


class CompanySettingsOut(BaseModel):
    """Full company profile shown/edited on the Company Settings page."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    business_type: str | None
    gst_number: str | None
    pan_number: str | None
    address: str | None
    phone: str | None
    email: str | None
    financial_year: str | None
    logo_url: str | None
    signature_url: str | None
    field_settings: dict | None = None

    # Basic Info (Ext)
    legal_name: str | None = None
    industry: str | None = None
    date_of_incorporation: str | None = None
    cin_number: str | None = None
    gstin_pan: str | None = None
    description: str | None = None

    # Contact Info (Ext)
    primary_mobile: str | None = None
    alternate_mobile: str | None = None
    landline: str | None = None
    website: str | None = None
    customer_support_number: str | None = None

    # Address Info (Ext)
    registered_address: str | None = None
    branch_address: str | None = None
    city: str | None = None
    state: str | None = None
    country: str | None = None
    pin_code: str | None = None

    # Branding (Ext)
    stamp_url: str | None = None
    letterhead_url: str | None = None
    banner_url: str | None = None

    # Digital Payments (Ext)
    payment_qr_url: str | None = None
    upi_id: str | None = None
    bank_account_details: str | None = None
    bank_account_holder: str | None = None
    bank_ifsc: str | None = None
    bank_name: str | None = None

    # Social Presence (Ext)
    facebook_url: str | None = None
    instagram_url: str | None = None
    linkedin_url: str | None = None
    twitter_url: str | None = None
    youtube_url: str | None = None
    whatsapp_number: str | None = None

    # Business Settings (Ext)
    currency: str | None = None
    timezone: str | None = None
    language: str | None = None
    tax_configuration: str | None = None
    invoice_settings: str | None = None
    employee_id_prefix: str | None = None

    # Documents (Ext)
    doc_gst_url: str | None = None
    doc_pan_url: str | None = None
    doc_coi_url: str | None = None
    doc_trade_license_url: str | None = None
    doc_msme_url: str | None = None
    doc_fssai_url: str | None = None
    doc_other_url: str | None = None
    # Every "Other Business Document" uploaded — managed via /settings/documents/other.
    doc_other_files: list[OtherDocument] = Field(default_factory=list)

    # Authorized Person (Ext)
    auth_person_name: str | None = None
    auth_person_designation: str | None = None
    auth_person_mobile: str | None = None
    auth_person_email: str | None = None
    auth_person_photo_url: str | None = None
    auth_person_signature_url: str | None = None

    # Additional Info (Ext)
    employee_count: int | None = None
    business_hours: str | None = None
    mission_vision: str | None = None
    notes: str | None = None
    # Is the company active? Defaults to active for rows written before this field.
    company_status: CompanyStatus = CompanyStatus.ACTIVE
    # Read-only mirror of the subscription lifecycle (trial / active / locked / …),
    # which only a Super Admin changes — never editable from this page.
    subscription_status: str | None = None

    @field_validator("doc_other_files", mode="before")
    @classmethod
    def _default_documents(cls, v: object) -> object:
        return v or []

    @field_validator("company_status", mode="before")
    @classmethod
    def _default_company_status(cls, v: object) -> object:
        return _normalize_status(v) or CompanyStatus.ACTIVE


class CompanySettingsUpdate(BaseModel):
    """Partial update — send only the fields being changed."""

    model_config = ConfigDict(populate_by_name=True)

    name: str | None = Field(default=None, min_length=1, max_length=200)
    business_type: str | None = Field(default=None, max_length=100)
    gst_number: str | None = Field(default=None, max_length=20)
    pan_number: str | None = Field(default=None, max_length=20)
    address: str | None = Field(default=None, max_length=500)
    phone: str | None = Field(default=None, max_length=20)
    email: str | None = None
    financial_year: str | None = Field(default=None, max_length=20)
    logo_url: str | None = None
    signature_url: str | None = None

    # Basic Info (Ext)
    legal_name: str | None = Field(default=None, max_length=200)
    industry: str | None = Field(default=None, max_length=100)
    date_of_incorporation: str | None = Field(default=None, max_length=50)
    cin_number: str | None = Field(default=None, max_length=50)
    gstin_pan: str | None = Field(default=None, max_length=50)
    description: str | None = None

    # Contact Info (Ext)
    primary_mobile: str | None = Field(default=None, max_length=20)
    alternate_mobile: str | None = Field(default=None, max_length=20)
    landline: str | None = Field(default=None, max_length=20)
    website: str | None = Field(default=None, max_length=200)
    customer_support_number: str | None = Field(default=None, max_length=20)

    # Address Info (Ext)
    registered_address: str | None = None
    branch_address: str | None = None
    city: str | None = Field(default=None, max_length=100)
    state: str | None = Field(default=None, max_length=100)
    country: str | None = Field(default=None, max_length=100)
    pin_code: str | None = Field(default=None, max_length=20)

    # Branding (Ext)
    stamp_url: str | None = None
    letterhead_url: str | None = None
    banner_url: str | None = None

    # Digital Payments (Ext)
    payment_qr_url: str | None = None
    upi_id: str | None = Field(default=None, max_length=100)
    bank_account_details: str | None = None
    bank_account_holder: str | None = Field(default=None, max_length=200)
    bank_ifsc: str | None = Field(default=None, max_length=20)
    bank_name: str | None = Field(default=None, max_length=200)

    # Social Presence (Ext)
    facebook_url: str | None = Field(default=None, max_length=200)
    instagram_url: str | None = Field(default=None, max_length=200)
    linkedin_url: str | None = Field(default=None, max_length=200)
    twitter_url: str | None = Field(default=None, max_length=200)
    youtube_url: str | None = Field(default=None, max_length=200)
    whatsapp_number: str | None = Field(default=None, max_length=20)

    # Business Settings (Ext)
    currency: str | None = Field(default=None, max_length=10)
    timezone: str | None = Field(default=None, max_length=50)
    language: str | None = Field(default=None, max_length=50)
    tax_configuration: str | None = None
    invoice_settings: str | None = None
    employee_id_prefix: str | None = Field(
        default=None, max_length=20, description='Prefix for auto-generated employee codes (default "EMP-")'
    )

    # Documents (Ext)
    doc_gst_url: str | None = None
    doc_pan_url: str | None = None
    doc_coi_url: str | None = None
    doc_trade_license_url: str | None = None
    doc_msme_url: str | None = None
    doc_fssai_url: str | None = None
    doc_other_url: str | None = None

    # Authorized Person (Ext)
    auth_person_name: str | None = Field(default=None, max_length=200)
    auth_person_designation: str | None = Field(default=None, max_length=100)
    auth_person_mobile: str | None = Field(default=None, max_length=20)
    auth_person_email: str | None = Field(default=None, max_length=255)
    auth_person_photo_url: str | None = None
    auth_person_signature_url: str | None = None

    # Additional Info (Ext)
    employee_count: int | None = None
    business_hours: str | None = Field(default=None, max_length=100)
    mission_vision: str | None = Field(default=None, max_length=1000)
    notes: str | None = None
    # Sent as either "company_status" or plain "status" — the Company Master toggle.
    # It can never reach the subscription `status` column, which is Super-Admin-only.
    company_status: CompanyStatusIn | None = Field(
        default=None, validation_alias=AliasChoices("company_status", "status")
    )

    @field_validator("website")
    @classmethod
    def validate_website(cls, v: str | None) -> str | None:
        if v is not None and v != "" and not v.startswith("https://"):
            raise ValueError("Website must begin with https://")
        return v

    @field_validator("email", "auth_person_email")
    @classmethod
    def validate_emails(cls, v: str | None) -> str | None:
        if v is not None and v != "":
            if "@" not in v or "." not in v:
                raise ValueError("Must be a valid email address")
        return v


class UploadResponse(BaseModel):
    url: str


class FieldSettingsOut(BaseModel):
    field_settings: dict[str, dict[str, bool]]
    available_fields: dict[str, dict[str, list[str]]]


class FieldSettingsUpdate(BaseModel):
    field_settings: dict[str, dict[str, bool]]


