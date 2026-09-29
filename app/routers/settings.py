"""Per-organization workflow and invoice-template settings.

Both always belong to the authenticated user's firm — there is no organization id in
any path or body, so one firm's settings can never reach another's.
"""

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile, status
from sqlalchemy.orm import Session

from app.core import workflow
from app.core.database import get_db
from app.core.deps import require_system_role
from app.core.files import save_upload
from app.models import Organization, SystemRole, User
from app.schemas.theme import OrganizationThemeOut, OrganizationThemeUpdate
from app.schemas.workflow_settings import (
    InvoiceSettings,
    InvoiceSettingsUpdate,
    SalesWorkflowSettings,
    SalesWorkflowSettingsUpdate,
)
from app.services import activity_service, theme_service

router = APIRouter(tags=["settings"])

_ADMIN = require_system_role(SystemRole.ADMIN)


def _org(admin: User) -> Organization:
    org = admin.organization
    if org is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No organization")
    return org


def _apply(stored: dict | None, changes: dict) -> dict:
    """Merge a partial update into what is stored, recursively for nested blocks."""
    result = dict(stored or {})
    for key, value in changes.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _apply(result[key], value)
        else:
            result[key] = value
    return result


# --------------------------- sales workflow -------------------------------


@router.get("/sales-workflow-settings", response_model=SalesWorkflowSettings)
def get_sales_workflow_settings(
    admin: User = Depends(_ADMIN), db: Session = Depends(get_db)
) -> SalesWorkflowSettings:
    """How this firm's sales flow behaves, with every default filled in.

    The one worth knowing: `order_requires_approval` is **false** by default, so an
    order is validated, reserved and placed on creation. An Admin is for
    organization control and exceptions, not a step in every sale.
    """
    return SalesWorkflowSettings(**workflow.sales_settings(_org(admin)))


@router.patch("/sales-workflow-settings", response_model=SalesWorkflowSettings)
def update_sales_workflow_settings(
    payload: SalesWorkflowSettingsUpdate,
    admin: User = Depends(_ADMIN),
    db: Session = Depends(get_db),
) -> SalesWorkflowSettings:
    """Change one or more settings. Takes effect on the next order — nothing is
    copied onto existing records."""
    org = _org(admin)
    changes = payload.model_dump(exclude_unset=True, exclude_none=True)
    if changes:
        org.sales_workflow_settings = _apply(org.sales_workflow_settings, changes)
        activity_service.record(
            db, org.id, admin, "company_profile", "Sales workflow settings updated",
            ", ".join(sorted(changes)),
        )
        db.commit()
        db.refresh(org)
    return SalesWorkflowSettings(**workflow.sales_settings(org))


# --------------------------- invoice template ------------------------------


@router.get("/invoice-settings", response_model=InvoiceSettings)
def get_invoice_settings(
    admin: User = Depends(_ADMIN), db: Session = Depends(get_db)
) -> InvoiceSettings:
    """The firm's invoice look: template, paper size, branding, which fields to
    print, typography, item table columns, print settings, and the standing terms / footer.
    One record, used by invoice PDF generation."""
    return InvoiceSettings(**workflow.invoice_settings(_org(admin)))


@router.patch("/invoice-settings", response_model=InvoiceSettings)
def update_invoice_settings(
    payload: InvoiceSettingsUpdate,
    admin: User = Depends(_ADMIN),
    db: Session = Depends(get_db),
) -> InvoiceSettings:
    """Partial update. Nested blocks merge key by key, so one toggle can be
    flipped without resending the rest.

    The logo and signature are ordinary uploads: POST /files/upload and send the
    `file_id` here. There is no separate logo endpoint.
    """
    org = _org(admin)
    changes = payload.model_dump(exclude_unset=True)
    if changes:
        org.invoice_template_settings = _apply(org.invoice_template_settings, changes)
        activity_service.record(
            db, org.id, admin, "branding", "Invoice template updated", ", ".join(sorted(changes))
        )
        db.commit()
        db.refresh(org)
    return InvoiceSettings(**workflow.invoice_settings(org))


# --------------------------------- theme -----------------------------------
# Organization appearance customization (organization_themes — one row per
# org at most). `custom_enabled`, not row existence, is what tells the
# frontend whether to apply this instead of the default CRM look; GET
# returns a complete configuration even before any row has ever been written.

_THEME_ALLOWED_IMAGE_TYPES = {"image/png", "image/jpeg", "image/webp"}
_THEME_MAX_UPLOAD_BYTES = 5 * 1024 * 1024  # 5 MB


def _check_theme_image_type(file: UploadFile) -> None:
    """PNG / JPEG / WebP only — an exact allow-list, deliberately stricter
    than app.core.files._check_type's generic "image/" prefix check (which
    would also accept e.g. image/gif or image/svg+xml)."""
    content_type = (file.content_type or "").lower()
    if content_type not in _THEME_ALLOWED_IMAGE_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="File must be PNG, JPG/JPEG, or WebP",
        )


@router.get("/organization/theme", response_model=OrganizationThemeOut)
def get_organization_theme(
    admin: User = Depends(_ADMIN), db: Session = Depends(get_db)
) -> OrganizationThemeOut:
    """The firm's complete appearance configuration. Returns the documented
    defaults if the firm has never customized its theme — `custom_enabled`
    is false in that case, and the frontend should keep using the existing
    CRM look regardless of the rest of this response."""
    org = _org(admin)
    theme = theme_service.get_theme(db, org)
    return OrganizationThemeOut.from_theme_dict(theme_service.theme_dict(theme))


@router.patch("/organization/theme", response_model=OrganizationThemeOut)
def update_organization_theme(
    payload: OrganizationThemeUpdate,
    admin: User = Depends(_ADMIN),
    db: Session = Depends(get_db),
) -> OrganizationThemeOut:
    """Change one or more theme fields. Omitted fields are left exactly as
    they were — this is the only place `custom_enabled` is ever flipped."""
    org = _org(admin)
    changes = payload.model_dump(exclude_unset=True)
    theme = theme_service.get_theme(db, org)
    if changes:
        theme = theme_service.get_or_create_theme(db, org)
        for field, value in changes.items():
            setattr(theme, field, value)
        activity_service.record(
            db, org.id, admin, "branding", "Theme updated", ", ".join(sorted(changes))
        )
        db.commit()
        db.refresh(theme)
    return OrganizationThemeOut.from_theme_dict(theme_service.theme_dict(theme))


@router.post("/organization/theme/background", response_model=OrganizationThemeOut)
def upload_theme_background(
    request: Request,
    file: UploadFile = File(...),
    admin: User = Depends(_ADMIN),
    db: Session = Depends(get_db),
) -> OrganizationThemeOut:
    """Upload the theme's background image (PNG/JPEG/WebP, up to 5 MB).

    Stored as an ordinary /files/{id} reference via the existing upload
    pipeline — never a signed R2 URL, never a hardcoded host. Replacing an
    existing background does not delete the previous StoredFile row (same
    convention as Organization's own logo/signature upload endpoints) —
    only the theme's own reference is updated.
    """
    org = _org(admin)
    _check_theme_image_type(file)
    url, _size = save_upload(db, org.id, file, request, allow_any=True, max_bytes=_THEME_MAX_UPLOAD_BYTES)

    theme = theme_service.get_or_create_theme(db, org)
    theme.background_image_url = url
    activity_service.record(db, org.id, admin, "branding", "Background uploaded")
    db.commit()
    db.refresh(theme)
    return OrganizationThemeOut.from_theme_dict(theme_service.theme_dict(theme))


@router.post("/organization/theme/logo", response_model=OrganizationThemeOut)
def upload_theme_logo(
    request: Request,
    file: UploadFile = File(...),
    admin: User = Depends(_ADMIN),
    db: Session = Depends(get_db),
) -> OrganizationThemeOut:
    """Upload the theme's own logo (PNG/JPEG/WebP, up to 5 MB).

    Deliberately separate from Organization.logo_url (invoices/company
    branding/documents) — this never reads or writes that field in either
    direction, so changing one can never surprise the other.
    """
    org = _org(admin)
    _check_theme_image_type(file)
    url, _size = save_upload(db, org.id, file, request, allow_any=True, max_bytes=_THEME_MAX_UPLOAD_BYTES)

    theme = theme_service.get_or_create_theme(db, org)
    theme.logo_url = url
    activity_service.record(db, org.id, admin, "branding", "Logo changed")
    db.commit()
    db.refresh(theme)
    return OrganizationThemeOut.from_theme_dict(theme_service.theme_dict(theme))

