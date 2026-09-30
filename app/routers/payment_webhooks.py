"""Webhook router for organization-specific Razorpay payments (Part B).

Handles webhooks for invoice payment links using each organization's own Razorpay account.
"""

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.services import invoice_payment_link_service

router = APIRouter(prefix="/payments", tags=["payments"])


@router.post("/razorpay/webhook/{organization_id}", status_code=status.HTTP_200_OK)
async def org_razorpay_webhook(
    organization_id: str,
    request: Request,
    db: Session = Depends(get_db),
) -> dict:
    """Receive Razorpay webhooks for an organization's invoice payment links.

    No session authentication: authenticated via X-Razorpay-Signature header
    verified against the organization's stored webhook secret.
    """
    raw_body = await request.body()
    signature = request.headers.get("X-Razorpay-Signature", "")

    return invoice_payment_link_service.process_webhook(
        db,
        organization_id=organization_id,
        raw_body=raw_body,
        signature=signature,
    )
