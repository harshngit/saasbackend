from datetime import datetime
from enum import Enum
from typing import Annotated

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    EmailStr,
    Field,
    field_validator,
    model_validator,
)

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


class AccountStatus(str, Enum):
    """The login account's state. Richer than the `is_active` boolean, which is
    what the login check reads — setting this keeps `is_active` in sync
    (only ACTIVE can log in)."""

    ACTIVE = "active"
    INACTIVE = "inactive"
    SUSPENDED = "suspended"
    LOCKED = "locked"


class EmployeeFileField(str, Enum):
    """Single-file slots on an employee, uploadable via POST /users/{id}/files/{field}."""

    profile_photo = "profile_photo"
    identity_proof_file = "identity_proof_file"
    resume_cv = "resume_cv"
    offer_letter = "offer_letter"
    appointment_letter = "appointment_letter"


class EmployeeDocumentCollection(str, Enum):
    """Many-file slots on an employee, managed via /users/{id}/documents/{collection}."""

    uploaded_documents = "uploaded_documents"
    experience_certificates = "experience_certificates"
    educational_certificates = "educational_certificates"


# Suggested values for the dropdown fields. Kept as suggestions rather than
# enums so a firm can enter something we did not anticipate; served by
# GET /users/meta/employee-options so the form can populate its dropdowns.
GENDERS = ["Male", "Female", "Other", "Prefer not to say"]
MARITAL_STATUSES = ["Single", "Married", "Divorced", "Widowed", "Separated"]
BLOOD_GROUPS = ["A+", "A-", "B+", "B-", "AB+", "AB-", "O+", "O-"]
IDENTITY_PROOF_TYPES = ["Aadhaar", "PAN", "Passport", "Driving Licence", "Voter ID", "Other"]
DESIGNATIONS = ["Admin", "Manager", "HR", "Sales", "Finance", "Employee"]
EMERGENCY_CONTACT_RELATIONSHIPS = [
    "Father", "Mother", "Spouse", "Sibling", "Son", "Daughter", "Friend", "Guardian", "Other",
]


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
    """Compact role info nested inside a user (name + permission matrix)."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
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


class EmployeeProfileIn(BaseModel):
    """The HR-side profile carried on a user. Shared by create and update, so both
    accept exactly the same employee fields. Everything is optional here; the
    fields the HR form treats as mandatory are re-declared as required on
    `StaffCreate` only, so editing stays a true partial update.

    The many-file slots (uploaded_documents / experience_certificates /
    educational_certificates) are absent on purpose — they are managed through
    /users/{id}/documents/{collection}, not by writing the whole list.
    """

    employee_id: str | None = Field(default=None, min_length=1, max_length=50)

    # 1. Basic information
    first_name: str | None = Field(default=None, max_length=50)
    last_name: str | None = Field(default=None, max_length=50)
    display_name: str | None = Field(default=None, max_length=150)
    gender: str | None = Field(default=None, max_length=30)
    date_of_birth: datetime | None = None
    marital_status: str | None = Field(default=None, max_length=30)
    blood_group: str | None = Field(default=None, max_length=10)
    nationality: str | None = Field(default=None, max_length=100)

    # 2. Contact information
    alternate_mobile_number: str | None = Field(default=None, max_length=20)
    personal_email: EmailStr | None = None
    emergency_contact_name: str | None = Field(default=None, max_length=150)
    emergency_contact_number: str | None = Field(default=None, max_length=20)
    emergency_contact_relationship: str | None = Field(default=None, max_length=50)

    # 3. Address information
    current_address: str | None = None
    permanent_address: str | None = None
    city: str | None = Field(default=None, max_length=100)
    state: str | None = Field(default=None, max_length=100)
    country: str | None = Field(default=None, max_length=100)
    pin_zip_code: str | None = Field(default=None, max_length=20)

    # 4. Employment information
    designation: str | None = Field(default=None, max_length=100)
    reporting_manager_id: str | None = Field(
        default=None, description="Another employee in the same firm"
    )
    employment_type: EmploymentTypeIn | None = None
    date_of_joining: datetime | None = None
    date_of_exit: datetime | None = None
    work_location: str | None = Field(default=None, max_length=150)
    shift: str | None = Field(default=None, max_length=50)
    employee_status: EmployeeStatusIn | None = None

    # 6. Payroll information
    basic_salary: float | None = Field(default=None, ge=0)
    bank_name: str | None = Field(default=None, max_length=150)
    account_number: str | None = Field(default=None, max_length=50)
    ifsc_swift_code: str | None = Field(default=None, max_length=30)
    account_holder_name: str | None = Field(default=None, max_length=150)
    upi_id: str | None = Field(default=None, max_length=100)

    # 7. Uploads — pass a URL / data: URL, or upload via /users/{id}/files/{field}
    profile_photo: str | None = None
    identity_proof_type: str | None = Field(default=None, max_length=50)
    identity_proof_file: str | None = None
    identify_proofs: str | None = Field(
        default=None, deprecated=True, description="Legacy alias of identity_proof_file"
    )
    resume_cv: str | None = None
    offer_letter: str | None = None
    appointment_letter: str | None = None
    skills: list[str] | None = None

    # 8. System preferences
    language: str | None = Field(default=None, max_length=30)
    time_zone: str | None = Field(default=None, max_length=64)
    status: AccountStatusIn | None = None

    @field_validator("reporting_manager_id", mode="before")
    @classmethod
    def _blank_to_none(cls, v: object) -> object:
        return None if v == "" else v

    @model_validator(mode="after")
    def _sync_identity_proof(self) -> "EmployeeProfileIn":
        """Whichever of the two ID-file names the caller used, fill in the other."""
        if self.identity_proof_file is None and self.identify_proofs is not None:
            self.identity_proof_file = self.identify_proofs
        elif self.identify_proofs is None and self.identity_proof_file is not None:
            self.identify_proofs = self.identity_proof_file
        return self


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


class StaffCreate(EmployeeProfileIn):
    """Admin creates a staff user. Prefer `role_id` (from GET /roles); `role` also
    accepts the role's *name* — any role the firm created on the Roles page, matched
    case/separator-insensitively, so legacy values like "sales_officer" still work.

    `name` is optional — omit it and it is composed from first + last name.
    `employee_id` is auto-assigned (EMP-0001, EMP-0002, …) using the firm's
    `employee_id_prefix` company setting. The fields re-declared below are the
    ones the HR form marks mandatory; everything else inherited from the profile
    stays optional.
    """

    name: str | None = Field(default=None, min_length=1, max_length=150)
    email: EmailStr = Field(description="Official email — the login identifier")
    username: str = Field(min_length=3, max_length=50)
    phone: str = Field(min_length=1, max_length=20, description="Primary mobile number")
    password: str = Field(min_length=8, max_length=128)
    confirm_password: str = Field(min_length=8, max_length=128)
    role_id: str | None = None
    role: str | None = Field(default=None, max_length=100)

    # Required by the employee form (see the class docstring).
    first_name: str = Field(min_length=1, max_length=50)
    last_name: str = Field(min_length=1, max_length=50)
    designation: str = Field(min_length=1, max_length=100)
    employment_type: EmploymentTypeIn
    date_of_joining: datetime
    employee_status: EmployeeStatusIn
    identity_proof_type: str = Field(min_length=1, max_length=50)
    identity_proof_file: str = Field(min_length=1, description="URL or base64 data: URL")
    status: AccountStatusIn

    @model_validator(mode="after")
    def _check_create(self) -> "StaffCreate":
        if self.role_id is None and self.role is None:
            raise ValueError("Provide role_id (preferred) or role")
        if self.password != self.confirm_password:
            raise ValueError("password and confirm_password do not match")
        if self.name is None:
            self.name = f"{self.first_name} {self.last_name}".strip()
        return self


class UserUpdate(EmployeeProfileIn):
    """Edit a staff member's account + employee profile (not their role — use
    /role for that, nor their login state — use /status). Partial: only the
    fields you send are written."""

    name: str | None = Field(default=None, min_length=1, max_length=150)
    email: EmailStr | None = None
    username: str | None = Field(default=None, min_length=3, max_length=50)
    phone: str | None = Field(default=None, max_length=20)


class RoleAssign(BaseModel):
    """Reassign a staff member — by `role_id` (preferred) or by role name."""

    role_id: str | None = None
    role: str | None = Field(default=None, max_length=100)

    @model_validator(mode="after")
    def _require_a_role(self) -> "RoleAssign":
        if self.role_id is None and self.role is None:
            raise ValueError("Provide role_id (preferred) or role")
        return self


class UserStatusUpdate(BaseModel):
    is_active: bool


class AccountStatusUpdate(BaseModel):
    """Set the richer account status. `is_active` follows it: only ACTIVE can log in."""

    status: AccountStatusIn


class AdminResetPassword(BaseModel):
    new_password: str = Field(min_length=8, max_length=128)


class EmployeeOptions(BaseModel):
    """Dropdown data for the employee form: the fixed choice lists, the suggested
    values for the free-text dropdown fields, and the values already in use in
    this firm. `employee_id_prefix` is the firm's current auto-number prefix."""

    employment_types: list[str]
    employee_statuses: list[str]
    account_statuses: list[str]
    genders: list[str]
    marital_statuses: list[str]
    blood_groups: list[str]
    identity_proof_types: list[str]
    emergency_contact_relationships: list[str]
    countries: list[str]
    nationalities: list[str]
    states: list[str]
    designations: list[str]      # the presets plus every designation in use here
    work_locations: list[str]
    shifts: list[str]
    employee_id_prefix: str
