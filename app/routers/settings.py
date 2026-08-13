"""Per-organization workflow and invoice-template settings.

Both always belong to the authenticated user's firm — there is no organization id in
any path or body, so one firm's settings can never reach another's.
"""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core import workflow
from app.core.database import get_db
from app.core.deps import require_system_role
from app.models import Organization, SystemRole, User
from app.schemas.workflow_settings import (
    InvoiceSettings,
    InvoiceSettingsUpdate,
    SalesWorkflowSettings,
    SalesWorkflowSettingsUpdate,
)
from app.services import activity_service

router = APIRouter(tags=["settings"])

_ADMIN = require_system_role(SystemRole.ADMIN)


def _org(admin: User) -> Organization:
    org = admin.organization
    if org is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No organization")
    return org


def _apply(stored: dict | None, changes: dict) -> dict:
    """Merge a partial update into what is stored, one level deep for nested blocks."""
    result = dict(stored or {})
    for key, value in changes.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = {**result[key], **value}
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
    print, and the standing terms / footer. One record, used by both PDF formats."""
    return InvoiceSettings(**workflow.invoice_settings(_org(admin)))


@router.patch("/invoice-settings", response_model=InvoiceSettings)
def update_invoice_settings(
    payload: InvoiceSettingsUpdate,
    admin: User = Depends(_ADMIN),
    db: Session = Depends(get_db),
) -> InvoiceSettings:
    """Partial update. `branding` and `fields` merge key by key, so one toggle can be
    flipped without resending the rest.

    The logo and signature are ordinary uploads: POST /files/upload and send the
    `file_id` here. There is no separate logo endpoint.
    """
    org = _org(admin)
    changes = payload.model_dump(exclude_unset=True)
    # A nested block sent partially must merge, not replace, so drop the keys the
    # caller did not mention inside it.
    for block in ("branding", "fields"):
        if block in changes and changes[block] is not None:
            sent = getattr(payload, block).model_dump(exclude_unset=True)
            changes[block] = sent
        elif block in changes:
            del changes[block]
    if changes:
        org.invoice_template_settings = _apply(org.invoice_template_settings, changes)
        activity_service.record(
            db, org.id, admin, "branding", "Invoice template updated", ", ".join(sorted(changes))
        )
        db.commit()
        db.refresh(org)
    return InvoiceSettings(**workflow.invoice_settings(org))
