from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import get_current_user, require_permission, require_unlocked_org
from app.core.pdf_docs import delivery_receipt_pdf
from app.models import (
    Customer,
    SalesOrder,
    User,
    VehicleLoading,
    VehicleLoadingItem,
)
from app.schemas.delivery import DeliveryStatusUpdate
from app.schemas.sales_order import OrderItemOut, OrderOut

router = APIRouter(prefix="/deliveries", tags=["deliveries"])

_view = require_permission("deliveries", "view")
_edit = require_permission("deliveries", "edit")


def _org_id(user: User) -> str:
    if not user.organization_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No organization on this account")
    return user.organization_id


def _owned_order(db: Session, id: str, user: User) -> SalesOrder:
    order = db.get(SalesOrder, id)
    if order is None or order.organization_id != _org_id(user):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Delivery/Order not found")
    
    # Staff/Delivery Partner can only access their assigned deliveries
    if user.effective_system_role == "staff" and order.assigned_delivery_partner_id != user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="This delivery is not assigned to you")
    return order


@router.get("/assigned", response_model=list[OrderOut])
def list_assigned_deliveries(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[SalesOrder]:
    """Retrieve all active deliveries assigned to the logged-in delivery partner."""
    org_id = _org_id(user)
    # Check if delivery partner role
    q = (
        db.query(SalesOrder)
        .filter(
            SalesOrder.organization_id == org_id,
            SalesOrder.assigned_delivery_partner_id == user.id,
            SalesOrder.status == "out_for_delivery",
        )
    )
    return q.order_by(SalesOrder.created_at.desc()).all()


@router.get("/{id}")
def get_delivery_details(
    id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """Retrieve full details of one delivery, including customer outstanding balance."""
    order = _owned_order(db, id, user)
    customer = order.customer
    
    return {
        "id": order.id,
        "organization_id": order.organization_id,
        "order_number": order.order_number,
        "status": order.status,
        "source": order.source,
        "subtotal": order.subtotal,
        "discount": order.discount,
        "tax": order.tax,
        "total": order.total,
        "notes": order.notes,
        "items": [OrderItemOut.model_validate(item) for item in order.items],
        "created_at": order.created_at,
        "customer": {
            "id": customer.id if customer else None,
            "name": customer.name if customer else "Unknown",
            "business_name": customer.business_name if customer else None,
            "phone": customer.phone if customer else None,
            "email": customer.email if customer else None,
            "billing_address": customer.billing_address if customer else None,
            "delivery_address": customer.delivery_address if customer else None,
            "outstanding_balance": customer.outstanding_balance if customer else 0.0,
        } if customer else None
    }


@router.patch("/{id}/status")
def update_delivery_status(
    id: str,
    payload: DeliveryStatusUpdate,
    user: User = Depends(get_current_user),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> dict:
    """Update delivery outcome. Syncs with delivery partner's vehicle stock if source is vehicle."""
    org_id = _org_id(user)
    order = _owned_order(db, id, user)

    # Deliver outcome mapping
    # choices: Delivered | Partial | Failed | Rescheduled
    if payload.status == "Delivered":
        order.status = "delivered"
        order.reject_reason = None
        
        # If source is vehicle stock, increment driver's loading delivered_qty
        if order.source == "delivery_vehicle" and order.assigned_delivery_partner_id:
            loading = (
                db.query(VehicleLoading)
                .filter(
                    VehicleLoading.delivery_partner_id == order.assigned_delivery_partner_id,
                    VehicleLoading.organization_id == org_id,
                    VehicleLoading.status == "active",
                )
                .first()
            )
            if loading:
                for item in order.items:
                    match = next(
                        (
                            x
                            for x in loading.items
                            if x.product_id == item.product_id and x.variant_id == item.variant_id
                        ),
                        None,
                    )
                    if match:
                        match.delivered_qty += item.quantity

    elif payload.status == "Partial":
        order.status = "partially_delivered"
        order.reject_reason = payload.reason
        
        # Assume order items were partially delivered
        if order.source == "delivery_vehicle" and order.assigned_delivery_partner_id:
            loading = (
                db.query(VehicleLoading)
                .filter(
                    VehicleLoading.delivery_partner_id == order.assigned_delivery_partner_id,
                    VehicleLoading.organization_id == org_id,
                    VehicleLoading.status == "active",
                )
                .first()
            )
            if loading:
                for item in order.items:
                    match = next(
                        (
                            x
                            for x in loading.items
                            if x.product_id == item.product_id and x.variant_id == item.variant_id
                        ),
                        None,
                    )
                    if match:
                        match.delivered_qty += item.quantity

    elif payload.status == "Failed":
        order.status = "failed"
        order.reject_reason = payload.reason

    elif payload.status == "Rescheduled":
        # Keep it out for delivery, but log the rescheduling reason
        order.status = "out_for_delivery"
        order.reject_reason = f"Rescheduled: {payload.reason}"

    db.commit()
    db.refresh(order)
    return {"status": "success", "order_status": order.status, "reject_reason": order.reject_reason}


@router.get("/{id}/receipt")
def get_delivery_receipt(
    id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Response:
    """Download delivery receipt PDF."""
    order = _owned_order(db, id, user)
    if not order.customer:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Order has no customer details")

    pdf_bytes = delivery_receipt_pdf(user.organization, order.customer, order)
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="delivery-receipt-{order.order_number}.pdf"'},
    )
