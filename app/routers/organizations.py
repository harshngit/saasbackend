import base64
import json
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.files import save_upload
from app.core.deps import require_system_role
from app.core import reference_data as R
from app.core.reference_data import (
    BANK_NAMES,
    BUSINESS_TYPES,
    COUNTRIES,
    CURRENCIES,
    INDIAN_STATES,
    INDUSTRIES,
    LANGUAGES,
    TIME_ZONES,
)
from app.models import Organization, Plan, SystemRole, User
from app.schemas.company import (
    BusinessDocumentSlot,
    CompanyCompleteness,
    CompanyOptions,
    CompanySettingsOut,
    CompanySettingsUpdate,
    CompanyStatus,
    FieldSettingsOut,
    FieldSettingsUpdate,
    OtherDocument,
    UploadResponse,
)
from app.schemas.organization import OrganizationOut, UpgradeRequest
from app.schemas.overview import CompanyOverviewOut
from app.services import activity_service, numbering_service, org_service, overview_service

router = APIRouter(prefix="/organizations", tags=["organizations"])

_ADMIN = require_system_role(SystemRole.ADMIN)
# Limits from the sheet's validation rules: images up to 5 MB, documents up to 10 MB.
_MAX_UPLOAD_BYTES = 5 * 1024 * 1024
_MAX_DOCUMENT_BYTES = 10 * 1024 * 1024
_MAX_OTHER_DOCUMENTS = 20  # "Other Business Documents" is multi-file, but not unbounded


def _admin_org(admin: User, db: Session) -> Organization:
    org = admin.organization
    if org is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No organization")
    # Every Company Settings request comes through here, which is where a firm
    # created before company codes existed picks one up.
    org_service.ensure_company_code(db, org)
    return org


def _store_image(db: Session, org_id: str | None, file: UploadFile, request: Request) -> str:
    """Store an uploaded image and return the URL it is served from."""
    url, _size = save_upload(
        db, org_id, file, request, allow_pdf=False, max_bytes=_MAX_UPLOAD_BYTES
    )
    return url


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


@router.get("/overview", response_model=CompanyOverviewOut)
def company_overview(
    activity_limit: int = Query(
        default=10, ge=1, le=100, description="How many Recent Activity entries to return"
    ),
    admin: User = Depends(_ADMIN),
    db: Session = Depends(get_db),
) -> CompanyOverviewOut:
    """Everything the Company Settings dashboard shows, in one call.

    One block per card on the page: `company` (header + plan), `counts` (the stat
    tiles), `storage`, `profile_completion`, `authorized_person`, `documents`,
    `addresses` and `recent_activity`. All read-only and derived — edit the
    underlying values through PUT /settings and the document endpoints.
    """
    org = _admin_org(admin, db)
    return overview_service.build_overview(
        db, org, REQUIRED_COMPANY_FIELDS, activity_limit=activity_limit
    )


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
    changes = payload.model_dump(exclude_unset=True)
    for field, value in changes.items():
        if isinstance(value, CompanyStatus):
            value = value.value
        elif field in ("tax_configuration", "invoice_settings") and isinstance(value, dict):
            value = json.dumps(value)  # TEXT column, so config panels ride as JSON
        elif field == "branch_addresses" and value is not None:
            value = _numbered_branches(value)
            # Keep the single legacy field pointing at the first branch.
            org.branch_address = value[0]["address"] if value else None
        setattr(org, field, value)
    activity_service.record_field_changes(db, org.id, admin, list(changes))
    db.commit()
    db.refresh(org)
    return org


# The Company Master sheet's mandatory fields, in sheet order. Used by
# /settings/completeness — see CompanyCompleteness for why these are reported
# rather than enforced on every partial update.
REQUIRED_COMPANY_FIELDS = [
    "name", "legal_name", "business_type", "industry",
    "primary_mobile", "email",
    "registered_address", "city", "state", "country", "pin_code",
    "logo_url", "signature_url", "payment_qr_url",
    "currency", "timezone", "language", "tax_configuration", "invoice_settings",
    "auth_person_name", "auth_person_designation", "auth_person_mobile", "auth_person_email",
    "company_status",
]


@router.get("/settings/options", response_model=CompanyOptions)
def company_options(
    admin: User = Depends(_ADMIN), db: Session = Depends(get_db)
) -> CompanyOptions:
    """Every dropdown list the app needs: Company Master fields, plus the allowed
    values the field sheet spells out for customers, sales, purchases and expenses,
    plus this firm's auto-number prefixes."""
    return CompanyOptions(
        business_types=BUSINESS_TYPES,
        industries=INDUSTRIES,
        currencies=CURRENCIES,
        time_zones=TIME_ZONES,
        languages=LANGUAGES,
        countries=COUNTRIES,
        states=INDIAN_STATES,
        bank_names=BANK_NAMES,
        company_statuses=[s.value for s in CompanyStatus],
        number_series={
            series: numbering_service.prefix_for(db, admin.organization_id, series)
            for series in ("CUST", "PROD", "LEAD", "QT", "SO", "INV", "SALE",
                           "RCPT", "RET", "DN", "EXP", "EXPID", "PUR", "PURID")
        },
        customer_statuses=R.CUSTOMER_STATUSES,
        customer_types=R.CUSTOMER_TYPES,
        sales_types=R.SALES_TYPES,
        sales_statuses=R.SALES_STATUSES,
        order_statuses=R.ORDER_STATUSES_SHEET,
        invoice_statuses=R.INVOICE_STATUSES,
        invoice_payment_statuses=R.INVOICE_PAYMENT_STATUSES,
        purchase_types=R.PURCHASE_TYPES,
        purchase_statuses=R.PURCHASE_STATUSES,
        receiving_statuses=R.RECEIVING_STATUSES,
        purchase_payment_statuses=R.PURCHASE_PAYMENT_STATUSES,
        expense_types=R.EXPENSE_TYPES,
        expense_statuses=R.EXPENSE_STATUSES_SHEET,
        expense_payment_statuses=R.EXPENSE_PAYMENT_STATUSES,
        approval_statuses=R.APPROVAL_STATUSES,
        return_types=R.RETURN_TYPES,
        return_statuses=R.RETURN_STATUSES,
        payment_methods=R.PAYMENT_METHODS,
        units_of_measure=R.UNITS_OF_MEASURE,
    )


@router.get("/settings/completeness", response_model=CompanyCompleteness)
def company_completeness(
    admin: User = Depends(_ADMIN), db: Session = Depends(get_db)
) -> CompanyCompleteness:
    """Which mandatory Company Master fields are still blank, so the UI can show
    a "profile 18/24 complete" indicator and flag what is left."""
    org = _admin_org(admin, db)
    missing = [f for f in REQUIRED_COMPANY_FIELDS if not getattr(org, f, None)]
    return CompanyCompleteness(
        complete=not missing,
        filled=len(REQUIRED_COMPANY_FIELDS) - len(missing),
        total=len(REQUIRED_COMPANY_FIELDS),
        missing=missing,
        required=REQUIRED_COMPANY_FIELDS,
    )


def _numbered_branches(branches: list[dict]) -> list[dict]:
    """Give every branch a stable id so the front-end can edit or delete one."""
    return [{**b, "id": b.get("id") or str(uuid.uuid4())} for b in branches]


@router.post("/settings/logo", response_model=UploadResponse)
def upload_logo(
    request: Request,
    file: UploadFile = File(...),
    admin: User = Depends(_ADMIN),
    db: Session = Depends(get_db),
) -> UploadResponse:
    org = _admin_org(admin, db)
    org.logo_url = _store_image(db, org.id, file, request)
    activity_service.record(db, org.id, admin, "branding", "Company logo updated")
    db.commit()
    return UploadResponse(url=org.logo_url)


@router.post("/settings/signature", response_model=UploadResponse)
def upload_signature(
    request: Request,
    file: UploadFile = File(...),
    admin: User = Depends(_ADMIN),
    db: Session = Depends(get_db),
) -> UploadResponse:
    org = _admin_org(admin, db)
    org.signature_url = _store_image(db, org.id, file, request)
    activity_service.record(db, org.id, admin, "branding", "Authorized signature updated")
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


def _store_document(
    db: Session, org_id: str | None, file: UploadFile, request: Request
) -> tuple[str, int]:
    """Validate + store one document upload, returning its URL and byte size."""
    content_type = file.content_type or ""
    if not _is_document(content_type):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported file format for '{file.filename}' (allowed: PDF, PNG, JPG, DOCX)",
        )
    return save_upload(
        db, org_id, file, request, allow_any=True, max_bytes=_MAX_DOCUMENT_BYTES
    )


@router.post("/settings/upload-file", response_model=UploadResponse)
def upload_settings_file(
    request: Request,
    file: UploadFile = File(...),
    admin: User = Depends(_ADMIN),
    db: Session = Depends(get_db),
) -> UploadResponse:
    """Generic file uploader for company settings assets (images, PDFs, documents up to 5 MB)."""
    org = _admin_org(admin, db)
    url, _ = _store_document(db, org.id, file, request)
    db.commit()
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
    request: Request,
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
        url, size = _store_document(db, org.id, file, request)
        documents.append(
            {
                "id": str(uuid.uuid4()),
                "name": file.filename or "document",
                "url": url,
                "content_type": file.content_type,
                "size": size,
                "uploaded_at": datetime.now(timezone.utc).isoformat(),
            }
        )
    activity_service.record(
        db, org.id, admin, "document", "Document uploaded",
        f"{len(files)} other business document(s) uploaded",
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
    activity_service.record(db, org.id, admin, "document", "Document removed")
    return _save_documents(db, org, remaining)


@router.delete("/settings/documents/other", status_code=status.HTTP_204_NO_CONTENT)
def clear_other_documents(admin: User = Depends(_ADMIN), db: Session = Depends(get_db)) -> None:
    """Remove every "Other Business Document" for this company."""
    _save_documents(db, _admin_org(admin, db), [])


# ------------------------- Single business documents -------------------------
# GST certificate, PAN, incorporation certificate, trade licence, MSME, FSSAI —
# one file each. These are plain URL columns, so they can still be set through
# PUT /settings; the endpoints below just make "pick a file, it's saved" one call
# instead of upload-file followed by a settings update.
#
# Registered *after* the /documents/other routes on purpose: "other" is not a
# BusinessDocumentSlot, and a path parameter declared first would shadow it.


@router.post("/settings/documents/{slot}", response_model=CompanySettingsOut)
def upload_business_document(
    request: Request,
    slot: BusinessDocumentSlot,
    file: UploadFile = File(..., description="PDF / PNG / JPG / DOCX, max 5 MB"),
    admin: User = Depends(_ADMIN),
    db: Session = Depends(get_db),
) -> Organization:
    """Upload one of the single-file business documents. Replaces whatever was in
    that slot. The many-file slot is /settings/documents/other."""
    org = _admin_org(admin, db)
    url, _ = _store_document(db, org.id, file, request)
    setattr(org, slot.column, url)
    activity_service.record(
        db, org.id, admin, "document", "Document uploaded",
        f"{slot.value.replace('_', ' ').title()} uploaded",
    )
    db.commit()
    db.refresh(org)
    return org


@router.delete("/settings/documents/{slot}", response_model=CompanySettingsOut)
def clear_business_document(
    slot: BusinessDocumentSlot,
    admin: User = Depends(_ADMIN),
    db: Session = Depends(get_db),
) -> Organization:
    """Remove one of the single-file business documents."""
    org = _admin_org(admin, db)
    setattr(org, slot.column, None)
    activity_service.record(
        db, org.id, admin, "document", "Document removed",
        f"{slot.value.replace('_', ' ').title()} removed",
    )
    db.commit()
    db.refresh(org)
    return org


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
            "employee_count", "business_hours", "mission_vision", "notes",
            "employee_id_prefix", "branch_addresses"
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

