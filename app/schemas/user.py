from datetime import datetime
from enum import Enum
from typing import Annotated

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    EmailStr,
    Field,
    model_validator,
)

from app.core.permissions import DEFAULT_DATA_SCOPE
from app.models.enums import UserRole
from app.schemas.role import DataScopeOut


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


class AccountStatus(str, Enum):
    """The login account's state. Richer than the `is_active` boolean, which is
    what the login check reads — setting this keeps `is_active` in sync
    (only ACTIVE can log in)."""

    ACTIVE = "active"
    INACTIVE = "inactive"
    SUSPENDED = "suspended"
    LOCKED = "locked"


class EmployeeDocumentCollection(str, Enum):
    """The employee's many-file slots. Written through the `documents` section of
    PATCH /users/{id}; named here so one file can be dropped from a list with
    DELETE /users/{id}/documents/{collection}.

    `other_documents` is the profile's name for the slot whose column is called
    `uploaded_documents`; both spellings work in the path."""

    other_documents = "other_documents"
    uploaded_documents = "uploaded_documents"
    experience_certificates = "experience_certificates"
    educational_certificates = "educational_certificates"


# The column behind each collection.
COLLECTION_COLUMN = {
    EmployeeDocumentCollection.other_documents: "uploaded_documents",
    EmployeeDocumentCollection.uploaded_documents: "uploaded_documents",
    EmployeeDocumentCollection.experience_certificates: "experience_certificates",
    EmployeeDocumentCollection.educational_certificates: "educational_certificates",
}


def _normalize_choice(value: object) -> object:
    """Accept "Full Time" / "Full-time" / "FULL_TIME" for the snake_case enums."""
    if isinstance(value, str):
        return value.strip().lower().replace(" ", "_").replace("-", "_")
    return value


def _as_list(value: object) -> object:
    """JSON columns added to an already-populated DB come back null — read those as []."""
    return value or []


EmploymentTypeIn = Annotated[EmploymentType, BeforeValidator(_normalize_choice)]
EmployeeStatusIn = Annotated[EmployeeStatus, BeforeValidator(_normalize_choice)]
AccountStatusIn = Annotated[AccountStatus, BeforeValidator(_normalize_choice)]
StringList = Annotated[list[str], BeforeValidator(_as_list)]


class RoleBrief(BaseModel):
    """The role as it appears nested inside an employee.

    `workspace` is what the Staff Detail page switches on to pick a layout — read
    that, not the role's name, since a firm can call its sales role anything.
    """

    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    workspace: str | None = None
    data_scope: DataScopeOut = DEFAULT_DATA_SCOPE
    is_default: bool
    permissions: dict[str, dict[str, bool]]


class EmployeeDocument(BaseModel):
    """One file in an employee's multi-file document slots."""

    id: str
    name: str
    url: str
    content_type: str | None = None
    size: int | None = None
    uploaded_at: datetime | None = None


DocumentList = Annotated[list[EmployeeDocument], BeforeValidator(_as_list)]


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
    google_id: str | None = None
    is_active: bool
    created_at: datetime

    # Employee profile. The choice fields are typed as plain strings on the way
    # out so rows written before these choices were validated still serialize.
    employee_id: str | None = None

    # 1. Basic information
    first_name: str | None = None
    last_name: str | None = None
    display_name: str | None = None
    gender: str | None = None
    date_of_birth: datetime | None = None
    marital_status: str | None = None
    blood_group: str | None = None
    nationality: str | None = None

    # 2. Contact information
    alternate_mobile_number: str | None = None
    personal_email: str | None = None
    emergency_contact_name: str | None = None
    emergency_contact_number: str | None = None
    emergency_contact_relationship: str | None = None

    # 3. Address information
    current_address: str | None = None
    permanent_address: str | None = None
    city: str | None = None
    state: str | None = None
    country: str | None = None
    pin_zip_code: str | None = None

    # 4. Employment information
    designation: str | None = None
    reporting_manager_id: str | None = None
    employment_type: str | None = None
    date_of_joining: datetime | None = None
    date_of_exit: datetime | None = None
    work_location: str | None = None
    shift: str | None = None
    employee_status: str | None = None

    # 6. Payroll information
    basic_salary: float | None = None
    bank_name: str | None = None
    account_number: str | None = None
    ifsc_swift_code: str | None = None
    account_holder_name: str | None = None
    upi_id: str | None = None

    # 7. Uploads
    profile_photo: str | None = None
    identity_proof_type: str | None = None
    identity_proof_file: str | None = None
    identify_proofs: str | None = None
    resume_cv: str | None = None
    offer_letter: str | None = None
    appointment_letter: str | None = None
    uploaded_documents: DocumentList = Field(default_factory=list)
    experience_certificates: DocumentList = Field(default_factory=list)
    educational_certificates: DocumentList = Field(default_factory=list)
    skills: StringList = Field(default_factory=list)

    # 8. System preferences
    language: str | None = None
    time_zone: str | None = None
    status: str | None = None

    @model_validator(mode="after")
    def _status_fallback(self) -> "UserOut":
        """Rows created before `status` existed report one derived from `is_active`,
        so clients never have to handle a null account status."""
        if self.status is None:
            self.status = (AccountStatus.ACTIVE if self.is_active else AccountStatus.INACTIVE).value
        return self


class AdminResetPassword(BaseModel):
    new_password: str = Field(min_length=8, max_length=128)
