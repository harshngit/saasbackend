from datetime import datetime
from pydantic import BaseModel, ConfigDict, Field, model_validator
from app.schemas.choices import LeadSource
from app.schemas.customer import CustomerOut


class LeadCustomerBrief(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    name: str
    business_name: str | None = None
    customer_id: str | None = None


class LeadSalespersonBrief(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    name: str
    email: str


class LeadProductBrief(BaseModel):
    """Lightweight Product identity for a Lead's interested-products list —
    same "brief" pattern as LeadCustomerBrief/QuotationCustomerBrief."""

    model_config = ConfigDict(from_attributes=True)
    id: str
    name: str
    sku: str | None = None


class LeadBase(BaseModel):
    lead_id: str | None = None
    name: str | None = None
    contact_person: str | None = None
    mobile_number: str | None = None
    mobile: str | None = None
    email: str | None = None
    lead_source: str | None = None
    source: str | None = None
    # Legacy free-text field, kept fully functional. interested_product_ids
    # (below) is the normalized replacement — the two are independent.
    interested_product: str | None = None
    # Frontend-defined categorization (not a fixed backend taxonomy) and
    # commercial-size classification — separate concepts from lead_status
    # and unrelated to Customer.customer_type.
    lead_type: str | None = None
    segment: str | None = None
    interested_product_ids: list[str] | None = Field(
        default=None,
        description="Product IDs this Lead is interested in. On update, sending "
                    "this replaces the entire existing set (send [] to clear it "
                    "entirely). All IDs must belong to the caller's organization.",
    )
    notes: str | None = None
    customer_id: str | None = None
    converted_customer_id: str | None = None
    assigned_salesperson_id: str | None = None
    assigned_sales_officer_id: str | None = None
    lead_status: str | None = "new"
    status: str | None = None
    converted_at: datetime | None = None

    @model_validator(mode="before")
    @classmethod
    def _unify_aliases(cls, data: object) -> object:
        if isinstance(data, dict):
            if "mobile" in data and "mobile_number" not in data:
                data["mobile_number"] = data["mobile"]
            if "source" in data and "lead_source" not in data:
                data["lead_source"] = data["source"]
            if "status" in data and "lead_status" not in data:
                data["lead_status"] = data["status"]
            if "assigned_sales_officer_id" in data and "assigned_salesperson_id" not in data:
                data["assigned_salesperson_id"] = data["assigned_sales_officer_id"]
            if "converted_customer_id" in data and "customer_id" not in data:
                data["customer_id"] = data["converted_customer_id"]
        return data


class LeadCreate(LeadBase):
    """Required to create a Lead: name, lead_source (or its alias `source`),
    and mobile_number (or its alias `mobile`). `_unify_aliases` above (run as
    a `mode="before"` validator, inherited from LeadBase) fills the canonical
    field in from its alias before these are checked, so either spelling
    satisfies the requirement.

    The backend owns the initial workflow state: whatever `status`/
    `lead_status` the client sends, a new Lead always starts as 'new' — a
    client cannot create a Lead directly as contacted/qualified/won/lost.
    """

    name: str = Field(min_length=1)
    mobile_number: str = Field(min_length=1)
    lead_source: LeadSource

    @model_validator(mode="after")
    def _initial_status_is_new(self) -> "LeadCreate":
        if self.lead_status not in (None, "new"):
            raise ValueError(
                "A new Lead always starts as 'new' — status cannot be set during creation"
            )
        self.lead_status = "new"
        return self


class LeadUpdate(BaseModel):
    lead_id: str | None = None
    name: str | None = None
    contact_person: str | None = None
    mobile_number: str | None = None
    mobile: str | None = None
    email: str | None = None
    lead_source: LeadSource | None = None
    source: str | None = None
    interested_product: str | None = None
    lead_type: str | None = None
    segment: str | None = None
    interested_product_ids: list[str] | None = Field(
        default=None,
        description="Replaces the entire existing interested-product set when sent "
                    "(send [] to clear it entirely). All IDs must belong to the "
                    "caller's organization.",
    )
    notes: str | None = None
    customer_id: str | None = None
    converted_customer_id: str | None = None
    assigned_salesperson_id: str | None = None
    assigned_sales_officer_id: str | None = None
    lead_status: str | None = None
    status: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _unify_aliases(cls, data: object) -> object:
        if isinstance(data, dict):
            if "mobile" in data and "mobile_number" not in data:
                data["mobile_number"] = data["mobile"]
            if "source" in data and "lead_source" not in data:
                data["lead_source"] = data["source"]
            if "status" in data and "lead_status" not in data:
                data["lead_status"] = data["status"]
            if "assigned_sales_officer_id" in data and "assigned_salesperson_id" not in data:
                data["assigned_salesperson_id"] = data["assigned_sales_officer_id"]
            if "converted_customer_id" in data and "customer_id" not in data:
                data["customer_id"] = data["converted_customer_id"]
        return data


class LeadOut(LeadBase):
    model_config = ConfigDict(from_attributes=True)

    id: str
    organization_id: str
    created_at: datetime
    updated_at: datetime

    customer: LeadCustomerBrief | None = None
    assigned_salesperson: LeadSalespersonBrief | None = None
    interested_products: list[LeadProductBrief] = []


class LeadConvertToCustomerIn(BaseModel):
    name: str | None = None
    business_name: str | None = None
    phone: str | None = None
    email: str | None = None
    gst_number: str | None = None
    billing_address: str | None = None
    delivery_address: str | None = None
    assigned_sales_officer_id: str | None = None
    credit_limit: float | None = None
    opening_balance: float | None = None
    category: str | None = None
    notes: str | None = None
    primary_contact_person: str | None = None
    customer_type: str | None = None
    customer_since: datetime | None = None
    status: str | None = None
    maps_latitude: float | None = None
    maps_longitude: float | None = None


class LeadConvertResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    lead_id: str
    customer_id: str
    lead_status: str
    converted: bool = True
    customer: CustomerOut | None = None
