from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class LeaveUserBrief(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    email: str | None = None


class LeaveCreate(BaseModel):
    leave_type: str = Field(default="casual", description="casual | sick | annual | unpaid | maternity | paternity | other")
    start_date: date
    end_date: date
    reason: str | None = None

    @model_validator(mode="after")
    def validate_dates(self) -> "LeaveCreate":
        if self.start_date > self.end_date:
            raise ValueError("start_date cannot be after end_date")
        return self


class LeaveUpdate(BaseModel):
    leave_type: str | None = None
    start_date: date | None = None
    end_date: date | None = None
    reason: str | None = None

    @model_validator(mode="after")
    def validate_dates(self) -> "LeaveUpdate":
        if self.start_date and self.end_date and self.start_date > self.end_date:
            raise ValueError("start_date cannot be after end_date")
        return self


class LeaveRejectBody(BaseModel):
    reject_reason: str = Field(min_length=1, max_length=500, description="Reason for rejection")


class LeaveOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    organization_id: str
    user_id: str
    user: LeaveUserBrief | None = None
    leave_type: str
    start_date: date
    end_date: date
    days_count: float
    reason: str | None = None
    status: str
    approved_by: str | None = None
    approver: LeaveUserBrief | None = None
    reject_reason: str | None = None
    created_at: datetime
    updated_at: datetime
