"""Per-organization workflow and invoice-template settings.

Both always belong to the authenticated user's firm — there is no organization id in
any path or body, so one firm's settings can never reach another's.
"""

import logging

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile, status
from sqlalchemy.orm import Session

from app.core import workflow
from app.core.database import get_db
from app.core.deps import get_current_user, require_system_role
from app.core.files import save_upload
from app.models import Organization, StoredFile, SystemRole, User
from app.schemas.theme import OrganizationThemeOut, OrganizationThemeUpdate
from app.schemas.workflow_settings import (
    InvoiceSettings,
    InvoiceSettingsUpdate,
    SalesWorkflowSettings,
    SalesWorkflowSettingsUpdate,
)
from app.services import activity_service, theme_service

logger = logging.getLogger("crm.settings")

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
            db,
            org.id,
            admin,
            "company_profile",
            "Sales workflow settings updated",
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
            db,
            org.id,
            admin,
            "branding",
            "Invoice template updated",
            ", ".join(sorted(changes)),
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


def _delete_background_file(db: Session, org_id: str, background_url: str | None) -> None:
    """Safely delete the StoredFile row and R2 object for a background image URL."""
    if not background_url:
        return
    file_id = background_url.rsplit("/", 1)[-1]
    stored = db.get(StoredFile, file_id)
    if stored is None:
        return
    # Guard: only delete if the file belongs to this org or is unassigned
    if stored.organization_id is not None and stored.organization_id != org_id:
        return
    if stored.storage_key:
        from app.core import r2

        if not r2.delete_object(stored.storage_key):
            logger.warning(
                "R2 delete_object failed for background file_id=%s — deleting DB row anyway",
                file_id,
            )
    db.delete(stored)


@router.get("/organization/theme", response_model=OrganizationThemeOut)
def get_organization_theme(
    user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> OrganizationThemeOut:
    """The firm's appearance configuration. Accessible to all active users in the firm.
    Super Admin (no org context) and firms without a customized theme receive the
    documented defaults (custom_enabled=false)."""
    if not user.organization_id or not user.organization:
        return OrganizationThemeOut.from_theme(None)
    theme = theme_service.get_theme(db, user.organization)
    return OrganizationThemeOut.from_theme(theme)


@router.patch("/organization/theme", response_model=OrganizationThemeOut)
def update_organization_theme(
    payload: OrganizationThemeUpdate,
    admin: User = Depends(_ADMIN),
    db: Session = Depends(get_db),
) -> OrganizationThemeOut:
    """Change one or more theme fields (custom_enabled, mode, primary_color, overlay_opacity).
    Omitted fields are left exactly as they were. Unknown fields return 422."""
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
    return OrganizationThemeOut.from_theme(theme)


@router.post("/organization/theme/background", response_model=OrganizationThemeOut)
def upload_theme_background(
    request: Request,
    file: UploadFile = File(...),
    admin: User = Depends(_ADMIN),
    db: Session = Depends(get_db),
) -> OrganizationThemeOut:
    """Upload the theme's background image (PNG/JPEG/WebP, up to 5 MB).
    Replacing an existing background deletes the old StoredFile record and R2 object."""
    org = _org(admin)
    _check_theme_image_type(file)

    theme = theme_service.get_or_create_theme(db, org)
    old_bg_url = theme.background_image_url

    url, _size = save_upload(
        db, org.id, file, request, allow_any=True, max_bytes=_THEME_MAX_UPLOAD_BYTES
    )

    # Delete previous background file now that the new one is successfully saved
    if old_bg_url and old_bg_url != url:
        _delete_background_file(db, org.id, old_bg_url)

    theme.background_image_url = url
    activity_service.record(db, org.id, admin, "branding", "Background uploaded")
    db.commit()
    db.refresh(theme)
    return OrganizationThemeOut.from_theme(theme)


@router.delete("/organization/theme/background", response_model=OrganizationThemeOut)
def delete_theme_background(
    admin: User = Depends(_ADMIN),
    db: Session = Depends(get_db),
) -> OrganizationThemeOut:
    """Delete the theme's background image, setting background_image_url to null
    and removing the associated StoredFile and R2 object. Preserves all other theme settings."""
    org = _org(admin)
    theme = theme_service.get_theme(db, org)
    if theme and theme.background_image_url:
        _delete_background_file(db, org.id, theme.background_image_url)
        theme.background_image_url = None
        activity_service.record(db, org.id, admin, "branding", "Background removed")
        db.commit()
        db.refresh(theme)
    return OrganizationThemeOut.from_theme(theme)


@router.post("/organization/theme/reset", response_model=OrganizationThemeOut)
def reset_organization_theme(
    admin: User = Depends(_ADMIN),
    db: Session = Depends(get_db),
) -> OrganizationThemeOut:
    """Reset the theme to documented defaults: custom_enabled=false, mode=light,
    primary_color=null, overlay_opacity=0.45, and delete the associated background file."""
    org = _org(admin)
    theme = theme_service.get_theme(db, org)
    if theme is not None:
        if theme.background_image_url:
            _delete_background_file(db, org.id, theme.background_image_url)
        theme.custom_enabled = False
        theme.mode = "light"
        theme.primary_color = None
        theme.background_image_url = None
        theme.overlay_opacity = 0.45
        activity_service.record(db, org.id, admin, "branding", "Theme reset to defaults")
        db.commit()
        db.refresh(theme)
    return OrganizationThemeOut.from_theme(theme)
