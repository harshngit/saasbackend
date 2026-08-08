"""The sectioned Customer Profile — request and response.

The API speaks in the ten sections the field sheet uses, while the database
stays flat (one column per field, so it is still queryable and indexable). The
mapping lives in `SECTION_FIELDS`: nothing else needs to know both shapes.

Flat bodies are still accepted on input, so callers written against the older
shape keep working — see `CustomerProfileIn.fold_flat_keys`.
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.core.reference_data import CUSTOMER_STATUSES, CUSTOMER_TYPES
from app.schemas.choices import CustomerStatus, CustomerType

# API field name -> column name, for the handful that differ.
ALIASES = {
    "customer_name": "name",
    "customer_category": "category",
    "mobile_number": "phone",
    "email_address": "email",
    "shipping_address": "delivery_address",
    "gstin_tax_id": "gst_number",
    "sales_representative_id": "assigned_sales_officer_id",
}
COLUMN_FOR = ALIASES
FIELD_FOR = {v: k for k, v in ALIASES.items()}

SECTION_FIELDS: dict[str, list[str]] = {
    "basic_information": [
        "customer_type", "customer_name", "legal_business_name", "display_name",
        "industry", "customer_category", "customer_since", "status", "profile_image_id",
    ],
    "contact_information": [
        "primary_contact_person", "designation", "mobile_number",
        "alternate_mobile_number", "email_address", "website",
    ],
    "address_information": [
        "billing_address", "shipping_address", "city", "state", "country", "pin_zip_code",
    ],
    "business_tax_information": [
        "gstin_tax_id", "pan_business_registration_no", "tax_exempt", "tax_category", "currency",
    ],
    "payment_information": [
        "payment_terms", "credit_limit", "opening_balance", "preferred_payment_method",
        "upi_id", "bank_name", "account_number", "ifsc_swift_code",
    ],
    "sales_crm_information": [
        "sales_representative_id", "lead_source", "territory", "customer_priority",
        "preferred_communication", "customer_tags",
    ],
    "social_media_online_presence": [
        "facebook_url", "instagram_url", "linkedin_url", "x_twitter_url", "youtube_url",
    ],
    "additional_information": [
        "date_of_birth", "anniversary_date", "referral_customer_id", "loyalty_number", "notes",
    ],
    "preferences": [
        "portal_access_enabled", "favorite_product_ids", "default_price_list_id",
        "default_tax_profile_id", "default_warehouse_id", "preferred_invoice_delivery",
    ],
}


class GeoLocation(BaseModel):
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)


class BasicInformation(BaseModel):
    customer_type: CustomerType | None = Field(
        default=None, description=" | ".join(CUSTOMER_TYPES))
    customer_name: str | None = Field(default=None, max_length=150)
    legal_business_name: str | None = Field(default=None, max_length=200)
    display_name: str | None = Field(default=None, max_length=200)
    industry: str | None = Field(default=None, max_length=100)
    customer_category: str | None = Field(default=None, max_length=100)
    customer_since: datetime | None = None
    status: CustomerStatus | None = Field(default=None, description=" | ".join(CUSTOMER_STATUSES))
    profile_image_id: str | None = Field(default=None, description="URL from POST /files/upload")


class ContactInformation(BaseModel):
    primary_contact_person: str | None = Field(default=None, max_length=150)
    designation: str | None = Field(default=None, max_length=100)
    mobile_number: str | None = Field(default=None, max_length=20)
    alternate_mobile_number: str | None = Field(default=None, max_length=20)
    email_address: str | None = Field(default=None, max_length=255)
    website: str | None = Field(default=None, max_length=200)


class AddressInformation(BaseModel):
    billing_address: str | None = None
    shipping_address: str | None = None
    city: str | None = Field(default=None, max_length=100)
    state: str | None = Field(default=None, max_length=100)
    country: str | None = Field(default=None, max_length=100)
    pin_zip_code: str | None = Field(default=None, max_length=10)
    google_maps_location: GeoLocation | None = None


class BusinessTaxInformation(BaseModel):
    gstin_tax_id: str | None = Field(default=None, max_length=20)
    pan_business_registration_no: str | None = Field(default=None, max_length=30)
    tax_exempt: bool = False
    tax_category: str | None = Field(default=None, max_length=50)
    currency: str | None = Field(default=None, max_length=10)


class PaymentInformation(BaseModel):
    payment_terms: str | None = Field(default=None, max_length=50, description="e.g. net_30")
    credit_limit: float | None = Field(default=None, ge=0)
    opening_balance: float | None = None
    preferred_payment_method: str | None = Field(default=None, max_length=50)
    upi_id: str | None = Field(default=None, max_length=100)
    bank_name: str | None = Field(default=None, max_length=150)
    account_number: str | None = Field(default=None, max_length=50)
    ifsc_swift_code: str | None = Field(default=None, max_length=30)


class NamedRef(BaseModel):
    """A related record resolved from its id, so the UI never looks the name up."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str


class SalesCrmInformation(BaseModel):
    sales_representative_id: str | None = None
    # Read-only, resolved from sales_representative_id.
    sales_representative: NamedRef | None = None
    lead_source: str | None = Field(default=None, max_length=50)
    territory: str | None = Field(default=None, max_length=100)
    customer_priority: str | None = Field(default=None, max_length=20, description="high | medium | low")
    preferred_communication: list[str] = Field(default_factory=list)
    customer_tags: list[str] = Field(default_factory=list)


class SocialMediaOnlinePresence(BaseModel):
    facebook_url: str | None = Field(default=None, max_length=200)
    instagram_url: str | None = Field(default=None, max_length=200)
    linkedin_url: str | None = Field(default=None, max_length=200)
    x_twitter_url: str | None = Field(default=None, max_length=200)
    youtube_url: str | None = Field(default=None, max_length=200)


class AdditionalInformation(BaseModel):
    date_of_birth: datetime | None = None
    anniversary_date: datetime | None = None
    referral_customer_id: str | None = None
    loyalty_number: str | None = Field(default=None, max_length=50)
    notes: str | None = None


class Preferences(BaseModel):
    portal_access_enabled: bool = False
    favorite_product_ids: list[str] = Field(default_factory=list)
    default_price_list_id: str | None = None
    default_tax_profile_id: str | None = None
    default_warehouse_id: str | None = None
    preferred_invoice_delivery: list[str] = Field(default_factory=list)


class DocumentsSection(BaseModel):
    """Read-only. Filled from the customer's uploaded documents, so the ids here
    are always real files — uploading is done through /customers/{id}/documents."""

    gst_certificate_id: str | None = None
    pan_card_id: str | None = None
    business_registration_certificate_id: str | None = None
    address_proof_id: str | None = None
    purchase_agreement_id: str | None = None
    other_document_ids: list[str] = Field(default_factory=list)


class FinancialSummary(BaseModel):
    opening_balance: float = 0
    total_billed: float = 0
    total_received: float = 0
    outstanding_balance: float = 0
    credit_limit: float = 0
    available_credit: float = 0


class SalesSummary(BaseModel):
    total_orders: int = 0
    total_purchases: float = 0
    last_purchase_date: datetime | None = None
    customer_lifetime_value: float = 0


class CustomerProfileIn(BaseModel):
    """Create / update body. Every section is optional, and within a section every
    field is optional, so a partial update can send just the section it touches."""

    model_config = ConfigDict(extra="ignore")

    basic_information: BasicInformation | None = None
    contact_information: ContactInformation | None = None
    address_information: AddressInformation | None = None
    business_tax_information: BusinessTaxInformation | None = None
    payment_information: PaymentInformation | None = None
    sales_crm_information: SalesCrmInformation | None = None
    social_media_online_presence: SocialMediaOnlinePresence | None = None
    additional_information: AdditionalInformation | None = None
    preferences: Preferences | None = None
    # Accepted so a round-tripped profile does not 422, but ignored: documents
    # are created by uploading them.
    documents: DocumentsSection | None = Field(default=None, deprecated=True)

    _SECTION_MODELS = {
        "basic_information": BasicInformation,
        "contact_information": ContactInformation,
        "address_information": AddressInformation,
        "business_tax_information": BusinessTaxInformation,
        "payment_information": PaymentInformation,
        "sales_crm_information": SalesCrmInformation,
        "social_media_online_presence": SocialMediaOnlinePresence,
        "additional_information": AdditionalInformation,
        "preferences": Preferences,
    }

    @model_validator(mode="before")
    @classmethod
    def fold_flat_keys(cls, data: object) -> object:
        """Accept a flat body as well as a sectioned one.

        Callers written against the older flat shape (`{"name": …, "phone": …}`)
        keep working: any top-level key that belongs to a section is folded into
        it. Explicit sections always win over a flat key for the same field.
        """
        if not isinstance(data, dict):
            return data
        folded = {k: v for k, v in data.items() if k in cls.model_fields}
        for section, fields in SECTION_FIELDS.items():
            picked = {}
            for field in fields:
                # A flat body may use either the API name or the column name.
                for key in (field, COLUMN_FOR.get(field)):
                    if key and key in data and key not in cls.model_fields:
                        picked[field] = data[key]
                        break
            if picked:
                existing = folded.get(section) or {}
                if isinstance(existing, BaseModel):
                    existing = existing.model_dump(exclude_unset=True)
                folded[section] = {**picked, **existing}
        if "google_maps_location" not in data and (
            "maps_latitude" in data or "maps_longitude" in data
        ):
            addr = folded.get("address_information") or {}
            addr["google_maps_location"] = {
                "latitude": data.get("maps_latitude"),
                "longitude": data.get("maps_longitude"),
            }
            folded["address_information"] = addr
        return folded

    def to_columns(self) -> dict:
        """Flatten the sections into the column names the model stores."""
        values: dict = {}
        for section in SECTION_FIELDS:
            block = getattr(self, section, None)
            if block is None:
                continue
            for field, value in block.model_dump(exclude_unset=True).items():
                values[COLUMN_FOR.get(field, field)] = value
        # The map pin is nested in the API but two plain columns in the table.
        values.pop("google_maps_location", None)
        address = self.address_information
        if address is not None and address.google_maps_location is not None:
            values["maps_latitude"] = address.google_maps_location.latitude
            values["maps_longitude"] = address.google_maps_location.longitude
        return values


class CustomerProfileOut(BaseModel):
    """The full 360° customer profile."""

    id: str
    customer_id: str | None = None
    organization_id: str

    basic_information: BasicInformation
    contact_information: ContactInformation
    address_information: AddressInformation
    business_tax_information: BusinessTaxInformation
    payment_information: PaymentInformation
    sales_crm_information: SalesCrmInformation
    social_media_online_presence: SocialMediaOnlinePresence
    documents: DocumentsSection
    additional_information: AdditionalInformation
    preferences: Preferences

    financial_summary: FinancialSummary
    sales_summary: SalesSummary

    is_active: bool = True
    created_at: datetime
    updated_at: datetime
