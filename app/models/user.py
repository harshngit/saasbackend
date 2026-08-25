import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, DateTime, Enum, Float, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.models.enums import UserRole


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


class User(Base):
    """A platform or firm user. Email is unique per organization.

    SUPER_ADMIN rows have organization_id = NULL (platform-level, no tenant).
    """

    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("organization_id", "email", name="uq_user_org_email"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    organization_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=True, index=True
    )

    name: Mapped[str] = mapped_column(String(150), nullable=False)
    email: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    # Unique platform-wide login/identifier for staff (in addition to email). Nullable
    # so existing users / admins created before this field remain valid.
    username: Mapped[str | None] = mapped_column(String(50), nullable=True, unique=True, index=True)
    phone: Mapped[str | None] = mapped_column(String(20), nullable=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    # Google OAuth subject identifier (sub claim). Nullable for password-only users.
    google_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)

    # Top-level role for routing/access control (super_admin / admin / staff).
    system_role: Mapped[str | None] = mapped_column(String(20), nullable=True, index=True)
    # Staff's detailed org-scoped role (permission matrix). Null for admin/super_admin.
    role_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("roles.id"), nullable=True, index=True
    )
    # Legacy fixed role enum — kept (nullable) for backward-compat in responses.
    role: Mapped[UserRole | None] = mapped_column(Enum(UserRole), nullable=True)

    # ----------------------------- Employee Profile -----------------------------
    # All nullable: rows created before a field existed keep working, and only the
    # create endpoint enforces which of them the HR form treats as mandatory.

    employee_id: Mapped[str | None] = mapped_column(String(50), nullable=True, index=True)

    # 1. Basic information (`name` above is the combined display name)
    first_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    last_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    display_name: Mapped[str | None] = mapped_column(String(150), nullable=True)
    gender: Mapped[str | None] = mapped_column(String(30), nullable=True)
    date_of_birth: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    marital_status: Mapped[str | None] = mapped_column(String(30), nullable=True)
    blood_group: Mapped[str | None] = mapped_column(String(10), nullable=True)
    nationality: Mapped[str | None] = mapped_column(String(100), nullable=True)

    # 2. Contact information (`email` / `phone` above are the login + primary contact)
    alternate_mobile_number: Mapped[str | None] = mapped_column(String(20), nullable=True)
    personal_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    emergency_contact_name: Mapped[str | None] = mapped_column(String(150), nullable=True)
    emergency_contact_number: Mapped[str | None] = mapped_column(String(20), nullable=True)
    emergency_contact_relationship: Mapped[str | None] = mapped_column(String(50), nullable=True)

    # 3. Address information
    current_address: Mapped[str | None] = mapped_column(Text, nullable=True)
    permanent_address: Mapped[str | None] = mapped_column(Text, nullable=True)
    city: Mapped[str | None] = mapped_column(String(100), nullable=True)
    state: Mapped[str | None] = mapped_column(String(100), nullable=True)
    country: Mapped[str | None] = mapped_column(String(100), nullable=True)
    pin_zip_code: Mapped[str | None] = mapped_column(String(20), nullable=True)

    # 4. Employment information
    designation: Mapped[str | None] = mapped_column(String(100), nullable=True)
    reporting_manager_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    employment_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    date_of_joining: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    date_of_exit: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    work_location: Mapped[str | None] = mapped_column(String(150), nullable=True)
    shift: Mapped[str | None] = mapped_column(String(50), nullable=True)
    employee_status: Mapped[str | None] = mapped_column(String(30), nullable=True)

    # 5. Login & security — `username` / `password_hash` above

    # 6. Payroll information
    basic_salary: Mapped[float | None] = mapped_column(Float, nullable=True)
    bank_name: Mapped[str | None] = mapped_column(String(150), nullable=True)
    account_number: Mapped[str | None] = mapped_column(String(50), nullable=True)
    ifsc_swift_code: Mapped[str | None] = mapped_column(String(30), nullable=True)
    account_holder_name: Mapped[str | None] = mapped_column(String(150), nullable=True)
    upi_id: Mapped[str | None] = mapped_column(String(100), nullable=True)

    # 7. Uploads — data: URLs (Text, not VARCHAR); the *_certificates /
    # uploaded_documents slots hold many files each, managed by
    # /users/{id}/documents/{collection}.
    profile_photo: Mapped[str | None] = mapped_column(Text, nullable=True)
    identity_proof_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    identity_proof_file: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Legacy name for identity_proof_file — kept in sync so older clients still work.
    identify_proofs: Mapped[str | None] = mapped_column(Text, nullable=True)
    resume_cv: Mapped[str | None] = mapped_column(Text, nullable=True)
    offer_letter: Mapped[str | None] = mapped_column(Text, nullable=True)
    appointment_letter: Mapped[str | None] = mapped_column(Text, nullable=True)
    uploaded_documents: Mapped[list | None] = mapped_column(JSON, default=list, nullable=True)
    experience_certificates: Mapped[list | None] = mapped_column(JSON, default=list, nullable=True)
    educational_certificates: Mapped[list | None] = mapped_column(JSON, default=list, nullable=True)
    skills: Mapped[list | None] = mapped_column(JSON, default=list, nullable=True)

    # 8. System preferences
    language: Mapped[str | None] = mapped_column(String(30), nullable=True)
    time_zone: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Account status enum (active / inactive / suspended / locked). Richer than
    # `is_active`, which is the flag the login check actually reads — the two are
    # kept in sync whenever `status` is written.
    status: Mapped[str | None] = mapped_column(String(20), nullable=True, index=True)

    # 9. Last known position, written by POST /users/me/location. Deliberately the
    # latest ping only — `work_location` above is the office they are posted to and
    # is never used as a live location.
    last_latitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    last_longitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    last_location_accuracy_m: Mapped[float | None] = mapped_column(Float, nullable=True)
    last_location_label: Mapped[str | None] = mapped_column(String(200), nullable=True)
    last_location_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)

    organization: Mapped["Organization | None"] = relationship(back_populates="users")  # noqa: F821
    role_detail: Mapped["Role | None"] = relationship(foreign_keys=[role_id], lazy="joined")  # noqa: F821

    @property
    def effective_system_role(self) -> str:
        """system_role if set, otherwise derived from the legacy role (transition safety)."""
        if self.system_role:
            return self.system_role
        from app.models.enums import system_role_for

        return system_role_for(self.role)
