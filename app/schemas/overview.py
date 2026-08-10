"""The Company Settings dashboard summary — everything that page's cards need in
one response, so it does not have to fan out to a dozen endpoints.

Read-only and entirely derived: nothing here is written back. Each block matches
one card on the page.
"""

from datetime import datetime

from pydantic import BaseModel, Field

# Column -> the label the "Missing Information" list shows for it.
FIELD_LABELS: dict[str, str] = {
    "name": "Company Name",
    "legal_name": "Legal Name",
    "business_type": "Company Type",
    "industry": "Industry",
    "primary_mobile": "Primary Mobile",
    "email": "Email Address",
    "registered_address": "Registered Address",
    "city": "City",
    "state": "State",
    "country": "Country",
    "pin_code": "PIN / ZIP Code",
    "logo_url": "Company Logo",
    "signature_url": "Authorized Signature",
    "payment_qr_url": "Payment QR Code",
    "currency": "Currency",
    "timezone": "Time Zone",
    "language": "Language",
    "tax_configuration": "Tax Configuration",
    "invoice_settings": "Invoice Settings",
    "auth_person_name": "Authorized Person Name",
    "auth_person_designation": "Authorized Person Designation",
    "auth_person_mobile": "Authorized Person Mobile",
    "auth_person_email": "Authorized Person Email",
    "company_status": "Company Status",
}

# The single-file business document slots, in the order the page lists them, with
# the organization column behind each and the label to show.
DOCUMENT_SLOTS: list[tuple[str, str, str]] = [
    ("gst_certificate", "GST Certificate", "doc_gst_url"),
    ("pan_card", "PAN Card", "doc_pan_url"),
    ("certificate_of_incorporation", "Certificate of Incorporation", "doc_coi_url"),
    ("trade_license", "Trade License", "doc_trade_license_url"),
    ("msme_certificate", "MSME Certificate", "doc_msme_url"),
    ("fssai_license", "FSSAI License", "doc_fssai_url"),
]


class OverviewPlan(BaseModel):
    """The firm's subscription, as the header badge and the Plan column show it."""

    id: str | None = None
    name: str | None = None
    price_monthly: float | None = None
    price_yearly: float | None = None
    billing_cycle: str | None = None
    max_users: int | None = Field(default=None, description="null = unlimited")
    max_storage_gb: float | None = Field(default=None, description="null = unlimited")
    subscription_status: str = Field(description="trial | active | locked | inactive | suspended")
    trial_ends_at: datetime | None = None
    trial_days_left: int | None = None
    upgrade_status: str | None = None


class OverviewCompany(BaseModel):
    """The header strip: who this firm is."""

    id: str
    name: str
    company_code: str | None = None
    legal_name: str | None = None
    industry: str | None = None
    company_type: str | None = Field(default=None, description="From business_type")
    registration_date: str | None = Field(
        default=None, description="From date_of_incorporation"
    )
    company_status: str | None = Field(default=None, description="active | inactive")
    logo_url: str | None = None
    gst_number: str | None = None
    pan_number: str | None = None
    created_at: datetime
    plan: OverviewPlan


class OverviewCounts(BaseModel):
    """The stat tiles."""

    employees: int = Field(description="Every employee record in the firm")
    active_users: int = Field(description="Employees whose account can log in")
    branches: int = Field(description="Entries in branch_addresses")
    documents: int = Field(description="Business documents on file, named slots + other")


class OverviewStorage(BaseModel):
    """The Storage Used tile. Counts every file uploaded by this firm."""

    files: int
    used_bytes: int
    used_mb: float
    used_gb: float
    limit_gb: float | None = Field(default=None, description="From the plan; null = unlimited")
    percent_used: float | None = Field(default=None, description="null when unlimited")


class OverviewProfileCompletion(BaseModel):
    """The Profile Completion ring and its "Missing Information" list."""

    percent: int
    filled: int
    total: int
    is_complete: bool
    missing_information: list[str] = Field(description="Human labels, for display")
    missing_fields: list[str] = Field(description="The same items as column names")


class OverviewAuthorizedPerson(BaseModel):
    """The Authorized Person card. `is_complete` is what the badge reads from —
    every field filled in, not an externally verified identity."""

    name: str | None = None
    designation: str | None = None
    email: str | None = None
    mobile: str | None = None
    photo_url: str | None = None
    signature_url: str | None = None
    is_complete: bool


class OverviewDocument(BaseModel):
    """One row of the Documents Overview card.

    `status` is `uploaded` when a file is on file and `pending` when the slot is
    empty. Expiry is not tracked yet, so `expires_at` is always null and nothing
    ever reports `expired`; wire those up here when the upload starts capturing a
    validity date.
    """

    key: str
    name: str
    status: str = Field(description="uploaded | pending")
    url: str | None = None
    size: int | None = None
    uploaded_at: datetime | None = Field(
        default=None, description="Known for `other` documents only"
    )
    expires_at: datetime | None = None


class OverviewDocuments(BaseModel):
    total: int
    uploaded: int
    pending: int
    items: list[OverviewDocument]


class OverviewAddress(BaseModel):
    """One entry of the Company Addresses card."""

    id: str | None = None
    type: str = Field(description="registered_office | branch")
    label: str | None = None
    is_primary: bool = False
    address: str | None = None
    city: str | None = None
    state: str | None = None
    country: str | None = None
    pin_code: str | None = None
    latitude: float | None = None
    longitude: float | None = None


class OverviewActivity(BaseModel):
    """One entry of the Recent Activity card."""

    id: str
    type: str
    title: str
    description: str | None = None
    at: datetime
    by: str | None = Field(default=None, description="Who made the change")


class CompanyOverviewOut(BaseModel):
    company: OverviewCompany
    counts: OverviewCounts
    storage: OverviewStorage
    profile_completion: OverviewProfileCompletion
    authorized_person: OverviewAuthorizedPerson
    documents: OverviewDocuments
    addresses: list[OverviewAddress]
    recent_activity: list[OverviewActivity]
