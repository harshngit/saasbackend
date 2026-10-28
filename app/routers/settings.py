"""Per-organization workflow and invoice-template settings.

Both always belong to the authenticated user's firm — there is no organization id in
any path or body, so one firm's settings can never reach another's.
"""

import logging

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile, status
from sqlalchemy.orm import Session

from app.core import workflow
from app.core.database import get_db
from app.core.deps import get_current_user, require_entitlement, require_permission, require_system_role
from app.core.files import delete_replaced_file, save_upload
from app.models import Organization, StoredFile, SystemRole, User
from app.schemas.theme import OrganizationThemeOut, OrganizationThemeUpdate
from app.schemas.payment_gateway import PaymentGatewayIn, PaymentGatewayOut
from app.schemas.workflow_settings import (
    InvoiceSettings,
    InvoiceSettingsUpdate,
    InvoiceTemplateCatalog,
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


# --------------------------- invoice template ------------------------------


@router.get(
    "/invoice-templates",
    response_model=InvoiceTemplateCatalog,
    dependencies=[Depends(require_entitlement("invoice.sales_invoices"))],
)
def list_invoice_templates(
    user: User = Depends(require_permission("invoices", "view")),
) -> InvoiceTemplateCatalog:
    """Every invoice theme and its stable id, plus which ones this firm has chosen.

    Readable by anyone who can view invoices (unlike /invoice-settings, which is
    admin-only); changing the choice still goes through PATCH /invoice-settings.
    """
    stored = workflow.invoice_settings(_org(user))
    variant = str(stored.get("template_variant") or "").strip().lower()
    layout = str((stored.get("thermal_print") or {}).get("layout") or "").strip().lower()
    return InvoiceTemplateCatalog(
        selected={
            "template": stored.get("template") or "classic",
            "template_variant": stored.get("template_variant"),
            # What the PDF actually renders: the stored theme if the catalog knows it,
            # else the base template's own theme.
            "resolved_template_variant": variant if variant in workflow.REGULAR_THEMES_BY_ID
            else (stored.get("template") if stored.get("template") in workflow.REGULAR_THEMES_BY_ID else "classic"),
            "thermal_layout": layout if layout in workflow.THERMAL_LAYOUTS else "thermal-compact",
        },
        regular=list(workflow.REGULAR_THEMES),
        thermal=list(workflow.THERMAL_THEMES),
    )


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
    """Call after the theme change is committed: deletes the old background's StoredFile
    row, R2 object and storage usage, only if it is this firm's file and nothing else
    (a logo set to the same URL, say) still uses it."""
    if background_url:
        delete_replaced_file(db, background_url, org_id)


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

    theme.background_image_url = url
    activity_service.record(db, org.id, admin, "branding", "Background uploaded")
    db.commit()
    # Delete the previous background only now that the new one is committed.
    if old_bg_url and old_bg_url != url:
        _delete_background_file(db, org.id, old_bg_url)
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
        old_bg_url = theme.background_image_url
        theme.background_image_url = None
        activity_service.record(db, org.id, admin, "branding", "Background removed")
        db.commit()
        _delete_background_file(db, org.id, old_bg_url)
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
        old_bg_url = theme.background_image_url
        theme.custom_enabled = False
        theme.mode = "light"
        theme.primary_color = None
        theme.background_image_url = None
        theme.overlay_opacity = 0.45
        activity_service.record(db, org.id, admin, "branding", "Theme reset to defaults")
        db.commit()
        _delete_background_file(db, org.id, old_bg_url)
        db.refresh(theme)
    return OrganizationThemeOut.from_theme(theme)


# --------------------------- payment gateway ------------------------------


@router.get("/settings/payment-gateway", response_model=PaymentGatewayOut)
@router.get("/payment-gateway", response_model=PaymentGatewayOut, include_in_schema=False)
def get_payment_gateway(
    admin: User = Depends(_ADMIN), db: Session = Depends(get_db)
) -> PaymentGatewayOut:
    """Return the organization's Razorpay payment gateway configuration.

    Secrets are never exposed.
    """
    from app.services import invoice_payment_link_service

    org = _org(admin)
    gateway = invoice_payment_link_service.get_gateway(db, org.id)
    return invoice_payment_link_service.format_gateway_out(gateway, org.id)


@router.put("/settings/payment-gateway", response_model=PaymentGatewayOut)
@router.put("/payment-gateway", response_model=PaymentGatewayOut, include_in_schema=False)
def put_payment_gateway(
    payload: PaymentGatewayIn,
    admin: User = Depends(_ADMIN),
    db: Session = Depends(get_db),
) -> PaymentGatewayOut:
    """Configure or update the organization's Razorpay API credentials."""
    from app.services import invoice_payment_link_service

    org = _org(admin)
    gateway = invoice_payment_link_service.upsert_gateway(db, org.id, payload)
    return invoice_payment_link_service.format_gateway_out(gateway, org.id)


@router.delete("/settings/payment-gateway", status_code=status.HTTP_200_OK)
@router.delete("/payment-gateway", status_code=status.HTTP_200_OK, include_in_schema=False)
def delete_payment_gateway(
    admin: User = Depends(_ADMIN), db: Session = Depends(get_db)
) -> dict:
    """Remove the organization's payment gateway credentials."""
    from app.services import invoice_payment_link_service

    org = _org(admin)
    invoice_payment_link_service.delete_gateway(db, org.id)
    return {"detail": "Payment gateway removed"}


@router.post("/settings/payment-gateway/test", response_model=PaymentGatewayOut)
@router.post("/payment-gateway/test", response_model=PaymentGatewayOut, include_in_schema=False)
def test_payment_gateway(
    admin: User = Depends(_ADMIN), db: Session = Depends(get_db)
) -> PaymentGatewayOut:
    """Test the stored credentials against Razorpay and record verified_at on success."""
    from app.services import invoice_payment_link_service

    org = _org(admin)
    gateway = invoice_payment_link_service.test_gateway(db, org.id)
    return invoice_payment_link_service.format_gateway_out(gateway, org.id)
