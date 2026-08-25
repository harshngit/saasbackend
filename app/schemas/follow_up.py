from datetime import datetime
from pydantic import BaseModel, ConfigDict, Field, model_validator


class FollowUpCustomerBrief(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    name: str
    business_name: str | None = None
    phone: str | None = None


class FollowUpUserBrief(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    name: str
    email: str


class FollowUpBase(BaseModel):
    customer_id: str | None = None
    visit_id: str | None = None
    assigned_to_id: str | None = None
    title: str
    description: str | None = None
    due_date: datetime
    priority: str = "medium"
    status: str = "pending"
    completed_at: datetime | None = None


class FollowUpCreate(BaseModel):
    customer_id: str | None = None
    visit_id: str | None = None
    assigned_to_id: str | None = None
    title: str
    description: str | None = None
    due_date: datetime
    priority: str = "medium"
    status: str = "pending"


class FollowUpUpdate(BaseModel):
    customer_id: str | None = None
    visit_id: str | None = None
    assigned_to_id: str | None = None
    title: str | None = None
    description: str | None = None
    due_date: datetime | None = None
    priority: str | None = None
    status: str | None = None
    completed_at: datetime | None = None


class FollowUpOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    organization_id: str
    customer_id: str
    visit_id: str | None = None
    assigned_to_id: str | None = None
    title: str
    description: str | None = None
    due_date: datetime
    priority: str
    status: str
    completed_at: datetime | None = None
    created_at: datetime
    updated_at: datetime

    customer: FollowUpCustomerBrief | None = None
    assigned_to: FollowUpUserBrief | None = None
