import json
import re
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


class BranchAddress(BaseModel):
    """One entry in the repeatable Branch / Office Addresses section."""

    id: str | None = Field(default=None, description="Server-assigned; omit when adding")
    label: str | None = Field(default=None, max_length=100, description='e.g. "Pune Warehouse"')
    address: str = Field(min_length=1, max_length=500)
    city: str | None = Field(default=None, max_length=100)
    state: str | None = Field(default=None, max_length=100)
    country: str | None = Field(default=None, max_length=100)
    pin_code: str | None = Field(default=None, max_length=10)


# --- Validation rules from the Company Master sheet -------------------------
# Applied on input only, so rows written before these existed still read back.

_GSTIN_RE = re.compile(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][0-9A-Z]Z[0-9A-Z]$")
_PAN_RE = re.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]$")
_IFSC_RE = re.compile(r"^[A-Z]{4}0[A-Z0-9]{6}$")
_PIN_RE = re.compile(r"^[0-9]{5,10}$")
# Deliberately permissive: E.164 is preferred but existing rows hold plain
# 10-digit numbers, and rejecting those would break every edit of an old record.
_PHONE_RE = re.compile(r"^\+?[0-9][0-9\s\-()]{5,19}$")


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
    branch_addresses: list[BranchAddress] = Field(default_factory=list)
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
    # Returned as an object when a config object was saved, otherwise as the
    # plain string that was there before configuration panels existed.
    tax_configuration: dict | str | None = None
    invoice_settings: dict | str | None = None
    employee_id_prefix: str | None = None
    number_prefixes: dict[str, str] = Field(default_factory=dict)

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

    @field_validator("doc_other_files", "branch_addresses", mode="before")
    @classmethod
    def _default_documents(cls, v: object) -> object:
        return v or []

    @field_validator("number_prefixes", mode="before")
    @classmethod
    def _default_prefixes(cls, v: object) -> object:
        return v or {}

    @field_validator("tax_configuration", "invoice_settings", mode="before")
    @classmethod
    def _parse_config(cls, v: object) -> object:
        """Config panels are stored as a JSON string in a TEXT column; hand back the
        object when it parses, and the raw string when it is legacy free text."""
        if isinstance(v, str) and v.strip().startswith("{"):
            try:
                return json.loads(v)
            except ValueError:
                return v
        return v

    @field_validator("company_status", mode="before")
    @classmethod
    def _default_company_status(cls, v: object) -> object:
        return _normalize_status(v) or CompanyStatus.ACTIVE


class CompanySettingsUpdate(BaseModel):
    """Partial update — send only the fields being changed.

    Field lengths and formats follow the Company Master sheet's validation rules.
    Where the sheet's name differs from the stored one, both are accepted (e.g.
    `pin_zip_code` for `pin_code`, `company_logo` for `logo_url`).
    """

    model_config = ConfigDict(populate_by_name=True)

    name: str | None = Field(default=None, min_length=1, max_length=100)
    business_type: str | None = Field(default=None, max_length=100)
    gst_number: str | None = Field(default=None, max_length=20)
    pan_number: str | None = Field(default=None, max_length=20)
    address: str | None = Field(default=None, max_length=500)
    phone: str | None = Field(default=None, max_length=20)
    email: str | None = None
    financial_year: str | None = Field(default=None, max_length=20)
    logo_url: str | None = Field(default=None, validation_alias=AliasChoices("logo_url", "company_logo"))
    signature_url: str | None = Field(
        default=None, validation_alias=AliasChoices("signature_url", "authorized_signature")
    )

    # Basic Info (Ext)
    legal_name: str | None = Field(default=None, max_length=150)
    industry: str | None = Field(default=None, max_length=100)
    date_of_incorporation: str | None = Field(default=None, max_length=50)
    cin_number: str | None = Field(default=None, max_length=30)
    gstin_pan: str | None = Field(default=None, max_length=15, description="GSTIN (15) or PAN (10)")
    description: str | None = Field(default=None, max_length=500)

    # Contact Info (Ext)
    primary_mobile: str | None = Field(default=None, max_length=20)
    alternate_mobile: str | None = Field(default=None, max_length=20)
    landline: str | None = Field(default=None, max_length=20)
    website: str | None = Field(default=None, max_length=200)
    customer_support_number: str | None = Field(default=None, max_length=20)

    # Address Info (Ext)
    registered_address: str | None = None
    branch_address: str | None = Field(
        default=None, deprecated=True, description="Single legacy branch address; use branch_addresses"
    )
    branch_addresses: list[BranchAddress] | None = Field(
        default=None, description="Repeatable branch / office addresses. Replaces the whole list."
    )
    city: str | None = Field(default=None, max_length=100)
    state: str | None = Field(default=None, max_length=100)
    country: str | None = Field(default=None, max_length=100)
    pin_code: str | None = Field(
        default=None, max_length=10, validation_alias=AliasChoices("pin_code", "pin_zip_code")
    )

    # Branding (Ext)
    stamp_url: str | None = None
    letterhead_url: str | None = None
    banner_url: str | None = None

    # Digital Payments (Ext)
    payment_qr_url: str | None = Field(
        default=None,
        validation_alias=AliasChoices("payment_qr_url", "google_pay_phonepe_paytm_qr_code"),
    )
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
    timezone: str | None = Field(
        default=None, max_length=50, validation_alias=AliasChoices("timezone", "time_zone")
    )
    language: str | None = Field(default=None, max_length=50)
    tax_configuration: dict | str | None = Field(
        default=None,
        description="Configuration object (e.g. {regime, default_gst_rate, prices_include_tax}) "
                    "or a plain string. Stored as JSON when an object is sent.",
    )
    invoice_settings: dict | str | None = Field(
        default=None,
        description="Configuration object (e.g. {numbering_series, prefix, next_number, footer, "
                    "terms, logo_placement, signature_placement}) or a plain string.",
    )
    employee_id_prefix: str | None = Field(
        default=None, max_length=20, description='Prefix for auto-generated employee codes (default "EMP-")'
    )
    number_prefixes: dict[str, str] | None = Field(
        default=None,
        description='Per-series prefix overrides, e.g. {"QT": "EST", "INV": "BILL"}. '
                    "Series: CUST, PROD, LEAD, QT, SO, INV, SALE, RCPT, RET, DN, EXP, EXPID, PUR, PURID.",
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
    auth_person_name: str | None = Field(
        default=None, max_length=200,
        validation_alias=AliasChoices("auth_person_name", "owner_director_name"),
    )
    auth_person_designation: str | None = Field(
        default=None, max_length=100,
        validation_alias=AliasChoices("auth_person_designation", "designation"),
    )
    auth_person_mobile: str | None = Field(
        default=None, max_length=20,
        validation_alias=AliasChoices("auth_person_mobile", "mobile_number"),
    )
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
        """Sheet: must begin with https://. A bare domain is upgraded rather than
        rejected, so "acme.com" typed into the form still saves."""
        if v is None or v == "":
            return v
        if v.startswith("http://"):
            raise ValueError("Website must begin with https://")
        if not v.startswith("https://"):
            return f"https://{v}"
        return v

    @field_validator("email", "auth_person_email")
    @classmethod
    def validate_emails(cls, v: str | None) -> str | None:
        if v is not None and v != "":
            if "@" not in v or "." not in v:
                raise ValueError("Must be a valid email address")
        return v

    @field_validator("gstin_pan", "gst_number", "pan_number", "bank_ifsc", mode="before")
    @classmethod
    def _upper(cls, v: object) -> object:
        """These are always uppercase — normalise instead of rejecting lowercase input."""
        return v.strip().upper() if isinstance(v, str) else v

    @field_validator("gstin_pan")
    @classmethod
    def validate_gstin_pan(cls, v: str | None) -> str | None:
        if not v:
            return v
        if len(v) == 15 and _GSTIN_RE.match(v):
            return v
        if len(v) == 10 and _PAN_RE.match(v):
            return v
        raise ValueError("Must be a valid 15-character GSTIN or 10-character PAN")

    @field_validator("bank_ifsc")
    @classmethod
    def validate_ifsc(cls, v: str | None) -> str | None:
        if v and not _IFSC_RE.match(v):
            raise ValueError("IFSC must be 11 characters, e.g. HDFC0001234")
        return v

    @field_validator("pin_code")
    @classmethod
    def validate_pin(cls, v: str | None) -> str | None:
        if v and not _PIN_RE.match(v):
            raise ValueError("PIN / ZIP code must be 5 to 10 digits")
        return v

    @field_validator(
        "phone", "primary_mobile", "alternate_mobile", "landline",
        "customer_support_number", "whatsapp_number", "auth_person_mobile",
    )
    @classmethod
    def validate_phone(cls, v: str | None) -> str | None:
        if v and not _PHONE_RE.match(v.strip()):
            raise ValueError("Enter a valid phone number, with country code where applicable")
        return v


class UploadResponse(BaseModel):
    url: str


class CompanyOptions(BaseModel):
    """Dropdown data for the Company Master form."""

    business_types: list[str]
    industries: list[str]
    currencies: list[str]
    time_zones: list[str]
    languages: list[str]
    countries: list[str]
    states: list[str]
    bank_names: list[str]
    company_statuses: list[str]
    number_series: dict[str, str]        # series -> the prefix in use for this firm
    customer_statuses: list[str]
    customer_types: list[str]
    sales_types: list[str]
    sales_statuses: list[str]
    order_statuses: list[str]
    invoice_statuses: list[str]
    invoice_payment_statuses: list[str]
    purchase_types: list[str]
    purchase_statuses: list[str]
    receiving_statuses: list[str]
    purchase_payment_statuses: list[str]
    expense_types: list[str]
    expense_statuses: list[str]
    expense_payment_statuses: list[str]
    approval_statuses: list[str]
    return_types: list[str]
    return_statuses: list[str]
    payment_methods: list[str]
    units_of_measure: list[str]


class CompanyCompleteness(BaseModel):
    """Which of the sheet's mandatory Company Master fields are still blank.

    Reported rather than enforced on write: PUT /settings is a partial update, so
    demanding all of them on every call would make editing one field impossible.
    """

    complete: bool
    filled: int
    total: int
    missing: list[str]
    required: list[str]


class FieldSettingsOut(BaseModel):
    field_settings: dict[str, dict[str, bool]]
    available_fields: dict[str, dict[str, list[str]]]


class FieldSettingsUpdate(BaseModel):
    field_settings: dict[str, dict[str, bool]]


