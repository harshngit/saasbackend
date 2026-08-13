from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import get_current_user, require_permission, require_unlocked_org
from app.core.pdf_docs import delivery_receipt_pdf
from app.models import (
    Customer,
    Product,
    ProductVariant,
    SalesOrder,
    User,
    VehicleLoading,
    VehicleLoadingItem,
    Delivery,
    DeliveryItem,
)
from app.services import numbering_service
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
            # The goods axis, not the order's own status. Everything still to do:
            # planned once a partner is named, then loaded, in transit, or part
            # delivered. `fulfilment_status` on each row is what the app shows —
            # being assigned is not the same as being out for delivery.
            SalesOrder.fulfilment_status.in_(
                ("planned", "loaded", "in_transit", "partially_delivered")
            ),
        )
    )
    return q.order_by(SalesOrder.created_at.desc()).all()


# ------------------------------- Delivery Notes (Ext) -------------------------------

from app.schemas.delivery import DeliveryNoteCreate, DeliveryNoteOut


@router.post("/notes", response_model=DeliveryNoteOut, status_code=status.HTTP_201_CREATED)
def create_delivery_note(
    payload: DeliveryNoteCreate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Delivery:
    org_id = _org_id(user)

    # Validate customer
    if payload.customer_id:
        cust = db.get(Customer, payload.customer_id)
        if cust is None or cust.organization_id != org_id:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid customer_id")

    # Validate order
    if payload.sales_order_id:
        order = db.get(SalesOrder, payload.sales_order_id)
        if order is None or order.organization_id != org_id:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid sales_order_id")

    delivery = Delivery(
        organization_id=org_id,
        delivery_note_number=payload.delivery_note_number or numbering_service.next_number(
            db, org_id, Delivery.delivery_note_number, "DN"
        ),
        delivery_date=payload.delivery_date or datetime.now(timezone.utc),
        sales_order_id=payload.sales_order_id,
        customer_id=payload.customer_id,
        warehouse=payload.warehouse,
        delivery_address=payload.delivery_address,
        delivery_status=payload.delivery_status or "pending",
    )

    for item in payload.items:
        # Resolve product name
        prod = db.get(Product, item.product_id) if item.product_id else None
        prod_name = prod.name if prod else "Unknown Item"
        if item.variant_id:
            variant = db.get(ProductVariant, item.variant_id)
            if variant:
                prod_name = f"{prod_name} ({variant.name})"

        delivery.items.append(
            DeliveryItem(
                product_id=item.product_id,
                variant_id=item.variant_id,
                product_name=prod_name,
                delivered_quantity=item.delivered_quantity,
            )
        )

    db.add(delivery)
    db.commit()
    db.refresh(delivery)
    return delivery


@router.get("/notes", response_model=list[DeliveryNoteOut])
def list_delivery_notes(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[Delivery]:
    return (
        db.query(Delivery)
        .filter(Delivery.organization_id == _org_id(user))
        .order_by(Delivery.delivery_date.desc())
        .all()
    )


@router.get("/notes/{id}", response_model=DeliveryNoteOut)
def get_delivery_note_detail(
    id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Delivery:
    d = db.get(Delivery, id)
    if d is None or d.organization_id != _org_id(user):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Delivery note not found")
    return d


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
        order.fulfilment_status = "delivered"
        order.status = "completed"
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
        order.fulfilment_status = "partially_delivered"
        order.status = "processing"
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
        order.fulfilment_status = "failed"
        order.status = "processing"
        order.reject_reason = payload.reason

    elif payload.status == "Rescheduled":
        # Still out with the partner; only the reason is recorded.
        order.fulfilment_status = "in_transit"
        order.status = "processing"
        order.reject_reason = f"Rescheduled: {payload.reason}"

    db.commit()
    db.refresh(order)
    return {
        "status": "success",
        "order_status": order.status,
        "fulfilment_status": order.fulfilment_status,
        "reject_reason": order.reject_reason,
    }


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


