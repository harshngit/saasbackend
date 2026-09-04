"""Assemble the sectioned employee profile from the flat `users` row.

The row keeps one column per field; this module is the only place that knows how
those columns fold back into the sections the API speaks (see
app/schemas/employee_profile.py for the other half of the mapping).
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.schemas.employee_profile import (
    COLUMN_FOR,
    DOCUMENT_COLLECTIONS,
    NOT_COLUMNS,
    SECTION_FIELDS,
    AddressInformation,
    BasicInformation,
    ContactInformation,
    DocumentsSection,
    EmployeeProfileOut,
    EmploymentInformation,
    LoginSecurityOut,
    NamedRef,
    PayrollInformation,
    ProfessionalInformation,
    SystemPreferences,
)
from app.schemas.user import AccountStatus, RoleBrief, TeamBrief

_SECTION_MODELS = {
    "basic_information": BasicInformation,
    "contact_information": ContactInformation,
    "address_information": AddressInformation,
    "employment_information": EmploymentInformation,
    "payroll_information": PayrollInformation,
    "professional_information": ProfessionalInformation,
    "system_preferences": SystemPreferences,
}


def document_id(url: str) -> str:
    """The id a document is deleted by — the stored file's id, which is the last
    segment of the URL that POST /files/upload handed back. Anything without one
    (an inline data: URL, an odd external link) gets an id of its own."""
    if not url or url.startswith("data:"):
        return str(uuid.uuid4())
    tail = url.split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1]
    return tail or str(uuid.uuid4())


def _document_row(db: Session, url: str, known: dict[str, dict]) -> dict:
    """One entry for a many-file column. A URL already on file keeps its row (and
    with it the original filename and upload time); a new one is described from
    the stored file it points at, when it is one of ours."""
    from app.models import StoredFile

    if url in known:
        return known[url]
    file_id = document_id(url)
    stored = db.get(StoredFile, file_id)
    return {
        "id": file_id,
        "name": stored.filename if stored is not None else url.rsplit("/", 1)[-1] or "document",
        "url": url,
        "content_type": stored.content_type if stored is not None else None,
        "size": stored.size if stored is not None else None,
        "uploaded_at": datetime.now(timezone.utc).isoformat(),
    }


def apply_documents(db: Session, user, section: DocumentsSection | None) -> None:
    """Write the many-file columns from the URL lists in a `documents` block.

    Only the lists the caller actually sent are touched, and each is replaced by
    what was sent — the API shows these as plain URL lists, so a list is the whole
    truth about that slot. The single-file slots need nothing here: they are plain
    URL columns that `to_columns()` already covers.
    """
    if section is None:
        return
    sent = section.model_dump(exclude_unset=True)
    for field, column in DOCUMENT_COLLECTIONS.items():
        if field not in sent:
            continue
        known = {row.get("url"): row for row in getattr(user, column) or [] if isinstance(row, dict)}
        # Reassigned wholesale: SQLAlchemy does not track in-place JSON mutation.
        setattr(user, column, [_document_row(db, url, known) for url in sent[field] or [] if url])


def documents_section(user) -> DocumentsSection:
    """The single-file slots as they are stored, and the many-file slots as the
    URL lists the API speaks in."""
    section = DocumentsSection(
        **{
            field: getattr(user, field, None)
            for field in SECTION_FIELDS["documents"]
            if field not in DOCUMENT_COLLECTIONS
        }
    )
    for field, column in DOCUMENT_COLLECTIONS.items():
        urls = [row.get("url") for row in getattr(user, column) or [] if isinstance(row, dict)]
        setattr(section, field, [url for url in urls if url])
    return section


def _section(db: Session, user, name: str):
    model = _SECTION_MODELS[name]
    block = model(
        **{
            field: getattr(user, COLUMN_FOR.get(field, field), None)
            for field in SECTION_FIELDS[name]
            if field not in NOT_COLUMNS
        }
    )
    if name == "employment_information":
        manager = db.get(type(user), user.reporting_manager_id) if user.reporting_manager_id else None
        block.reporting_manager = (
            NamedRef(id=manager.id, name=manager.name) if manager is not None else None
        )
        block.role_detail = (
            RoleBrief.model_validate(user.role_detail) if user.role_detail is not None else None
        )
        block.team_id = user.team_id
        block.team = TeamBrief.model_validate(user.team) if user.team is not None else None
    if name == "system_preferences" and block.account_status is None:
        # Rows created before `status` existed report one derived from `is_active`,
        # so clients never have to handle a null account status.
        block.account_status = AccountStatus.ACTIVE if user.is_active else AccountStatus.INACTIVE
    return block


def build_profile(db: Session, user) -> EmployeeProfileOut:
    return EmployeeProfileOut(
        id=user.id,
        employee_id=user.employee_id,
        organization_id=user.organization_id,
        name=user.name,
        system_role=user.effective_system_role,
        **{name: _section(db, user, name) for name in _SECTION_MODELS},
        login_security=LoginSecurityOut(username=user.username),
        documents=documents_section(user),
        is_active=user.is_active,
        created_at=user.created_at,
    )
