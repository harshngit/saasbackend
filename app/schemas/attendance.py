from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, field_validator

from app.models import ATTENDANCE_TYPES


class CheckInBody(BaseModel):
    type: str

    @field_validator("type")
    @classmethod
    def _valid(cls, v: str) -> str:
        if v not in ATTENDANCE_TYPES:
            raise ValueError(f"type must be one of {ATTENDANCE_TYPES}")
        return v


class AttendanceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    user_id: str
    date: date
    office_check_in: datetime | None
    departure: datetime | None
    return_to_office: datetime | None
    final_check_out: datetime | None
