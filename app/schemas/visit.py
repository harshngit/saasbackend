from datetime import datetime
from pydantic import BaseModel, ConfigDict, Field, model_validator
from app.schemas.follow_up import FollowUpOut


class VisitCustomerBrief(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    name: str
    business_name: str | None = None
    phone: str | None = None


class VisitUserBrief(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    name: str
    email: str


class VisitLeadBrief(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    name: str | None = None
    contact_person: str | None = None


class VisitBase(BaseModel):
    customer_id: str | None = None
    lead_id: str | None = None
    user_id: str | None = None
    salesperson_id: str | None = None
    visit_date: datetime | None = None
    visit_type: str = "meeting"
    purpose: str | None = None
    notes: str | None = None
    outcome: str | None = None
    status: str = "planned"
    location: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _unify_aliases(cls, data: object) -> object:
        if isinstance(data, dict):
            if "salesperson_id" in data and "user_id" not in data:
                data["user_id"] = data["salesperson_id"]
            if "sales_officer_id" in data and "user_id" not in data:
                data["user_id"] = data["sales_officer_id"]
        return data

    @model_validator(mode="after")
    def _validate_ids(self) -> "VisitBase":
        if not self.customer_id and not self.lead_id:
            raise ValueError("At least one of customer_id or lead_id must be provided")
        return self


class VisitCreate(VisitBase):
    pass


class VisitUpdate(BaseModel):
    customer_id: str | None = None
    lead_id: str | None = None
    user_id: str | None = None
    salesperson_id: str | None = None
    visit_date: datetime | None = None
    visit_type: str | None = None
    purpose: str | None = None
    notes: str | None = None
    outcome: str | None = None
    status: str | None = None
    location: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _unify_aliases(cls, data: object) -> object:
        if isinstance(data, dict):
            if "salesperson_id" in data and "user_id" not in data:
                data["user_id"] = data["salesperson_id"]
            if "sales_officer_id" in data and "user_id" not in data:
                data["user_id"] = data["sales_officer_id"]
        return data


class VisitOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    organization_id: str
    customer_id: str | None = None
    lead_id: str | None = None
    user_id: str | None = None
    visit_date: datetime
    visit_type: str
    purpose: str | None = None
    notes: str | None = None
    outcome: str | None = None
    status: str
    location: str | None = None
    created_at: datetime
    updated_at: datetime

    customer: VisitCustomerBrief | None = None
    lead: VisitLeadBrief | None = None
    user: VisitUserBrief | None = None
    follow_ups: list[FollowUpOut] = Field(default_factory=list)
