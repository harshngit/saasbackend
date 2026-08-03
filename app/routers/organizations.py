import base64

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import require_system_role
from app.models import Organization, Plan, SystemRole, User
from app.schemas.company import (
    CompanySettingsOut,
    CompanySettingsUpdate,
    FieldSettingsOut,
    FieldSettingsUpdate,
    UploadResponse,
)
from app.schemas.organization import OrganizationOut, UpgradeRequest
from app.services import org_service

router = APIRouter(prefix="/organizations", tags=["organizations"])

_ADMIN = require_system_role(SystemRole.ADMIN)
_MAX_UPLOAD_BYTES = 1024 * 1024  # 1 MB cap for logo/signature images


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
    """Partial update of the company profile (send only changed fields)."""
    org = _admin_org(admin, db)
    for field, value in payload.model_dump(exclude_unset=True).items():
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


@router.post("/settings/upload-file", response_model=UploadResponse)
def upload_settings_file(
    file: UploadFile = File(...),
    admin: User = Depends(_ADMIN),
) -> UploadResponse:
    """Generic file uploader for company settings assets (images, PDFs, documents up to 5 MB)."""
    content_type = file.content_type or ""
    is_allowed = (
        content_type.startswith("image/")
        or content_type == "application/pdf"
        or content_type.startswith("application/vnd")
        or content_type.endswith("document")
        or content_type == "application/octet-stream"
    )
    if not is_allowed:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Unsupported file format")
    
    content = file.file.read()
    if len(content) > 5 * 1024 * 1024:
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="File too large (max 5 MB)")
        
    encoded = base64.b64encode(content).decode("ascii")
    url = f"data:{content_type};base64,{encoded}"
    return UploadResponse(url=url)


# ------------------------------- Field Settings -------------------------------

AVAILABLE_FIELDS = {
    "company": {
        "mandatory": [
            "name", "legal_name", "business_type", "industry", "primary_mobile", "email",
            "registered_address", "city", "state", "country", "pin_code",
            "logo_url", "signature_url", "payment_qr_url",
            "currency", "timezone", "language", "tax_configuration", "invoice_settings",
            "auth_person_name", "auth_person_designation", "auth_person_mobile", "auth_person_email"
        ],
        "optional": [
            "date_of_incorporation", "cin_number", "gstin_pan", "description",
            "alternate_mobile", "landline", "website", "customer_support_number",
            "branch_address", "stamp_url", "letterhead_url", "banner_url",
            "upi_id", "bank_account_details", "bank_account_holder", "bank_ifsc", "bank_name",
            "facebook_url", "instagram_url", "linkedin_url", "twitter_url", "youtube_url", "whatsapp_number",
            "financial_year",
            "doc_gst_url", "doc_pan_url", "doc_coi_url", "doc_trade_license_url", "doc_msme_url", "doc_fssai_url", "doc_other_url",
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

