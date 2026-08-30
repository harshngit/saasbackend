from datetime import datetime
from pydantic import BaseModel, ConfigDict, Field, model_validator
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


class LeadBase(BaseModel):
    lead_id: str | None = None
    name: str | None = None
    contact_person: str | None = None
    mobile_number: str | None = None
    mobile: str | None = None
    email: str | None = None
    lead_source: str | None = None
    source: str | None = None
    interested_product: str | None = None
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
    pass


class LeadUpdate(BaseModel):
    lead_id: str | None = None
    name: str | None = None
    contact_person: str | None = None
    mobile_number: str | None = None
    mobile: str | None = None
    email: str | None = None
    lead_source: str | None = None
    source: str | None = None
    interested_product: str | None = None
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
