from datetime import datetime
from enum import Enum
from typing import Annotated

from pydantic import BaseModel, BeforeValidator, ConfigDict, EmailStr, Field, model_validator

from app.models.enums import UserRole


class EmploymentType(str, Enum):
    """How the employee is engaged by the firm."""

    FULL_TIME = "full_time"
    PART_TIME = "part_time"
    CONTRACT = "contract"
    INTERN = "intern"
    TEMPORARY = "temporary"


class EmployeeStatus(str, Enum):
    """Where the employee is in their employment lifecycle (distinct from
    `is_active`, which controls whether they can log in)."""

    ACTIVE = "active"
    PROBATION = "probation"
    ON_LEAVE = "on_leave"
    NOTICE_PERIOD = "notice_period"
    RESIGNED = "resigned"
    TERMINATED = "terminated"


def _normalize_choice(value: object) -> object:
    """Accept "Full Time" / "Full-time" / "FULL_TIME" for the snake_case enums."""
    if isinstance(value, str):
        return value.strip().lower().replace(" ", "_").replace("-", "_")
    return value


EmploymentTypeIn = Annotated[EmploymentType, BeforeValidator(_normalize_choice)]
EmployeeStatusIn = Annotated[EmployeeStatus, BeforeValidator(_normalize_choice)]


class RoleBrief(BaseModel):
    """Compact role info nested inside a user (name + permission matrix)."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    is_default: bool
    permissions: dict[str, dict[str, bool]]


class EmployeeProfileIn(BaseModel):
    """The HR-side profile carried on a user. Shared by create and update, so
    both accept exactly the same employee fields."""

    employee_id: str | None = Field(default=None, min_length=1, max_length=50)
    first_name: str | None = Field(default=None, max_length=100)
    last_name: str | None = Field(default=None, max_length=100)
    designation: str | None = Field(default=None, max_length=100)
    employment_type: EmploymentTypeIn | None = None
    date_of_joining: datetime | None = None
    employee_status: EmployeeStatusIn | None = None
    identify_proofs: str | None = None  # data: URL — set via POST /users/{id}/identity-proof


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    organization_id: str | None
    name: str
    email: EmailStr
    username: str | None
    phone: str | None
    system_role: str | None            # super_admin / admin / staff
    role_id: str | None
    role_detail: RoleBrief | None      # the staff member's role + permissions
    role: UserRole | None              # legacy fixed-role enum (kept for backward-compat)
    is_active: bool
    created_at: datetime

    # Employee profile. Typed as plain strings on the way out so rows written
    # before these choices were validated still serialize.
    employee_id: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    designation: str | None = None
    employment_type: str | None = None
    date_of_joining: datetime | None = None
    employee_status: str | None = None
    identify_proofs: str | None = None


class StaffCreate(EmployeeProfileIn):
    """Admin creates a staff user. Prefer `role_id`; `role` (legacy enum) still
    accepted for backward-compat and mapped to the org's matching default role.
    Every employee-profile field is optional — `employee_id` is auto-assigned
    (EMP-0001, EMP-0002, …) when omitted."""

    name: str = Field(min_length=1, max_length=150)
    email: EmailStr
    username: str = Field(min_length=3, max_length=50)
    phone: str | None = Field(default=None, max_length=20)
    password: str = Field(min_length=8, max_length=128)
    role_id: str | None = None
    role: UserRole | None = None

    @model_validator(mode="after")
    def _require_a_role(self) -> "StaffCreate":
        if self.role_id is None and self.role is None:
            raise ValueError("Provide role_id (preferred) or role")
        return self


class UserUpdate(EmployeeProfileIn):
    """Edit a staff member's account + employee profile (not their role — use
    /role for that, nor their login state — use /status)."""

    name: str | None = Field(default=None, min_length=1, max_length=150)
    email: EmailStr | None = None
    username: str | None = Field(default=None, min_length=3, max_length=50)
    phone: str | None = Field(default=None, max_length=20)


class RoleAssign(BaseModel):
    role_id: str


class UserStatusUpdate(BaseModel):
    is_active: bool


class AdminResetPassword(BaseModel):
    new_password: str = Field(min_length=8, max_length=128)


class EmployeeOptions(BaseModel):
    """Dropdown data for the employee form: the fixed choice lists plus the
    designations already in use in this firm."""

    employment_types: list[str]
    employee_statuses: list[str]
    designations: list[str]
