"""The sectioned Employee profile — request and response.

The API speaks in the sections the HR form uses, while the `users` table stays
flat (one column per field, so it is still queryable and indexable). The mapping
lives in `SECTION_FIELDS` plus `ALIASES`: nothing else needs to know both shapes.

Flat bodies are still accepted on input, so callers written against the older
shape keep working — see `EmployeeProfileIn.fold_flat_keys`.

Create and update take exactly the same body, and every field in it is optional.
Which fields a form insists on is the form's business, not this API's; the only
two a login account cannot exist without are `contact_information.official_email`
and `login_security.password`, and the create route checks those itself so the
message can name them.
"""

from datetime import datetime
from enum import Enum
from typing import Annotated

from pydantic import BaseModel, BeforeValidator, ConfigDict, EmailStr, Field, model_validator

from app.schemas.user import (
    AccountStatusIn,
    EmployeeStatusIn,
    EmploymentTypeIn,
    RoleBrief,
    StringList,
)

# API field name -> column name, for the handful that differ.
ALIASES = {
    "mobile_number": "phone",
    "official_email": "email",
    "account_status": "status",
    "other_documents": "uploaded_documents",
}
COLUMN_FOR = ALIASES

# A sanity bound on the many-file slots, so a runaway client cannot grow one
# employee row without limit. Not a policy about what a form should allow.
MAX_DOCUMENTS = 50

# The many-file document slots: API name -> column. Their values are plain URLs
# in the API; the column stores one dict per file (see employee_profile_service).
DOCUMENT_COLLECTIONS = {
    "experience_certificates": "experience_certificates",
    "educational_certificates": "educational_certificates",
    "other_documents": "uploaded_documents",
}

SECTION_FIELDS: dict[str, list[str]] = {
    "basic_information": [
        "first_name", "last_name", "display_name", "gender", "date_of_birth",
        "marital_status", "blood_group", "nationality", "profile_photo",
    ],
    "contact_information": [
        "mobile_number", "alternate_mobile_number", "personal_email", "official_email",
        "emergency_contact_name", "emergency_contact_number", "emergency_contact_relationship",
    ],
    "address_information": [
        "current_address", "permanent_address", "city", "state", "country", "pin_zip_code",
    ],
    "employment_information": [
        "designation", "reporting_manager_id", "employment_type", "date_of_joining",
        "date_of_exit", "work_location", "shift", "employee_status", "role_id",
    ],
    "login_security": ["username", "password", "confirm_password"],
    "payroll_information": [
        "basic_salary", "bank_name", "account_number", "ifsc_swift_code",
        "account_holder_name", "upi_id",
    ],
    "documents": [
        "identity_proof_type", "identity_proof_file", "resume_cv", "offer_letter",
        "appointment_letter", *DOCUMENT_COLLECTIONS,
    ],
    "professional_information": ["skills"],
    "system_preferences": ["language", "time_zone", "account_status"],
}

# Fields that live in a section but are not columns to write: the password pair
# (hashed by the route), the read-only lookups, and the many-file slots (whose
# columns hold richer rows than the URL list the API shows).
NOT_COLUMNS = {
    "password", "confirm_password", "reporting_manager", "role_detail", *DOCUMENT_COLLECTIONS,
}


def _blank_to_none(value: object) -> object:
    """An untouched form field arrives as "" — store it as "not set", not as a
    blank string that would fail email/id validation."""
    return None if value == "" else value


OptionalEmail = Annotated[EmailStr | None, BeforeValidator(_blank_to_none)]
OptionalDate = Annotated[datetime | None, BeforeValidator(_blank_to_none)]
OptionalRef = Annotated[str | None, BeforeValidator(_blank_to_none)]


class NamedRef(BaseModel):
    """A related record resolved from its id, so the UI never looks the name up."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str


class BasicInformation(BaseModel):
    first_name: str | None = Field(default=None, max_length=50)
    last_name: str | None = Field(default=None, max_length=50)
    display_name: str | None = Field(default=None, max_length=150)
    gender: str | None = Field(default=None, max_length=30)
    date_of_birth: OptionalDate = None
    marital_status: str | None = Field(default=None, max_length=30)
    blood_group: str | None = Field(default=None, max_length=10)
    nationality: str | None = Field(default=None, max_length=100)
    profile_photo: str | None = Field(default=None, description="URL from POST /files/upload")


class ContactInformation(BaseModel):
    mobile_number: str | None = Field(default=None, max_length=20)
    alternate_mobile_number: str | None = Field(default=None, max_length=20)
    personal_email: OptionalEmail = None
    official_email: OptionalEmail = Field(default=None, description="The login identifier")
    emergency_contact_name: str | None = Field(default=None, max_length=150)
    emergency_contact_number: str | None = Field(default=None, max_length=20)
    emergency_contact_relationship: str | None = Field(default=None, max_length=50)


class AddressInformation(BaseModel):
    current_address: str | None = None
    permanent_address: str | None = None
    city: str | None = Field(default=None, max_length=100)
    state: str | None = Field(default=None, max_length=100)
    country: str | None = Field(default=None, max_length=100)
    pin_zip_code: str | None = Field(default=None, max_length=20)


class EmploymentInformation(BaseModel):
    designation: str | None = Field(default=None, max_length=100)
    reporting_manager_id: OptionalRef = Field(
        default=None, description="Another employee in the same firm"
    )
    # Read-only, resolved from reporting_manager_id.
    reporting_manager: NamedRef | None = None
    employment_type: EmploymentTypeIn | None = None
    date_of_joining: OptionalDate = None
    date_of_exit: OptionalDate = None
    work_location: str | None = Field(default=None, max_length=150)
    shift: str | None = Field(default=None, max_length=50)
    employee_status: EmployeeStatusIn | None = None
    role_id: OptionalRef = Field(default=None, description="One of the firm's roles (GET /roles)")
    # Read-only, resolved from role_id — the role's name and permission matrix.
    role_detail: RoleBrief | None = None


class LoginSecurity(BaseModel):
    """Write side of the login account. `password` is accepted on update too, so
    one PATCH can change the credentials along with everything else."""

    username: str | None = Field(default=None, min_length=3, max_length=50)
    password: str | None = Field(default=None, min_length=8, max_length=128)
    confirm_password: str | None = Field(default=None, min_length=8, max_length=128)

    @model_validator(mode="after")
    def _passwords_match(self) -> "LoginSecurity":
        if (
            self.password is not None
            and self.confirm_password is not None
            and self.password != self.confirm_password
        ):
            raise ValueError("password and confirm_password do not match")
        return self


class LoginSecurityOut(BaseModel):
    """Read side — the password never comes back out."""

    username: str | None = None


class PayrollInformation(BaseModel):
    basic_salary: float | None = Field(default=None, ge=0)
    bank_name: str | None = Field(default=None, max_length=150)
    account_number: str | None = Field(default=None, max_length=50)
    ifsc_swift_code: str | None = Field(default=None, max_length=30)
    account_holder_name: str | None = Field(default=None, max_length=150)
    upi_id: str | None = Field(default=None, max_length=100)


class ProfessionalInformation(BaseModel):
    skills: StringList = Field(default_factory=list)


class SystemPreferences(BaseModel):
    language: str | None = Field(default=None, max_length=30)
    time_zone: str | None = Field(default=None, max_length=64)
    account_status: AccountStatusIn | None = Field(
        default=None, description="Only an active account can log in"
    )


class DocumentsSection(BaseModel):
    """The employee's files, as URLs.

    Upload first with POST /files/upload (which needs no employee id, so it works
    before the employee exists), then send the `url` it returned here. The same
    URLs come back on read, so what you send is what you get.

    A named slot holds one file — sending a new URL replaces it, and null empties
    it. The three lists hold many, and are replaced wholesale by what you send, so
    `[]` empties one; to drop a single file without resending the rest, use
    DELETE /users/{id}/documents/{collection}?document_id=… where `document_id` is
    the `file_id` from the upload (the last segment of the URL).
    """

    identity_proof_type: str | None = Field(default=None, max_length=50)
    identity_proof_file: str | None = None
    resume_cv: str | None = None
    offer_letter: str | None = None
    appointment_letter: str | None = None
    experience_certificates: StringList = Field(default_factory=list, max_length=MAX_DOCUMENTS)
    educational_certificates: StringList = Field(default_factory=list, max_length=MAX_DOCUMENTS)
    other_documents: StringList = Field(
        default_factory=list,
        max_length=MAX_DOCUMENTS,
        description="Anything without a slot of its own",
    )


_SECTION_MODELS: dict[str, type[BaseModel]] = {
    "basic_information": BasicInformation,
    "contact_information": ContactInformation,
    "address_information": AddressInformation,
    "employment_information": EmploymentInformation,
    "login_security": LoginSecurity,
    "payroll_information": PayrollInformation,
    "documents": DocumentsSection,
    "professional_information": ProfessionalInformation,
    "system_preferences": SystemPreferences,
}


class EmployeeProfileIn(BaseModel):
    """Create / update body. Every section is optional, and within a section every
    field is optional, so a partial update can send just the section it touches.

    One endpoint covers every change: resigning someone is `employment_information`
    (`employee_status`, `date_of_exit`) plus `system_preferences.account_status`
    in a single PATCH — there is no separate status endpoint.
    """

    model_config = ConfigDict(extra="ignore")

    employee_id: str | None = Field(
        default=None, max_length=50, description="Auto-assigned (EMP-0001, …) when omitted"
    )
    name: str | None = Field(
        default=None, max_length=150, description="Composed from first + last name when omitted"
    )
    role: str | None = Field(
        default=None,
        max_length=100,
        description="Role *name*; prefer employment_information.role_id",
    )

    basic_information: BasicInformation | None = None
    contact_information: ContactInformation | None = None
    address_information: AddressInformation | None = None
    employment_information: EmploymentInformation | None = None
    login_security: LoginSecurity | None = None
    payroll_information: PayrollInformation | None = None
    documents: DocumentsSection | None = Field(
        default=None, description="URLs from POST /files/upload"
    )
    professional_information: ProfessionalInformation | None = None
    system_preferences: SystemPreferences | None = None

    @model_validator(mode="before")
    @classmethod
    def fold_flat_keys(cls, data: object) -> object:
        """Accept a flat body as well as a sectioned one.

        Callers written against the older flat shape (`{"first_name": …,
        "phone": …}`) keep working: any top-level key that belongs to a section is
        folded into it. Explicit sections always win over a flat key for the same
        field. `document_information` is taken as another name for `documents`.
        """
        if not isinstance(data, dict):
            return data
        folded = {k: v for k, v in data.items() if k in cls.model_fields}
        if "documents" not in folded and isinstance(data.get("document_information"), dict):
            folded["documents"] = data["document_information"]
        for section, fields in SECTION_FIELDS.items():
            picked = {}
            for field in fields:
                # A flat body may use either the API name or the column name.
                for key in (field, COLUMN_FOR.get(field)):
                    if key and key in data and key not in cls.model_fields:
                        picked[field] = data[key]
                        break
            if picked:
                existing = folded.get(section) or {}
                if isinstance(existing, BaseModel):
                    existing = existing.model_dump(exclude_unset=True)
                folded[section] = {**picked, **existing}
        # Legacy alias of the identity proof file.
        if "identity_proof_file" not in data and data.get("identify_proofs") is not None:
            documents = folded.get("documents") or {}
            if isinstance(documents, BaseModel):
                documents = documents.model_dump(exclude_unset=True)
            documents.setdefault("identity_proof_file", data["identify_proofs"])
            folded["documents"] = documents
        return folded

    def to_columns(self) -> dict:
        """Flatten the sections into the column names the model stores.

        Only what the caller actually sent comes back, so an update writes just
        those columns. The password and the many-file slots are left out — the
        route hashes the one and `employee_profile_service` builds the others.
        """
        values: dict = {}
        for section in SECTION_FIELDS:
            block = getattr(self, section, None)
            if block is None:
                continue
            for field, value in block.model_dump(exclude_unset=True).items():
                if field in NOT_COLUMNS:
                    continue
                # Choice fields are stored as their plain string value.
                values[COLUMN_FOR.get(field, field)] = (
                    value.value if isinstance(value, Enum) else value
                )
        sent = self.model_dump(exclude_unset=True)
        for field in ("employee_id", "name"):
            if field in sent:
                values[field] = getattr(self, field)
        return values

    @property
    def password(self) -> str | None:
        return self.login_security.password if self.login_security else None


class EmployeeProfileOut(BaseModel):
    """The full employee profile, in the same sections the create body uses."""

    id: str
    employee_id: str | None = None
    organization_id: str | None = None
    name: str
    system_role: str | None = None

    basic_information: BasicInformation
    contact_information: ContactInformation
    address_information: AddressInformation
    employment_information: EmploymentInformation
    login_security: LoginSecurityOut
    payroll_information: PayrollInformation
    documents: DocumentsSection
    professional_information: ProfessionalInformation
    system_preferences: SystemPreferences

    is_active: bool = True
    created_at: datetime
