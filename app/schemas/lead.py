from datetime import datetime
from pydantic import BaseModel, ConfigDict


class LeadCustomerBrief(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    name: str
    business_name: str | None = None


class LeadSalespersonBrief(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    name: str
    email: str


class LeadBase(BaseModel):
    lead_id: str | None = None
    lead_source: str | None = None
    customer_id: str | None = None
    mobile_number: str | None = None
    assigned_salesperson_id: str | None = None
    lead_status: str | None = "new"


class LeadCreate(LeadBase):
    pass


class LeadUpdate(BaseModel):
    lead_id: str | None = None
    lead_source: str | None = None
    customer_id: str | None = None
    mobile_number: str | None = None
    assigned_salesperson_id: str | None = None
    lead_status: str | None = None


class LeadOut(LeadBase):
    model_config = ConfigDict(from_attributes=True)

    id: str
    organization_id: str
    created_at: datetime
    updated_at: datetime

    customer: LeadCustomerBrief | None = None
    assigned_salesperson: LeadSalespersonBrief | None = None
