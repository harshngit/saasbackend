import base64
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import require_system_role
from app.models import Organization, Plan, SystemRole, User
from app.schemas.company import (
    CompanySettingsOut,
    CompanySettingsUpdate,
    CompanyStatus,
    FieldSettingsOut,
    FieldSettingsUpdate,
    OtherDocument,
    UploadResponse,
)
from app.schemas.organization import OrganizationOut, UpgradeRequest
from app.services import org_service

router = APIRouter(prefix="/organizations", tags=["organizations"])

_ADMIN = require_system_role(SystemRole.ADMIN)
_MAX_UPLOAD_BYTES = 1024 * 1024  # 1 MB cap for logo/signature images
_MAX_DOCUMENT_BYTES = 5 * 1024 * 1024  # 5 MB cap per uploaded document
_MAX_OTHER_DOCUMENTS = 20  # "Other Business Documents" is multi-file, but not unbounded


def _admin_org(admin: User, db: Session) -> Organization:
    org = admin.organization
    if org is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No organization")
    return org


def _store_image(file: UploadFile) -> str:
    """Read an uploaded image and return it as a base64 data: URL.

    Works on Render's ephemeral disk (persisted in the DB). Swap for S3/Cloudinary
    later without changing the API contract (this still returns a URL string).
    """
    if not (file.content_type or "").startswith("image/"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="File must be an image")
    content = file.file.read()
    if len(content) > _MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="Image too large (max 1 MB)")
    encoded = base64.b64encode(content).decode("ascii")
    return f"data:{file.content_type};base64,{encoded}"


@router.get("/me", response_model=OrganizationOut)
def my_organization(
    admin: User = Depends(require_system_role(SystemRole.ADMIN)),
    db: Session = Depends(get_db),
) -> object:
    """Current org state (status, plan, trial, upgrade status). Works even when locked."""
    org = org_service.apply_trial_expiry(db, admin.organization)
    if org is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No organization")
    return org


@router.post("/upgrade-request", response_model=OrganizationOut)
def request_upgrade(
    payload: UpgradeRequest,
    admin: User = Depends(require_system_role(SystemRole.ADMIN)),
    db: Session = Depends(get_db),
) -> object:
    """Admin submits an upgrade request. Allowed even while locked — this is how a
    locked org escapes the lock. A Super Admin then approves it."""
    org = admin.organization
    if org is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No organization")

    plan = db.get(Plan, payload.requested_plan_id)
    if plan is None or not plan.is_active:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Selected plan does not exist or is no longer available",
        )
    return org_service.request_upgrade(db, org, plan, payload.billing_cycle)


# ------------------------------- Company Settings -------------------------------


@router.get("/settings", response_model=CompanySettingsOut)
def get_company_settings(admin: User = Depends(_ADMIN), db: Session = Depends(get_db)) -> Organization:
    """Full company profile for the Company Settings page (Admin only)."""
    return _admin_org(admin, db)


@router.put("/settings", response_model=CompanySettingsOut)
def update_company_settings(
    payload: CompanySettingsUpdate,
    admin: User = Depends(_ADMIN),
    db: Session = Depends(get_db),
) -> Organization:
    """Partial update of the company profile (send only changed fields).

    `status` / `company_status` here is the Company Master's active-inactive toggle;
    the subscription lifecycle (trial / locked / …) is not editable from this page.
    """
    org = _admin_org(admin, db)
    for field, value in payload.model_dump(exclude_unset=True).items():
        if isinstance(value, CompanyStatus):
            value = value.value
        setattr(org, field, value)
    db.commit()
    db.refresh(org)
    return org


@router.post("/settings/logo", response_model=UploadResponse)
def upload_logo(
    file: UploadFile = File(...),
    admin: User = Depends(_ADMIN),
    db: Session = Depends(get_db),
) -> UploadResponse:
    org = _admin_org(admin, db)
    org.logo_url = _store_image(file)
    db.commit()
    return UploadResponse(url=org.logo_url)


@router.post("/settings/signature", response_model=UploadResponse)
def upload_signature(
    file: UploadFile = File(...),
    admin: User = Depends(_ADMIN),
    db: Session = Depends(get_db),
) -> UploadResponse:
    org = _admin_org(admin, db)
    org.signature_url = _store_image(file)
    db.commit()
    return UploadResponse(url=org.signature_url)


def _is_document(content_type: str) -> bool:
    """PDF / PNG / JPG / DOCX — the formats the Company Master accepts for documents."""
    return (
        content_type.startswith("image/")
        or content_type == "application/pdf"
        or content_type.startswith("application/vnd")
        or content_type.endswith("document")
        or content_type == "application/octet-stream"
    )


def _store_document(file: UploadFile) -> tuple[str, bytes]:
    """Validate + read one document upload, returning its data: URL and raw bytes."""
    content_type = file.content_type or ""
    if not _is_document(content_type):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported file format for '{file.filename}' (allowed: PDF, PNG, JPG, DOCX)",
        )
    content = file.file.read()
    if len(content) > _MAX_DOCUMENT_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"'{file.filename}' is too large (max 5 MB per file)",
        )
    encoded = base64.b64encode(content).decode("ascii")
    return f"data:{content_type};base64,{encoded}", content


@router.post("/settings/upload-file", response_model=UploadResponse)
def upload_settings_file(
    file: UploadFile = File(...),
    admin: User = Depends(_ADMIN),
) -> UploadResponse:
    """Generic file uploader for company settings assets (images, PDFs, documents up to 5 MB)."""
    url, _ = _store_document(file)
    return UploadResponse(url=url)


# --------------------------- Other Business Documents ---------------------------
# The one Company Master slot that holds many files (the rest are single-document
# fields like doc_gst_url). Stored as a JSON list on the org.


def _other_documents(org: Organization) -> list[dict]:
    return list(org.doc_other_files or [])


def _save_documents(db: Session, org: Organization, documents: list[dict]) -> list[dict]:
    """Persist the document list. Reassigned wholesale — SQLAlchemy does not track
    in-place mutation of a JSON column. `doc_other_url` mirrors the first file so
    clients still reading the single-file field keep working."""
    org.doc_other_files = documents
    org.doc_other_url = documents[0]["url"] if documents else None
    db.commit()
    db.refresh(org)
    return _other_documents(org)


@router.get("/settings/documents/other", response_model=list[OtherDocument])
def list_other_documents(admin: User = Depends(_ADMIN), db: Session = Depends(get_db)) -> list[dict]:
    """Every "Other Business Document" on file for this company."""
    return _other_documents(_admin_org(admin, db))


@router.post(
    "/settings/documents/other",
    response_model=list[OtherDocument],
    status_code=status.HTTP_201_CREATED,
)
def upload_other_documents(
    files: list[UploadFile] = File(..., description="One or more PDF / PNG / JPG / DOCX files"),
    admin: User = Depends(_ADMIN),
    db: Session = Depends(get_db),
) -> list[dict]:
    """Upload one or more "Other Business Documents" — they're appended to whatever
    is already on file, so uploading again does not replace the earlier ones.
    Returns the full list."""
    org = _admin_org(admin, db)
    documents = _other_documents(org)
    if len(documents) + len(files) > _MAX_OTHER_DOCUMENTS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"At most {_MAX_OTHER_DOCUMENTS} other documents "
                   f"({len(documents)} already uploaded). Delete some first.",
        )

    # Validate and read every file before writing, so a bad one in the batch
    # doesn't leave the company profile half-updated.
    for file in files:
        url, content = _store_document(file)
        documents.append(
            {
                "id": str(uuid.uuid4()),
                "name": file.filename or "document",
                "url": url,
                "content_type": file.content_type,
                "size": len(content),
                "uploaded_at": datetime.now(timezone.utc).isoformat(),
            }
        )
    return _save_documents(db, org, documents)


@router.delete("/settings/documents/other/{document_id}", response_model=list[OtherDocument])
def delete_other_document(
    document_id: str,
    admin: User = Depends(_ADMIN),
    db: Session = Depends(get_db),
) -> list[dict]:
    """Remove one uploaded document. Returns the remaining list."""
    org = _admin_org(admin, db)
    documents = _other_documents(org)
    remaining = [d for d in documents if d.get("id") != document_id]
    if len(remaining) == len(documents):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")
    return _save_documents(db, org, remaining)


@router.delete("/settings/documents/other", status_code=status.HTTP_204_NO_CONTENT)
def clear_other_documents(admin: User = Depends(_ADMIN), db: Session = Depends(get_db)) -> None:
    """Remove every "Other Business Document" for this company."""
    _save_documents(db, _admin_org(admin, db), [])


# ------------------------------- Field Settings -------------------------------

AVAILABLE_FIELDS = {
    "company": {
        "mandatory": [
            "name", "legal_name", "business_type", "industry", "primary_mobile", "email",
            "registered_address", "city", "state", "country", "pin_code",
            "logo_url", "signature_url", "payment_qr_url",
            "currency", "timezone", "language", "tax_configuration", "invoice_settings",
            "auth_person_name", "auth_person_designation", "auth_person_mobile", "auth_person_email",
            "company_status"
        ],
        "optional": [
            "date_of_incorporation", "cin_number", "gstin_pan", "description",
            "alternate_mobile", "landline", "website", "customer_support_number",
            "branch_address", "stamp_url", "letterhead_url", "banner_url",
            "upi_id", "bank_account_details", "bank_account_holder", "bank_ifsc", "bank_name",
            "facebook_url", "instagram_url", "linkedin_url", "twitter_url", "youtube_url", "whatsapp_number",
            "financial_year",
            "doc_gst_url", "doc_pan_url", "doc_coi_url", "doc_trade_license_url", "doc_msme_url", "doc_fssai_url",
            "doc_other_url", "doc_other_files",
            "auth_person_photo_url", "auth_person_signature_url",
            "employee_count", "business_hours", "mission_vision", "notes"
        ]
    },
    "customer": {
        "mandatory": ["name"],
        "optional": ["business_name", "phone", "email", "gst_number", "billing_address", "delivery_address", "category", "notes", "credit_limit", "opening_balance"]
    },
    "product": {
        "mandatory": ["name", "price"],
        "optional": ["brand", "description", "category_id", "tax_rate", "min_stock", "barcode"]
    },
    "sales": {
        "mandatory": ["customer_id", "items"],
        "optional": ["discount", "tax", "notes", "source", "assigned_delivery_partner_id"]
    },
    "purchase": {
        "mandatory": ["invoice_number", "supplier_id", "items"],
        "optional": ["invoice_date", "discount", "tax", "notes", "attachment_url"]
    },
    "expenses": {
        "mandatory": ["category", "amount"],
        "optional": ["description", "receipt_url", "payment_mode", "expense_date"]
    }
}


@router.get("/settings/fields", response_model=FieldSettingsOut)
def get_field_settings(
    admin: User = Depends(_ADMIN),
    db: Session = Depends(get_db),
) -> dict:
    """Retrieve the current configurable fields setting and available fields metadata."""
    org = _admin_org(admin, db)
    current_settings = org.field_settings or {}
    
    # Initialize dictionary structure with defaults if not present
    initialized_settings = {}
    for module, fields_info in AVAILABLE_FIELDS.items():
        initialized_settings[module] = {}
        saved_module = current_settings.get(module, {})
        for opt_field in fields_info["optional"]:
            initialized_settings[module][opt_field] = saved_module.get(opt_field, False)
            
    return {
        "field_settings": initialized_settings,
        "available_fields": AVAILABLE_FIELDS
    }


@router.put("/settings/fields", response_model=FieldSettingsOut)
def update_field_settings(
    payload: FieldSettingsUpdate,
    admin: User = Depends(_ADMIN),
    db: Session = Depends(get_db),
) -> dict:
    """Update enabling/disabling status of optional fields for each module."""
    org = _admin_org(admin, db)
    
    # Clean/validate payload to ensure only valid optional fields are updated
    cleaned_settings = {}
    for module, fields_info in AVAILABLE_FIELDS.items():
        cleaned_settings[module] = {}
        payload_module = payload.field_settings.get(module, {})
        for opt_field in fields_info["optional"]:
            cleaned_settings[module][opt_field] = payload_module.get(opt_field, False)
            
    org.field_settings = cleaned_settings
    db.commit()
    db.refresh(org)
    
    return {
        "field_settings": org.field_settings,
        "available_fields": AVAILABLE_FIELDS
    }

