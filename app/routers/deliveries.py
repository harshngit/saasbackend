from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import get_current_user, require_permission, require_unlocked_org
from app.core.pdf_docs import delivery_challan_pdf, delivery_receipt_pdf
from app.core import workflow
from app.models import (
    Customer,
    Product,
    ProductVariant,
    SalesOrder,
    User,
    Vehicle,
    VehicleLoading,
    VehicleLoadingItem,
    Delivery,
    DeliveryItem,
)
from app.services import delivery_service, numbering_service
from app.schemas.delivery import (
    DeliveryConfirm,
    DeliveryOut,
    DeliveryPlanCreate,
    DeliveryPlanUpdate,
    DeliveryStatusUpdate,
)
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


_create = require_permission("deliveries", "create")


def _owned_delivery(db: Session, delivery_id: str, user: User) -> Delivery:
    """A delivery by **its own id** — the identifier the whole flow uses.

    A field role only reaches the deliveries assigned to them; out of scope reads as
    not found, the same rule as everywhere else.
    """
    delivery = db.get(Delivery, delivery_id)
    if delivery is None or delivery.organization_id != _org_id(user):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Delivery not found")
    if (
        user.effective_system_role == "staff"
        and delivery.delivery_partner_id not in (None, user.id)
    ):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Delivery not found")
    return delivery


def _delivery_out(db: Session, delivery: Delivery) -> DeliveryOut:
    out = DeliveryOut.model_validate(delivery)
    for key, value in delivery_service.sync_delivery_view(db, delivery).items():
        setattr(out, key, value)
    out.vehicle = db.get(Vehicle, delivery.vehicle_id) if delivery.vehicle_id else None
    out.delivery_partner = (
        db.get(User, delivery.delivery_partner_id) if delivery.delivery_partner_id else None
    )
    return out


@router.post("", response_model=DeliveryOut, status_code=status.HTTP_201_CREATED)
def plan_delivery(
    payload: DeliveryPlanCreate,
    user: User = Depends(_create),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> DeliveryOut:
    """Plan a delivery for a sales order.

    Anyone with the `deliveries` create permission may plan one — a dispatch or
    warehouse role, not only an Admin.

    Naming a partner and a vehicle is **planning, not dispatch**: the delivery starts
    `planned`, nothing has moved, and the partner does not see it as live yet. Omit
    `items` to plan everything still outstanding on the order; send them to split an
    order across several deliveries. A quantity already planned into another delivery
    cannot be planned again.
    """
    org_id = _org_id(user)
    order = db.get(SalesOrder, payload.order_id)
    if order is None or order.organization_id != org_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="order_id is not an order in your firm"
        )
    if order.status == "cancelled":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="This order was cancelled"
        )
    if order.status == "awaiting_approval":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This order is still awaiting approval",
        )

    partner = None
    if payload.delivery_partner_id:
        partner = db.get(User, payload.delivery_partner_id)
        if partner is None or partner.organization_id != org_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="delivery_partner_id is not an employee in your firm",
            )
    if payload.vehicle_id:
        vehicle = db.get(Vehicle, payload.vehicle_id)
        if vehicle is None or vehicle.organization_id != org_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="vehicle_id is not a vehicle in your firm",
            )

    delivery = delivery_service.plan(
        db, user, order,
        delivery_partner=partner,
        vehicle_id=payload.vehicle_id,
        warehouse_id=payload.warehouse_id,
        scheduled_date=payload.scheduled_date,
        delivery_address=payload.delivery_address,
        notes=payload.notes,
        wanted=(
            [{"order_item_id": i.order_item_id, "planned_quantity": i.planned_quantity}
             for i in payload.items]
            if payload.items is not None else None
        ),
    )
    db.commit()
    db.refresh(delivery)
    return _delivery_out(db, delivery)


@router.get("", response_model=list[DeliveryOut])
def list_deliveries(
    user: User = Depends(_view),
    status_filter: str | None = Query(
        default=None, alias="status",
        description="planned | loaded | in_transit | partially_delivered | delivered | "
                    "failed | cancelled",
    ),
    order_id: str | None = Query(default=None),
    delivery_partner_id: str | None = Query(default=None),
    open_only: bool = Query(default=False, description="Only deliveries still out"),
    db: Session = Depends(get_db),
) -> list[DeliveryOut]:
    """The firm's deliveries, newest first. A field role sees only their own."""
    org_id = _org_id(user)
    query = db.query(Delivery).filter(Delivery.organization_id == org_id)
    if status_filter:
        query = query.filter(Delivery.status == status_filter)
    if order_id:
        query = query.filter(Delivery.sales_order_id == order_id)
    if delivery_partner_id:
        query = query.filter(Delivery.delivery_partner_id == delivery_partner_id)
    if open_only:
        query = query.filter(Delivery.status.in_(workflow.OPEN_DELIVERY_STATUSES))
    if user.effective_system_role == "staff":
        query = query.filter(Delivery.delivery_partner_id == user.id)
    return [
        _delivery_out(db, d) for d in query.order_by(Delivery.created_at.desc()).all()
    ]


@router.get("/by-id/{delivery_id}", response_model=DeliveryOut)
def get_delivery(
    delivery_id: str, user: User = Depends(_view), db: Session = Depends(get_db)
) -> DeliveryOut:
    """One delivery by its own id.

    Registered under /by-id/ because the older GET /deliveries/{id} takes a **Sales
    Order** id and is kept working for existing clients. New code should use this one
    (and the /{delivery_id}/… actions below, which are all Delivery-id based).
    """
    return _delivery_out(db, _owned_delivery(db, delivery_id, user))


@router.patch("/by-id/{delivery_id}", response_model=DeliveryOut)
def update_delivery(
    delivery_id: str,
    payload: DeliveryPlanUpdate,
    user: User = Depends(_edit),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> DeliveryOut:
    """Re-plan or dispatch a delivery.

    Setting `status` to `in_transit` is the **dispatch** step: it stamps
    `dispatched_at` and `dispatched_by_id`, and only then does the partner see the
    delivery as live. Goods must already be loaded. `cancelled` abandons a plan that
    has not been loaded.

    Recording the outcome is POST /deliveries/{delivery_id}/confirm, not this.
    """
    org_id = _org_id(user)
    delivery = _owned_delivery(db, delivery_id, user)
    data = payload.model_dump(exclude_unset=True)

    if data.get("delivery_partner_id"):
        partner = db.get(User, data["delivery_partner_id"])
        if partner is None or partner.organization_id != org_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="delivery_partner_id is not an employee in your firm",
            )
    if data.get("vehicle_id"):
        vehicle = db.get(Vehicle, data["vehicle_id"])
        if vehicle is None or vehicle.organization_id != org_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="vehicle_id is not a vehicle in your firm",
            )

    new_status = data.pop("status", None)
    for field, value in data.items():
        setattr(delivery, field, value)

    if new_status == "in_transit":
        delivery_service.dispatch(db, user, delivery)
    elif new_status == "cancelled":
        if delivery.loaded_total > 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Goods are already on the vehicle. Bring them back with the "
                       "end-of-day return instead of cancelling.",
            )
        delivery.status = "cancelled"
    elif new_status == "planned":
        if delivery.status not in ("planned", "loaded"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"A '{delivery.status}' delivery cannot go back to planned",
            )
        delivery.status = "planned"

    db.commit()
    db.refresh(delivery)
    return _delivery_out(db, delivery)


@router.post("/{delivery_id}/load", response_model=DeliveryOut)
def load_delivery(
    delivery_id: str,
    user: User = Depends(_edit),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> DeliveryOut:
    """Load the planned goods onto the partner's vehicle.

    This is the one place stock physically leaves the warehouse:

        warehouse on hand ↓   reservation consumed ↓   vehicle stock ↑   loaded_quantity ↑

    All of it in one transaction. Safe to call twice: a line can only ever be loaded
    up to its planned quantity, so a repeat call loads whatever is left and then
    nothing — the same units are never deducted twice.

    To load specific quantities rather than everything planned, use
    POST /vehicle-stock/loading with `delivery_id` and per-item quantities.
    """
    delivery = _owned_delivery(db, delivery_id, user)
    delivery_service.load(db, user, delivery, wanted=None)
    db.commit()
    db.refresh(delivery)
    return _delivery_out(db, delivery)


@router.post("/{delivery_id}/confirm", response_model=DeliveryOut)
def confirm_delivery(
    delivery_id: str,
    payload: DeliveryConfirm,
    user: User = Depends(_edit),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> DeliveryOut:
    """Record what was actually handed over, with the proof of delivery.

    Delivering less than was loaded leaves the rest **on the vehicle** — it is not put
    back into the warehouse. It stays there until a re-attempt or the end-of-day
    return. The delivery becomes `delivered` or `partially_delivered` accordingly, and
    the order follows across all of its deliveries.

    A failed attempt (`failed: true` with a `failure_reason`) hands nothing over and
    leaves vehicle stock untouched.

    POD photos and the signature are ordinary uploads: POST /files/upload and send the
    `file_id`s here.
    """
    delivery = _owned_delivery(db, delivery_id, user)
    delivery_service.confirm(
        db, user, delivery,
        lines=[
            {"delivery_item_id": i.delivery_item_id, "delivered_quantity": i.delivered_quantity}
            for i in payload.items
        ],
        pod_photo_file_ids=payload.pod_photo_file_ids,
        signature_file_id=payload.signature_file_id,
        notes=payload.notes,
        failed=payload.failed,
        failure_reason=payload.failure_reason,
    )
    db.commit()
    db.refresh(delivery)
    return _delivery_out(db, delivery)


@router.get("/{delivery_id}/challan/pdf")
def delivery_challan(
    delivery_id: str,
    user: User = Depends(_view),
    db: Session = Depends(get_db),
) -> Response:
    """The challan that travels with the vehicle.

    Goods-movement documentation only — it creates no revenue, receivable or payment,
    so no totals or balances appear on it.
    """
    delivery = _owned_delivery(db, delivery_id, user)
    order = db.get(SalesOrder, delivery.sales_order_id) if delivery.sales_order_id else None
    customer = db.get(Customer, delivery.customer_id) if delivery.customer_id else None
    partner = (
        db.get(User, delivery.delivery_partner_id) if delivery.delivery_partner_id else None
    )
    vehicle = db.get(Vehicle, delivery.vehicle_id) if delivery.vehicle_id else None
    body = delivery_challan_pdf(user.organization, delivery, order, customer, partner, vehicle)
    return Response(
        content=body,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'inline; filename="{delivery.delivery_note_number}.pdf"'
        },
    )


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

    # Move the order's own Delivery records with it. This older route reports an
    # outcome against the order; the Delivery is the record everything else reads, so
    # leaving it behind would have the partner's own deliveries disagree with the order.
    delivery_status = {
        "Delivered": "delivered",
        "Partial": "partially_delivered",
        "Failed": "failed",
        "Rescheduled": "in_transit",
    }.get(payload.status)
    if delivery_status:
        for delivery in db.query(Delivery).filter(
            Delivery.sales_order_id == order.id,
            Delivery.status.in_(workflow.OPEN_DELIVERY_STATUSES),
        ):
            delivery.status = delivery_status
            if delivery_status in ("delivered", "partially_delivered"):
                delivery.confirmed_at = delivery.confirmed_at or datetime.now(timezone.utc)
                for line in delivery.items:
                    if not line.delivered_quantity:
                        line.delivered_quantity = line.planned_quantity
            if delivery_status == "failed":
                delivery.failure_reason = payload.reason or delivery.failure_reason

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


