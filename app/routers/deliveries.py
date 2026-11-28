from datetime import date, datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from typing import Union

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, joinedload, lazyload

from app.core.database import get_db
from app.core.deps import get_current_user, has_permission, require_entitlement, require_permission, require_unlocked_org
from app.core.files import content_disposition, document_filename
from app.core import business_time, perf, scoping, workflow
from app.core.sorting import apply_sorting
from app.models.enums import UserRole
from app.models import (
    Customer,
    CustomerPayment,
    CustomerPaymentAllocation,
    Invoice,
    Product,
    ProductVariant,
    SalesOrder,
    User,
    Vehicle,
    VehicleLoading,
    VehicleLoadingItem,
    Delivery,
    DeliveryCollection,
    DeliveryCollectionAllocation,
    DeliveryHistory,
    DeliveryItem,
)
from app.core import notification_types as nt
from app.services import collection_dates, day_end_service
from app.services import (
    delivery_redistribution_service,
    delivery_service,
    notification_service,
    numbering_service,
    order_service,
    payment_service,
    stock_service,
)
from app.schemas.delivery import (
    AppDeliveryConfirm,
    AppDeliveryConfirmOut,
    CollectionAllocationIn,
    CollectionAllocationOut,
    CustomerCollectionCreate,
    DeliveryBatchLoadItemOut,
    DeliveryBatchLoadRequest,
    DeliveryBatchLoadResponse,
    DeliveryCapacityOut,
    DeliveryCollectionCreate,
    DeliveryCollectionOut,
    COLLECTION_STATUS_BY_RECONCILIATION,
    CollectionSummaryOut,
    CollectionTotals,
    DeliverySummaryOut,
    collection_status,
    DeliveryConfirm,
    DeliveryCustomerBrief,
    DeliveryHistoryOut,
    DeliveryNoteOut,
    DeliveryOut,
    DeliveryPartnerBrief,
    DeliveryPartnerOption,
    DeliveryPickBody,
    DeliveryPlanCreate,
    DeliveryPlanUpdate,
    DeliveryRejectBody,
    DeliveryStatusUpdate,
    DeliveryWarehouseBrief,
    VehicleBrief,
    CollectedCollectionOut,
    CollectedCollectionsViewOut,
    CollectionsPagination,
    CollectionsViewSummary,
    PendingCollectionCustomerOut,
    PendingCollectionsViewOut,
)
from app.core.pagination import paginate
from app.schemas.pagination import PaginatedResponse
from app.schemas.sales_order import OrderItemOut, OrderOut
from app.routers.sales_orders import _ORDER_LIST_LOADING, _order_context, _order_out

router = APIRouter(
    prefix="/deliveries",
    tags=["deliveries"],
    dependencies=[Depends(require_entitlement("sales.deliveries"))],
)
customer_payments_router = APIRouter(
    prefix="/customer-payments",
    tags=["customer-payments"],
    dependencies=[Depends(require_entitlement("sales.deliveries"))],
)


_view = require_permission("deliveries", "view")
_edit = require_permission("deliveries", "edit")

DELIVERY_SORTABLE_FIELDS = {
    "delivery_number": Delivery.delivery_note_number,
    "delivery_note_number": Delivery.delivery_note_number,
    "order_number": select(SalesOrder.order_number).where(SalesOrder.id == Delivery.sales_order_id).scalar_subquery(),
    "customer": select(Customer.name).where(Customer.id == Delivery.customer_id).scalar_subquery(),
    "customer_name": select(Customer.name).where(Customer.id == Delivery.customer_id).scalar_subquery(),
    "delivery_partner": select(User.name).where(User.id == Delivery.delivery_partner_id).scalar_subquery(),
    "vehicle": select(Vehicle.vehicle_number).where(Vehicle.id == Delivery.vehicle_id).scalar_subquery(),
    "sales_officer": select(User.name).select_from(SalesOrder).join(User, User.id == func.coalesce(SalesOrder.salesperson_id, SalesOrder.created_by)).where(SalesOrder.id == Delivery.sales_order_id).scalar_subquery(),
    "delivery_date": Delivery.delivery_date,
    "date": Delivery.delivery_date,
    "scheduled_date": Delivery.scheduled_date,
    "status": Delivery.status,
    "delivery_status": Delivery.delivery_status,
    "created_at": Delivery.created_at,
}

ASSIGNED_DELIVERY_SORTABLE_FIELDS = {
    "order_number": SalesOrder.order_number,
    "order_date": SalesOrder.order_date,
    "date": SalesOrder.order_date,
    "customer": select(Customer.name).where(Customer.id == SalesOrder.customer_id).scalar_subquery(),
    "customer_name": select(Customer.name).where(Customer.id == SalesOrder.customer_id).scalar_subquery(),
    "sales_officer": select(User.name).where(User.id == func.coalesce(SalesOrder.salesperson_id, SalesOrder.created_by)).scalar_subquery(),
    "total": SalesOrder.total,
    "total_amount": SalesOrder.total,
    "status": SalesOrder.status,
    "delivery_status": SalesOrder.fulfilment_status,
    "delivery_date": SalesOrder.delivery_date,
    "created_at": SalesOrder.created_at,
}

DELIVERY_NOTE_SORTABLE_FIELDS = {
    "delivery_note_number": Delivery.delivery_note_number,
    "delivery_number": Delivery.delivery_note_number,
    "delivery_date": Delivery.delivery_date,
    "customer": select(Customer.name).where(Customer.id == Delivery.customer_id).scalar_subquery(),
    "status": Delivery.status,
    "created_at": Delivery.created_at,
}

# ?status= on the collection history: the reported status names, plus the stored
# reconciliation_status values older clients send.
_COLLECTION_STATUS_FILTER = {
    "awaiting_reconciliation": "recorded",
    "recorded": "recorded",
    "reconciled": "reconciled",
    "voided": "voided",
    # Canonical names (collection_status) -- same stored states.
    "pending_approval": "recorded",
    "approved": "reconciled",
    # The app's collection_status names (To Collect -> Collected -> Completed).
    "collected": "recorded",
    "completed": "reconciled",
}

DELIVERY_COLLECTION_SORTABLE_FIELDS = {
    "collected_at": DeliveryCollection.collected_at,
    "business_date": DeliveryCollection.collected_at,
    "date": DeliveryCollection.collected_at,
    "reference": DeliveryCollection.reference,
    "payment_reference": DeliveryCollection.reference,
    "amount": DeliveryCollection.amount,
    "payment_mode": DeliveryCollection.payment_mode,
    "payment_method": DeliveryCollection.payment_mode,
    "status": DeliveryCollection.reconciliation_status,
    "reconciliation_status": DeliveryCollection.reconciliation_status,
    "collector": select(User.name).where(User.id == DeliveryCollection.delivery_partner_id).scalar_subquery(),
    "delivery_partner": select(User.name).where(User.id == DeliveryCollection.delivery_partner_id).scalar_subquery(),
    "customer": select(Customer.name).where(Customer.id == DeliveryCollection.customer_id).scalar_subquery(),
    "created_at": DeliveryCollection.created_at,
}


def _org_id(user: User) -> str:
    if not user.organization_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No organization on this account")
    return user.organization_id


def _require_delivery_transition(current: str, new: str) -> None:
    """workflow.validate_delivery_transition, translated to an HTTPException —
    the same centralized rules delivery_service.py's own functions validate against."""
    try:
        workflow.validate_delivery_transition(current, new)
    except workflow.DeliveryTransitionError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=exc.message) from exc


def _owned_order(db: Session, id: str, user: User) -> SalesOrder:
    order = db.get(SalesOrder, id)
    if order is None or order.organization_id != _org_id(user):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Delivery/Order not found")

    # An "own"-scope user (Delivery Partner, or any custom role with
    # data_scope=="own") can only reach the order/delivery assigned to them —
    # the same dynamic scope check every other module uses, not a hardcoded
    # role-name check. "all"-scope (Admin, or a custom org-wide role) is
    # unaffected. team_attributes=() deliberately opts Delivery out of Team
    # Scope widening (not one of the six confirmed CRM modules) even if a
    # role somehow holds data_scope=="team" here -- it behaves as own-only.
    if not scoping.owns_record(db, user, order, "assigned_delivery_partner_id", team_attributes=()):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="This delivery is not assigned to you")
    return order


@router.get("/assigned", response_model=PaginatedResponse[OrderOut])
def list_assigned_deliveries(
    page: int = Query(default=1, ge=1, description="Page number (1-indexed)"),
    page_size: int = Query(default=10, ge=1, le=100, description="Items per page (default 10)"),
    sort_by: str | None = Query(default=None, description="Field to sort by"),
    sort_order: str = Query(default="asc", description="Sort order: asc or desc"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> PaginatedResponse[OrderOut]:
    """Retrieve all active deliveries assigned to the logged-in delivery partner."""
    org_id = _org_id(user)
    # Check if delivery partner role
    q = (
        db.query(SalesOrder)
        .filter(
            SalesOrder.organization_id == org_id,
            SalesOrder.assigned_delivery_partner_id == user.id,
            SalesOrder.fulfilment_status.in_(
                ("planned", "loaded", "in_transit", "partially_delivered")
            ),
        )
    )
    q = apply_sorting(
        q,
        sort_by=sort_by,
        sort_order=sort_order,
        sortable_fields=ASSIGNED_DELIVERY_SORTABLE_FIELDS,
        default_order=[SalesOrder.created_at.desc(), SalesOrder.id.desc()],
        secondary_key=SalesOrder.id.desc(),
    )
    q = q.options(*_ORDER_LIST_LOADING)
    items, total, page, page_size, total_pages = paginate(q, page=page, page_size=page_size)
    # The same order response (and page preload) as GET /orders.
    ctx = _order_context(db, items)
    return PaginatedResponse(
        items=[_order_out(db, o, ctx=ctx) for o in items],
        total=total,
        page=page,
        page_size=page_size,
        total_pages=total_pages,
    )


# Who may load the Delivery Partner picker: whoever can place / edit an order (Create Order ->
# Home Delivery) or create / edit a delivery (assign a partner). Not a staff directory.
_PARTNER_PICKER = (("sales_orders", "create"), ("sales_orders", "edit"), ("deliveries", "create"),
                   ("deliveries", "edit"))


def require_partner_picker(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> User:
    if any(has_permission(db, user, module, action) for module, action in _PARTNER_PICKER):
        return user
    if not user.role_id:
        # Legacy account without a role: judge it by its legacy role's default matrix.
        from app.core.permissions import default_role_matrices
        from app.models.enums import STAFF_ROLE_NAME
        try:
            legacy = UserRole(user.role) if user.role else None
        except ValueError:
            legacy = None
        matrix = default_role_matrices().get(STAFF_ROLE_NAME.get(legacy, ""), {})
        if any(matrix.get(m, {}).get(a) for m, a in _PARTNER_PICKER):
            return user
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                        detail="You don't have permission to load delivery partners")


@router.get("/partners", response_model=list[DeliveryPartnerOption])
def list_delivery_partners(
    user: User = Depends(require_partner_picker),
    db: Session = Depends(get_db),
) -> list[DeliveryPartnerOption]:
    """Delivery Partner picker (Create Order -> Home Delivery, delivery assignment): active
    delivery partners of the caller's own organization -- id, name, is_active, role only.
    Needs Sales Orders > Create/Edit or Deliveries > Create/Edit (Admins always)."""
    org_id = _org_id(user)
    users = (
        db.query(User)
        .filter(
            User.organization_id == org_id,
            User.is_active.is_(True),
            or_(User.status == "active", User.status.is_(None)),
        )
        .all()
    )
    partners = sorted((u for u in users if delivery_service.is_delivery_partner(db, u)),
                      key=lambda u: ((u.name or "").lower(), u.id))
    return [DeliveryPartnerOption(id=u.id, name=u.name, is_active=True, role="delivery_partner") for u in partners]


_create = require_permission("deliveries", "create")


def _owned_delivery(db: Session, delivery_id: str, user: User) -> Delivery:
    """A delivery by **its own id** — the identifier the whole flow uses.

    An "own"-scope user (Delivery Partner, or any custom role with
    data_scope=="own") only reaches a Delivery actually assigned to them —
    the dynamic scope check every other module uses, not a hardcoded
    role-name check. Deliberately requires a real assignment: an unassigned
    Delivery (delivery_partner_id is None) is NOT visible to an own-scope
    user just because nobody else has claimed it yet. "all"-scope (Admin, or
    a custom org-wide role) is unaffected. Out of scope reads as not found,
    the same rule as everywhere else.
    """
    delivery = db.get(Delivery, delivery_id)
    if delivery is None or delivery.organization_id != _org_id(user):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Delivery not found")
    # team_attributes=(): Delivery is explicitly excluded from Team Scope.
    if not scoping.owns_record(db, user, delivery, "delivery_partner_id", team_attributes=()):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Delivery not found")
    return delivery


def _delivery_out(
    db: Session, delivery: Delivery, include_timeline: bool = False, ctx: dict | None = None
) -> DeliveryOut:
    """`ctx` (delivery_service.delivery_list_context) carries the per-delivery lookups
    preloaded for a list page. Without one, it is built here for this single delivery: the
    same fixed handful of queries, instead of ~4 stock queries per line (a 20-line
    delivery's response used to cost ~80 queries for the lines' `warehouse_available`)."""
    if ctx is None:
        ctx = delivery_service.delivery_list_context(db, [delivery])
    out = DeliveryOut.model_validate(delivery)
    # Raw internal status, before status's own public-mapping field_validator
    # ran — see DeliveryOut.internal_status: distinguishes "loaded" from
    # "in_transit", which the public `status` deliberately reports the same.
    out.internal_status = delivery.status
    # A planned delivery nobody is assigned to yet reads "unassigned", not "pending".
    out.status = workflow.public_delivery_status_for(delivery.status, delivery.delivery_partner_id)
    # Older deliveries were saved without scheduled_date. delivery_date holds the planned
    # date in that case (it is set to scheduled_date, or the creation time), the same
    # fallback the partner-availability check already uses.
    if out.scheduled_date is None:
        out.scheduled_date = delivery.delivery_date
    for key, value in delivery_service.sync_delivery_view(db, delivery, ctx).items():
        setattr(out, key, value)

    cust = delivery.customer
    if cust is None and delivery.customer_id:
        cust = db.get(Customer, delivery.customer_id)
    if cust is None and delivery.sales_order and delivery.sales_order.customer:
        cust = delivery.sales_order.customer
    if cust:
        cust_brief = DeliveryCustomerBrief.model_validate(cust)
        cust_brief.previous_pending_balance = out.previous_pending_balance
        out.customer = cust_brief
    else:
        out.customer = None

    veh = db.get(Vehicle, delivery.vehicle_id) if delivery.vehicle_id else None
    out.vehicle = VehicleBrief.model_validate(veh) if veh else None

    partner = (
        db.get(User, delivery.delivery_partner_id) if delivery.delivery_partner_id else None
    )
    out.delivery_partner = DeliveryPartnerBrief.model_validate(partner) if partner else None

    resolved_wh = delivery_service.resolve_delivery_warehouse(db, delivery)
    if resolved_wh:
        out.warehouse = DeliveryWarehouseBrief.model_validate(resolved_wh)
        if not out.warehouse_id:
            out.warehouse_id = resolved_wh.id
    else:
        out.warehouse = None

    items_by_id = {item.id: item for item in (delivery.items or [])}
    for line_out in out.items:
        orm_item = items_by_id.get(line_out.id)
        prod = orm_item.product if orm_item else None
        var = orm_item.variant if orm_item else None
        line_out.uom = prod.uom if prod and prod.uom else None
        line_out.weight_kg = delivery_service.compute_weight_kg(prod, var)
        if resolved_wh and line_out.product_id:
            line_out.warehouse_available = delivery_service.available_for_delivery_line(
                db,
                resolved_wh.id,
                delivery,
                orm_item,
                line_out.product_id,
                line_out.variant_id,
                ctx=ctx,
            )
        else:
            line_out.warehouse_available = None

    # Only the detail endpoint asks for this — see the docstring on
    # DeliveryOut.timeline. No join to `users`: DeliveryHistory carries its
    # own denormalized actor_id/actor_name (see DeliveryHistoryOut._build_actor).
    if include_timeline:
        rows = (
            db.query(DeliveryHistory)
            .filter(
                DeliveryHistory.organization_id == delivery.organization_id,
                DeliveryHistory.delivery_id == delivery.id,
            )
            .order_by(DeliveryHistory.created_at.asc(), DeliveryHistory.id.asc())
            .all()
        )
        out.timeline = [DeliveryHistoryOut.model_validate(r) for r in rows]

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
    if order.status in ("draft", "cancelled", "pending", "rejected", "awaiting_approval"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Order must be confirmed before delivery can be planned",
        )
    if order.fulfilment_method == "pickup":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot plan delivery for a pickup order",
        )

    partner = None
    if payload.delivery_partner_id:
        partner = db.get(User, payload.delivery_partner_id)
        if partner is None or partner.organization_id != org_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="delivery_partner_id is not an employee in your firm",
            )
        if not delivery_service.is_delivery_partner(db, partner):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Selected user is not a delivery partner",
            )
    if payload.vehicle_id:
        vehicle = db.get(Vehicle, payload.vehicle_id)
        if vehicle is None or vehicle.organization_id != org_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="vehicle_id is not a vehicle in your firm",
            )
        if not vehicle.is_active or vehicle.status != "active":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Vehicle '{vehicle.vehicle_number}' is {vehicle.status} and cannot be assigned to new deliveries",
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
    if partner is not None:
        delivery_service.notify_delivery_assignment(
            db,
            delivery=delivery,
            new_partner_id=partner.id,
            previous_partner_id=None,
        )
    db.commit()
    db.refresh(delivery)
    return _delivery_out(db, delivery)


_PICKING_STATUSES = ("not_started", "picking", "picked")


def _bad_filter(name: str, allowed) -> HTTPException:  # noqa: ANN001
    return HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        detail=f"{name} must be one of: " + ", ".join(allowed),
    )


def _csv_filter(value: str | None, name: str, allowed) -> list[str] | None:  # noqa: ANN001
    """A comma-separated filter value, lower-cased and checked against `allowed`."""
    if not value or not value.strip():
        return None
    wanted = [v.strip().lower() for v in value.split(",") if v.strip()]
    if any(v not in allowed for v in wanted):
        raise _bad_filter(name, allowed)
    return wanted


def _filtered_deliveries(
    db: Session,
    user: User,
    *,
    status_filter: str | None = None,
    internal_status: str | None = None,
    picking_status: str | None = None,
    order_id: str | None = None,
    delivery_partner_id: str | None = None,
    open_only: bool = False,
    search: str | None = None,
):
    """The deliveries a list (or its summary) covers: this firm's, narrowed by the
    filters, and only the caller's own for a field role. Every filter is applied in SQL
    before pagination, so totals count only matching deliveries."""
    org_id = _org_id(user)
    query = db.query(Delivery).filter(Delivery.organization_id == org_id)
    try:
        stored_statuses = workflow.delivery_statuses_for_filter(status_filter)
        has_partner = workflow.delivery_partner_condition_for_filter(status_filter)
    except ValueError:
        raise _bad_filter("status", ("all",) + workflow.DELIVERY_STATUS_FILTER_VALUES) from None
    if stored_statuses is not None:
        query = query.filter(Delivery.status.in_(stored_statuses))
    if has_partner is True:
        query = query.filter(Delivery.delivery_partner_id.is_not(None))
    elif has_partner is False:
        query = query.filter(Delivery.delivery_partner_id.is_(None))
    internal = _csv_filter(internal_status, "internal_status", workflow.DELIVERY_STATUSES)
    if internal:
        query = query.filter(Delivery.status.in_(internal))
    picking = _csv_filter(picking_status, "picking_status", _PICKING_STATUSES)
    if picking:
        query = query.filter(Delivery.picking_status.in_(picking))
    if order_id:
        query = query.filter(Delivery.sales_order_id == order_id)
    if delivery_partner_id:
        query = query.filter(Delivery.delivery_partner_id == delivery_partner_id)
    if open_only:
        query = query.filter(Delivery.status.in_(workflow.OPEN_DELIVERY_STATUSES))
    if search and search.strip():
        # delivery_number is a Python property, not a column: filtering on it raised and
        # every ?search= answered 500. The stored column is delivery_note_number.
        s = f"%{search.strip()}%"
        query = query.filter(
            or_(
                Delivery.delivery_note_number.ilike(s),
                Delivery.notes.ilike(s),
                Delivery.customer_id.in_(
                    select(Customer.id).where(Customer.organization_id == org_id, Customer.name.ilike(s))
                ),
                Delivery.sales_order_id.in_(
                    select(SalesOrder.id).where(
                        SalesOrder.organization_id == org_id, SalesOrder.order_number.ilike(s)
                    )
                ),
            )
        )
    return scoping.owned_by(query, db, user, Delivery.delivery_partner_id, team_columns=())


_STATUS_FILTER_DOC = (
    "The status shown on each delivery: unassigned | pending | accepted | in_transit | "
    "partially_delivered | delivered | rejected | returned | cancelled (case-insensitive; "
    "intransit / In Transit accepted; all or empty = no filter). Stored values "
    "planned | ready | loaded | failed also work."
)


@router.get("/summary", response_model=DeliverySummaryOut)
def delivery_summary(
    user: User = Depends(_view),
    order_id: str | None = Query(default=None),
    delivery_partner_id: str | None = Query(default=None),
    search: str | None = Query(default=None, description="Same matching as GET /deliveries"),
    db: Session = Depends(get_db),
) -> DeliverySummaryOut:
    """Delivery counts per status in one call: the same deliveries GET /deliveries lists
    with these filters (a field role counts only their own). One grouped query."""
    query = _filtered_deliveries(db, user, order_id=order_id, delivery_partner_id=delivery_partner_id, search=search)
    no_partner = Delivery.delivery_partner_id.is_(None)
    rows = (
        query.order_by(None)
        .with_entities(Delivery.status, no_partner, func.count(Delivery.id))
        .group_by(Delivery.status, no_partner)
        .all()
    )
    by_status = {key: 0 for key in workflow.PUBLIC_DELIVERY_STATUS_ORDER}
    by_internal = {key: 0 for key in workflow.DELIVERY_STATUSES}
    for stored, unassigned, count in rows:
        public = workflow.public_delivery_status_for(stored, None if unassigned else "assigned")
        by_status[public] = by_status.get(public, 0) + count
        by_internal[stored] = by_internal.get(stored, 0) + count
    return DeliverySummaryOut(total=sum(by_internal.values()), by_status=by_status, by_internal_status=by_internal)


@router.get("", response_model=PaginatedResponse[DeliveryOut])
def list_deliveries(
    user: User = Depends(_view),
    status_filter: str | None = Query(default=None, alias="status", description=_STATUS_FILTER_DOC),
    internal_status: str | None = Query(
        default=None,
        description="Stored workflow status, comma-separated: planned, accepted, rejected, ready, "
                    "loaded, in_transit, partially_delivered, delivered, failed, cancelled",
    ),
    picking_status: str | None = Query(
        default=None, description="Warehouse picking stage, comma-separated: not_started, picking, picked"
    ),
    order_id: str | None = Query(default=None),
    delivery_partner_id: str | None = Query(default=None),
    open_only: bool = Query(default=False, description="Only deliveries still out"),
    search: str | None = Query(
        default=None, description="matches delivery number, notes, customer name or order number"
    ),
    page: int = Query(default=1, ge=1, description="Page number (1-indexed)"),
    page_size: int = Query(default=10, ge=1, le=100, description="Items per page (default 10)"),
    sort_by: str | None = Query(default=None, description="Field to sort by"),
    sort_order: str = Query(default="asc", description="Sort order: asc or desc"),
    db: Session = Depends(get_db),
) -> PaginatedResponse[DeliveryOut]:
    """The firm's deliveries, newest first. A field role sees only their own."""
    query = _filtered_deliveries(
        db, user, status_filter=status_filter, internal_status=internal_status,
        picking_status=picking_status, order_id=order_id, delivery_partner_id=delivery_partner_id,
        open_only=open_only, search=search,
    )
    query = apply_sorting(
        query,
        sort_by=sort_by,
        sort_order=sort_order,
        sortable_fields=DELIVERY_SORTABLE_FIELDS,
        default_order=[Delivery.created_at.desc(), Delivery.id.desc()],
        secondary_key=Delivery.id.desc(),
    )
    # Customer, order and vehicle come in the page query itself, without their own
    # relationships (a customer's payments and collections, an order's lines...): the
    # response only reads their columns.
    query = query.options(
        joinedload(Delivery.customer).lazyload("*"),
        joinedload(Delivery.sales_order).lazyload("*"),
        joinedload(Delivery.vehicle).lazyload("*"),
    )
    items, total, page, page_size, total_pages = paginate(query, page=page, page_size=page_size)
    ctx = delivery_service.delivery_list_context(db, items)
    return PaginatedResponse(
        items=[_delivery_out(db, d, ctx=ctx) for d in items],
        total=total,
        page=page,
        page_size=page_size,
        total_pages=total_pages,
    )


@router.post("/{delivery_id}/pick", response_model=DeliveryOut)
def pick_items(
    delivery_id: str,
    payload: DeliveryPickBody,
    user: User = Depends(_edit),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> DeliveryOut:
    """Record picked quantities for a delivery (warehouse picking). This is a
    preparatory step: it does not move stock, only records how many units were
    picked from shelves for each delivery item.
    """
    delivery = _owned_delivery(db, delivery_id, user)
    if delivery.status != "accepted":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Delivery must be accepted before picking",
        )

    # Map items by id for quick lookup
    items_by_id = {i.id: i for i in delivery.items}
    any_changed = False
    for it in payload.items:
        di = items_by_id.get(it.delivery_item_id)
        if di is None:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Unknown delivery_item_id: {it.delivery_item_id}")
        if it.picked_quantity < 0 or it.picked_quantity > (di.planned_quantity or 0):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Invalid picked_quantity for item {it.delivery_item_id}")
        if di.picked_quantity != it.picked_quantity:
            di.picked_quantity = it.picked_quantity
            any_changed = True

    # Update picking_status
    previous_picking_status = delivery.picking_status
    if any(i.picked_quantity and i.picked_quantity > 0 for i in delivery.items):
        if all((i.picked_quantity or 0) >= (i.planned_quantity or 0) for i in delivery.items):
            delivery.picking_status = "picked"
        else:
            delivery.picking_status = "picking"
    else:
        delivery.picking_status = "not_started"

    if any_changed and delivery.picking_status != previous_picking_status:
        if delivery.picking_status == "picking" and previous_picking_status == "not_started":
            delivery_service.record_history(db, delivery, "picking_started", actor=user)
        elif delivery.picking_status == "picked":
            delivery_service.record_history(db, delivery, "picking_completed", actor=user)

    db.commit()
    db.refresh(delivery)
    return _delivery_out(db, delivery)


@router.post("/{delivery_id}/ready", response_model=DeliveryOut)
def mark_ready(
    delivery_id: str,
    user: User = Depends(_edit),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> DeliveryOut:
    """Mark a delivery as `ready` once picking is complete. This moves the
    delivery into the `ready` lifecycle step — loading may only proceed from
    `ready`.
    """
    delivery = _owned_delivery(db, delivery_id, user)
    if delivery.status != "accepted":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Can only mark accepted deliveries as ready",
        )
    if delivery.picking_status != "picked":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="All items must be fully picked before marking ready",
        )

    previous = delivery.status
    delivery.status = "ready"
    delivery_service.record_history(
        db, delivery, "ready", actor=user, previous_status=previous, new_status="ready"
    )
    db.commit()
    db.refresh(delivery)
    return _delivery_out(db, delivery)


@router.get("/by-id/{delivery_id}", response_model=DeliveryOut)
def get_delivery(
    delivery_id: str, user: User = Depends(_view), db: Session = Depends(get_db)
) -> DeliveryOut:
    """One delivery by its own id.

    Registered under /by-id/ because the older GET /deliveries/{id} takes a **Sales
    Order** id and is kept working for existing clients. New code should use this one
    (and the /{delivery_id}/… actions below, which are all Delivery-id based).

    Includes the full `timeline` — the only Delivery response that does; list
    responses deliberately leave it empty (see DeliveryOut.timeline).
    """
    return _delivery_out(db, _owned_delivery(db, delivery_id, user), include_timeline=True)


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
        delivery_service.require_partner_available(
            db, org_id, partner, delivery.scheduled_date or delivery.delivery_date
        )
    if data.get("vehicle_id"):
        vehicle = db.get(Vehicle, data["vehicle_id"])
        if vehicle is None or vehicle.organization_id != org_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="vehicle_id is not a vehicle in your firm",
            )
        if not vehicle.is_active or vehicle.status != "active":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Vehicle '{vehicle.vehicle_number}' is {vehicle.status} and cannot be assigned to new deliveries",
            )

    new_status = data.pop("status", None)
    was_rejected = (delivery.status == "rejected")
    previous_partner_id = delivery.delivery_partner_id
    for field, value in data.items():
        setattr(delivery, field, value)

    if "delivery_partner_id" in data and delivery.delivery_partner_id != previous_partner_id:
        delivery_service.record_history(
            db, delivery, "reassigned" if previous_partner_id else "assigned", actor=user,
            metadata={
                "previous_delivery_partner_id": previous_partner_id,
                "delivery_partner_id": delivery.delivery_partner_id,
            },
        )
        if delivery.delivery_partner_id:
            delivery_service.notify_delivery_assignment(
                db,
                delivery=delivery,
                new_partner_id=delivery.delivery_partner_id,
                previous_partner_id=previous_partner_id,
                is_reassignment=was_rejected or bool(previous_partner_id),
            )

    if new_status == "in_transit":
        delivery_service.dispatch(db, user, delivery)
    elif new_status == "cancelled":
        delivery_service.cancel(db, delivery, actor=user)
    elif new_status == "planned":
        # In this workflow only a rejected delivery can go back to planned — to be
        # reassigned to a new partner. (A loaded delivery cannot: physical stock has
        # already left the warehouse, so its status must not silently claim otherwise.)
        _require_delivery_transition(delivery.status, "planned")
        if delivery.status == "rejected":
            partner_id = data.get("delivery_partner_id") or delivery.delivery_partner_id
            if not partner_id:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="A new delivery partner is required to reassign a rejected delivery",
                )
        delivery.status = "planned"

    db.commit()
    db.refresh(delivery)
    return _delivery_out(db, delivery)


def _require_delivery_perm(action: str):  # noqa: ANN202
    """Deliveries > `action` (Admins / owners are not permission-checked). A legacy account
    with no role is judged by its legacy role's default matrix, as for collections."""

    def _guard(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> User:
        if has_permission(db, user, "deliveries", action):
            return user
        if not user.role_id:
            from app.core.permissions import default_role_matrices
            from app.models.enums import STAFF_ROLE_NAME
            try:
                legacy = UserRole(user.role) if user.role else None
            except ValueError:
                legacy = None
            if default_role_matrices().get(STAFF_ROLE_NAME.get(legacy, ""), {}).get("deliveries", {}).get(action):
                return user
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail=f"You don't have permission to {action} deliveries")

    return _guard


_accept_reject_guard = _require_delivery_perm("edit")
_note_create_guard = _require_delivery_perm("create")


@router.post("/{delivery_id}/accept", response_model=DeliveryOut)
def accept_delivery(
    delivery_id: str,
    user: User = Depends(_accept_reject_guard),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> DeliveryOut:
    """Accept a planned delivery. Partner-specific action that marks status as accepted."""
    delivery = _owned_delivery(db, delivery_id, user)
    delivery_service.accept(db, user, delivery)
    db.commit()
    db.refresh(delivery)
    return _delivery_out(db, delivery)


@router.post("/{delivery_id}/reject", response_model=DeliveryOut)
def reject_delivery(
    delivery_id: str,
    payload: DeliveryRejectBody = DeliveryRejectBody(),
    user: User = Depends(_accept_reject_guard),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> DeliveryOut:
    """Reject a planned delivery. Partner-specific action that clears partner & vehicle and notifies admins."""
    delivery = _owned_delivery(db, delivery_id, user)
    delivery_service.reject(db, user, delivery, reason=payload.reason if payload else None)
    db.commit()
    db.refresh(delivery)
    try:
        notification_service.notify_org_admins(
            db,
            organization_id=delivery.organization_id,
            title="Delivery rejected",
            body=f"{delivery.delivery_number} was rejected by the partner — needs reassignment",
            type="delivery",
            link=delivery.id,
            data=nt.build_data(nt.DELIVERY_REJECTED, delivery_id=delivery.id),
        )
        db.commit()
    except Exception:
        pass
    return _delivery_out(db, delivery)


@router.post("/load-batch", response_model=DeliveryBatchLoadResponse)
def load_deliveries_batch(
    payload: DeliveryBatchLoadRequest,
    user: User = Depends(_edit),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> DeliveryBatchLoadResponse:
    """Load multiple deliveries onto vehicles in a single batch.

    Each delivery is loaded independently: failure of one delivery does not rollback
    other successful deliveries. If a delivery is already loaded, ok=true is returned
    without double-deducting stock. If vehicle capacity is exceeded (when all weights are known),
    the batch is rejected before loading.
    """
    org_id = _org_id(user)
    if not payload.delivery_ids:
        return DeliveryBatchLoadResponse(results=[])

    # 1. First, validate access and gather deliveries to evaluate capacity
    deliveries_map = {}
    for d_id in payload.delivery_ids:
        try:
            d = _owned_delivery(db, d_id, user)
            deliveries_map[d_id] = d
        except Exception:
            # Will be handled during execution
            pass

    # 2. Check capacity across vehicle(s) for all deliveries in the batch that are not yet loaded
    deliveries_by_vehicle: dict[str, list[Delivery]] = {}
    for d_id, d in deliveries_map.items():
        if d.status in ("loaded", "in_transit", "partially_delivered", "delivered") or (
            d.planned_total > 0 and d.loaded_total >= d.planned_total
        ):
            continue

        veh_id = d.vehicle_id
        if not veh_id and d.delivery_partner_id:
            driver_veh = delivery_service.get_driver_active_vehicle(db, org_id, d.delivery_partner_id)
            if driver_veh:
                veh_id = driver_veh.id

        if veh_id:
            deliveries_by_vehicle.setdefault(veh_id, []).append(d)

    for veh_id, v_deliveries in deliveries_by_vehicle.items():
        veh = db.get(Vehicle, veh_id)
        if veh and veh.capacity_kg and veh.capacity_kg > 0:
            total_batch_weight = 0.0
            all_weights_known = True
            for d in v_deliveries:
                for item in (d.items or []):
                    per_unit_w = delivery_service.compute_weight_kg(item.product, item.variant)
                    qty = (item.planned_quantity or 0) - (item.loaded_quantity or 0)
                    if qty > 0:
                        if per_unit_w is None:
                            all_weights_known = False
                            break
                        total_batch_weight += qty * per_unit_w
                if not all_weights_known:
                    break

            if all_weights_known and round(total_batch_weight, 4) > round(veh.capacity_kg, 4):
                total_str = f"{round(total_batch_weight, 2):g}"
                cap_str = f"{round(veh.capacity_kg, 2):g}"
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Total batch weight ({total_str} kg) exceeds vehicle capacity ({cap_str} kg)",
                )

    # 3. Process each delivery in its own savepoint/transaction
    results: list[DeliveryBatchLoadItemOut] = []
    for d_id in payload.delivery_ids:
        try:
            with db.begin_nested():
                delivery = _owned_delivery(db, d_id, user)

                # Check if already loaded
                if delivery.status in ("loaded", "in_transit", "partially_delivered", "delivered") or (
                    delivery.planned_total > 0 and delivery.loaded_total >= delivery.planned_total
                ):
                    results.append(
                        DeliveryBatchLoadItemOut(
                            delivery_id=d_id,
                            ok=True,
                            detail="Delivery is already loaded",
                            message="Delivery is already loaded",
                            delivery=_delivery_out(db, delivery),
                        )
                    )
                    continue

                delivery_service.load(db, user, delivery, wanted=None)
                db.flush()
                db.refresh(delivery)
                results.append(
                    DeliveryBatchLoadItemOut(
                        delivery_id=d_id,
                        ok=True,
                        detail="Loaded successfully",
                        message="Loaded successfully",
                        delivery=_delivery_out(db, delivery),
                    )
                )
        except HTTPException as exc:
            # A structured error ({"code", "message"}, e.g. DAY_END_CLOSED) keeps its code and
            # readable message instead of a stringified dict.
            code, message = None, exc.detail
            if isinstance(exc.detail, dict):
                code = exc.detail.get("code")
                message = exc.detail.get("message") or exc.detail.get("detail") or str(exc.detail)
            message = str(message)
            results.append(
                DeliveryBatchLoadItemOut(
                    delivery_id=d_id,
                    ok=False,
                    detail=message,
                    delivery=None,
                    code=code,
                    message=message,
                )
            )
        except Exception as exc:
            results.append(
                DeliveryBatchLoadItemOut(
                    delivery_id=d_id,
                    ok=False,
                    detail=str(exc) if str(exc) else "An unexpected error occurred while loading this delivery",
                    message=str(exc) if str(exc) else "An unexpected error occurred while loading this delivery",
                    delivery=None,
                )
            )

    db.commit()
    return DeliveryBatchLoadResponse(results=results)


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
    # Stage timings go to the Server-Timing header and, for a request over 5 s, to one
    # "slow request" log line (app.core.perf).
    with perf.stage("load"):
        delivery = _owned_delivery(db, delivery_id, user)
    # An identical retry of a confirm already recorded (e.g. sent again after a client
    # timeout) changes nothing and gets the recorded result back with 200.
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
        receiver_name=payload.receiver_name,
        replay_check=True,
    )
    with perf.stage("commit"):
        db.commit()
    with perf.stage("response"):
        db.refresh(delivery)
        return _delivery_out(db, delivery)


@router.get("/{delivery_id}/app/delivery-capacity", response_model=DeliveryCapacityOut)
def app_delivery_capacity(
    delivery_id: str,
    user: User = Depends(_view),
    db: Session = Depends(get_db),
) -> DeliveryCapacityOut:
    """Delivery app: the most each line can receive right now.

    `max_allowed_delivery` is the line's own units still on the vehicle plus units
    other receivers on the **same vehicle run** (the partner's active vehicle
    loading) did not take — same product and variant only. Send it back as
    `expected_max` on POST /deliveries/{id}/app/confirm.
    """
    delivery = _owned_delivery(db, delivery_id, user)
    delivery_redistribution_service.require_own_target(user, delivery)
    return DeliveryCapacityOut.model_validate(delivery_redistribution_service.capacity(db, delivery))


@router.post("/{delivery_id}/app/confirm", response_model=AppDeliveryConfirmOut)
def app_confirm_delivery(
    delivery_id: str,
    payload: AppDeliveryConfirm,
    user: User = Depends(_edit),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> AppDeliveryConfirmOut:
    """Delivery app: confirm a hand-over that may exceed what was loaded for this
    delivery, using spare units from the same vehicle run.

    The extra units are first moved onto this delivery's lines from the donor lines
    (recorded as `quantity_reallocated` on both timelines), then the hand-over is
    recorded by exactly the same logic as POST /deliveries/{id}/confirm. Ordered
    quantities never change; no warehouse stock moves.

    409 when an item's `expected_max` no longer matches the server's figure.
    """
    delivery = _owned_delivery(db, delivery_id, user)
    # Before the spare units are moved between deliveries (not only in the confirm itself).
    day_end_service.require_stock_day_open(db, delivery.organization_id, delivery.delivery_partner_id)
    reallocations = delivery_redistribution_service.confirm(
        db, user, delivery,
        lines=[
            {
                "delivery_item_id": i.delivery_item_id,
                "delivered_quantity": i.delivered_quantity,
                "expected_max": i.expected_max,
            }
            for i in payload.items
        ],
        pod_photo_file_ids=payload.pod_photo_file_ids,
        signature_file_id=payload.signature_file_id,
        notes=payload.notes,
        failed=payload.failed,
        failure_reason=payload.failure_reason,
        receiver_name=payload.receiver_name,
    )
    db.commit()
    db.refresh(delivery)
    out = _delivery_out(db, delivery)
    return AppDeliveryConfirmOut(
        **out.model_dump(),
        reallocations=reallocations,
        **delivery_redistribution_service.delivered_amounts(db, delivery),
    )


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
    from app.core.pdf_docs import delivery_challan_pdf  # lazy: fpdf is only loaded when a PDF is actually built

    body = delivery_challan_pdf(user.organization, delivery, order, customer, partner, vehicle)
    return Response(
        content=body,
        media_type="application/pdf",
        headers={
            "Content-Disposition": content_disposition(document_filename(f"{delivery.delivery_note_number}.pdf"))
        },
    )


# ------------------------------- Delivery Notes (Ext) -------------------------------

from app.schemas.delivery import DeliveryNoteCreate, DeliveryNoteOut


@router.post("/notes", response_model=DeliveryNoteOut, status_code=status.HTTP_201_CREATED)
def create_delivery_note(
    payload: DeliveryNoteCreate,
    user: User = Depends(_note_create_guard),
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
            db, org_id, Delivery.delivery_note_number, "DO"
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


@router.get("/notes", response_model=PaginatedResponse[DeliveryNoteOut])
def list_delivery_notes(
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1, le=100),
    sort_by: str | None = Query(default=None, description="Field to sort by"),
    sort_order: str = Query(default="asc", description="Sort order: asc or desc"),
    search: str | None = None,
    order_id: str | None = None,
    customer_id: str | None = None,
    status: str | None = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> PaginatedResponse[DeliveryNoteOut]:
    org_id = _org_id(user)
    q = db.query(Delivery).filter(Delivery.organization_id == org_id)

    if order_id:
        q = q.filter(Delivery.sales_order_id == order_id)
    if customer_id:
        q = q.filter(Delivery.customer_id == customer_id)
    if status:
        q = q.filter(Delivery.status == status)

    if search:
        s = f"%{search.strip()}%"
        q = q.filter(
            or_(
                Delivery.delivery_number.ilike(s),
                Delivery.notes.ilike(s),
            )
        )

    q = apply_sorting(
        q,
        sort_by=sort_by,
        sort_order=sort_order,
        sortable_fields=DELIVERY_NOTE_SORTABLE_FIELDS,
        default_order=[Delivery.delivery_date.desc().nullslast(), Delivery.created_at.desc(), Delivery.id.desc()],
        secondary_key=Delivery.id.desc(),
    )
    return paginate(q, page=page, page_size=page_size)


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



# -------------------- Delivery Collection & Reconciliation --------------------

COLLECTIONS_APPROVE_DETAIL = (
    "You don't have permission to reconcile, void or approve delivery collections: this needs "
    "the Deliveries > Approve permission on your role."
)


def require_collections_approve(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> User:
    """Reconcile / void a collection and approve a Day-End move money: the role needs the
    `deliveries` module's `approve` right (Admins / owners are not permission-checked). On
    top of each endpoint's existing check, never instead of it."""
    if not has_permission(db, user, "deliveries", "approve"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=COLLECTIONS_APPROVE_DETAIL)
    return user


def require_collections_view(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> User:
    """Read collections: Deliveries > View/Create or Payments > View/Create (Admins / owners are
    not permission-checked). Row scoping (own records for Delivery Partners) still applies."""
    for module, action in (("deliveries", "view"), ("deliveries", "create"), ("payments", "view"),
                           ("payments", "create")):
        if has_permission(db, user, module, action):
            return user
    if not user.role_id:
        # Legacy account without a role: judge it by its legacy role's default matrix.
        from app.core.permissions import default_role_matrices
        from app.models.enums import STAFF_ROLE_NAME
        try:
            legacy = UserRole(user.role) if user.role else None
        except ValueError:
            legacy = None
        matrix = default_role_matrices().get(STAFF_ROLE_NAME.get(legacy, ""), {})
        if any(matrix.get(m, {}).get(a) for m in ("deliveries", "payments") for a in ("view", "create")):
            return user
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You don't have permission to view collections")


def _lock_collection(db: Session, collection_id: str, org_id: str) -> DeliveryCollection:
    """Row-lock and re-read a collection before its state is checked.

    First step of the payment lock order (collection -> customer -> invoices, id order ->
    number / journal counters; payment_service.lock_for_payment does the middle two). Without
    it two simultaneous reconciles (or voids) of one collection both saw "recorded" and both
    recorded (or voided) its payment. 404 when it is not this firm's."""
    coll = (
        db.query(DeliveryCollection)
        .options(lazyload("*"))
        .filter(DeliveryCollection.id == collection_id)
        .with_for_update(of=DeliveryCollection)
        .populate_existing()
        .first()
    )
    if coll is None or coll.organization_id != org_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Collection not found")
    return coll


def _is_accountant_or_admin(user: User) -> bool:
    if user.system_role == "admin" or user.effective_system_role == "admin":
        return True
    if user.role == UserRole.ACCOUNTANT:
        return True
    if user.role_detail and user.role_detail.name in ("Accountant", "Admin"):
        return True
    if user.role_detail and isinstance(user.role_detail.permissions, dict):
        perms = user.role_detail.permissions
        if perms.get("payments", {}).get("view") or perms.get("invoices", {}).get("view"):
            return True
    return False


def _is_delivery_partner(user: User) -> bool:
    if user.role == UserRole.DELIVERY_PARTNER:
        return True
    if user.role_detail and user.role_detail.name == "Delivery Partner":
        return True
    return False


def _filtered_collections(
    db: Session,
    user: User,
    *,
    status_filter: str | None = None,
    search: str | None = None,
    delivery_partner_id: str | None = None,
    collected_by_user_id: str | None = None,
    customer_id: str | None = None,
    order_id: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    collection_status_filter: str | None = None,
    payment_mode: str | None = None,
):
    """The collections the history (or its summary) covers. A Delivery Partner / own-scope
    user only ever gets their own; the collector filters apply to everyone else."""
    org_id = _org_id(user)
    q = db.query(DeliveryCollection).filter(DeliveryCollection.organization_id == org_id)

    data_scope = getattr(user.role_detail, "data_scope", "all") if user.role_detail else "all"
    if _is_delivery_partner(user) or data_scope == "own":
        if user.system_role != "admin" and user.effective_system_role != "admin":
            q = q.filter(DeliveryCollection.delivery_partner_id == user.id)
    else:
        # The collector is stored as delivery_partner_id; collected_by_user_id is the
        # same thing under the name the payment screens use.
        for collector in {delivery_partner_id, collected_by_user_id} - {None, ""}:
            q = q.filter(DeliveryCollection.delivery_partner_id == collector)

    if customer_id:
        q = q.filter(DeliveryCollection.customer_id == customer_id)
    if order_id:
        q = q.filter(DeliveryCollection.sales_order_id == order_id)

    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="date_from is after date_to")
    if date_from or date_to:
        # Whole days in the firm's business timezone, both ends inclusive.
        # By the business day (payment_date); rows without one fall back to collected_at.
        q = q.filter(collection_dates.day_range_filter(db, org_id, date_from, date_to))

    # These filters used to name columns the model does not have (status,
    # collection_number, payment_reference), so any call with ?status= or ?search=
    # failed with a 500 and the history screen showed nothing.
    stored = _stored_collection_status(status_filter, collection_status_filter)
    if stored:
        q = q.filter(DeliveryCollection.reconciliation_status == stored)

    if payment_mode and payment_mode.strip():
        m = payment_mode.strip().lower()
        mode = func.lower(func.coalesce(DeliveryCollection.payment_mode, "other"))
        if m in _BANK_MODES:            # the same grouping as the summary's bank_transfer_total
            q = q.filter(mode.in_(_BANK_MODES))
        elif m == "other":              # the same set as the summary's other_total
            q = q.filter(mode.notin_(("cash", "upi", "cheque") + _BANK_MODES))
        else:
            q = q.filter(mode == m)

    if search and search.strip():
        # One term over the collection, its customer, order, delivery and collector. Each
        # lookup is an org-scoped IN subquery, so this stays one SQL statement.
        s = f"%{search.strip()}%"
        q = q.filter(
            or_(
                DeliveryCollection.reference.ilike(s),
                DeliveryCollection.notes.ilike(s),
                DeliveryCollection.customer_id.in_(
                    select(Customer.id).where(
                        Customer.organization_id == org_id,
                        or_(Customer.name.ilike(s), Customer.business_name.ilike(s),
                            Customer.primary_contact_person.ilike(s), Customer.customer_id.ilike(s)),
                    )
                ),
                DeliveryCollection.sales_order_id.in_(
                    select(SalesOrder.id).where(SalesOrder.organization_id == org_id, SalesOrder.order_number.ilike(s))
                ),
                DeliveryCollection.delivery_id.in_(
                    select(Delivery.id).where(Delivery.organization_id == org_id,
                                              Delivery.delivery_note_number.ilike(s))
                ),
                DeliveryCollection.delivery_partner_id.in_(
                    select(User.id).where(User.organization_id == org_id, User.name.ilike(s))
                ),
            )
        )
    return q


def _stored_collection_status(status_filter: str | None, collection_status_filter: str | None) -> str | None:
    """?status= and ?collection_status= are the same filter (any of the accepted names). Both
    given and meaning different states -> 400; an unknown name -> 422 (as before)."""
    stored = []
    for value in (status_filter, collection_status_filter):
        if value is None or not value.strip():
            continue
        s = _COLLECTION_STATUS_FILTER.get(value.strip().lower())
        if s is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="status must be one of: awaiting_reconciliation, recorded, reconciled, voided",
            )
        stored.append(s)
    if len(set(stored)) > 1:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"status={status_filter} and collection_status={collection_status_filter} conflict; "
                   "send one of them (they are the same filter)",
        )
    return stored[0] if stored else None


def _collections_out(db: Session, rows: list, cls=DeliveryCollectionOut) -> list:
    """Collection rows with their display names, in a fixed number of queries per page.
    Customer, order, collector and confirmer come from the row's relationships (loaded for
    the whole page at once); deliveries, assigned partners and receipt numbers are one
    batched lookup each. Everything is checked against the row's organization."""
    if not rows:
        return []
    pay_days = collection_dates.payment_dates(db, rows)
    org_id = rows[0].organization_id
    dlv_ids = {r.delivery_id for r in rows if r.delivery_id}
    deliveries = {
        d_id: (number, partner)
        for d_id, number, partner in db.query(Delivery.id, Delivery.delivery_note_number, Delivery.delivery_partner_id)
        .filter(Delivery.organization_id == org_id, Delivery.id.in_(dlv_ids)).all()
    } if dlv_ids else {}
    partner_ids = {p for _, p in deliveries.values() if p}
    names = dict(
        db.query(User.id, User.name).filter(User.organization_id == org_id, User.id.in_(partner_ids)).all()
    ) if partner_ids else {}
    pay_ids = {r.customer_payment_id for r in rows if r.customer_payment_id}
    receipts = dict(
        db.query(CustomerPayment.id, CustomerPayment.receipt_number)
        .filter(CustomerPayment.organization_id == org_id, CustomerPayment.id.in_(pay_ids)).all()
    ) if pay_ids else {}
    fields = cls.model_fields

    def same_org(obj):  # noqa: ANN001, ANN202
        return obj if obj is not None and getattr(obj, "organization_id", None) == org_id else None

    out = []
    for r in rows:
        item = cls.model_validate(r)
        cust = same_org(r.customer)
        if cust is not None:
            card = {**_customer_card(cust), "business_name": cust.business_name}
            for k, v in card.items():
                if k in fields:
                    setattr(item, k, v)
        collector, confirmer, order = same_org(r.delivery_partner), same_org(r.reconciled_by), same_org(r.sales_order)
        item.collected_by_name = collector.name if collector else None
        item.reconciled_by_name = confirmer.name if confirmer else None
        item.order_number = order.order_number if order else None
        number, partner = deliveries.get(r.delivery_id, (None, None))
        item.delivery_number = number
        item.assigned_delivery_partner_id = partner
        item.delivery_partner_name = names.get(partner) if partner else None
        item.receipt_number = receipts.get(r.customer_payment_id)
        item.payment_date = pay_days.get(r.id)
        out.append(item)
    return out


_DATE_DOC = "YYYY-MM-DD, inclusive, in the firm's business timezone (by collected_at)"
_COLLECTION_STATUS_DOC = ("Same filter as `status`: collected | completed | voided (or any name `status` takes). "
                          "Both given and meaning different states -> 400")
_PAYMENT_MODE_DOC = ("cash | upi | cheque | card | bank_transfer (also matches bank / neft / rtgs / imps / "
                     "netbanking / online) | other (anything else, as the summary's other_total); "
                     "any other value matches that stored mode exactly. Case-insensitive")


@router.get("/collections/summary", response_model=CollectionSummaryOut)
def delivery_collections_summary(
    search: str | None = None,
    status_filter: str | None = Query(default=None, alias="status", description="Same values as the history"),
    delivery_partner_id: str | None = None,
    collected_by_user_id: str | None = Query(default=None, description="The collector (same as delivery_partner_id)"),
    customer_id: str | None = None,
    order_id: str | None = None,
    date_from: date | None = Query(default=None, description=_DATE_DOC),
    date_to: date | None = Query(default=None, description=_DATE_DOC),
    collection_status_q: str | None = Query(default=None, alias="collection_status", description=_COLLECTION_STATUS_DOC),
    payment_mode: str | None = Query(default=None, description=_PAYMENT_MODE_DOC),
    user: User = Depends(require_collections_view),
    db: Session = Depends(get_db),
) -> CollectionSummaryOut:
    """Count and amount of the collections GET /deliveries/collections would list with the
    same filters: in total, per status and per payment mode. Two grouped queries."""
    q = _filtered_collections(
        db, user, status_filter=status_filter, search=search, delivery_partner_id=delivery_partner_id,
        collected_by_user_id=collected_by_user_id, customer_id=customer_id, order_id=order_id,
        date_from=date_from, date_to=date_to, collection_status_filter=collection_status_q, payment_mode=payment_mode,
    ).order_by(None)
    amount = func.coalesce(func.sum(DeliveryCollection.amount), 0.0)
    by_status = {key: CollectionTotals(count=0, amount=0.0) for key in COLLECTION_STATUS_BY_RECONCILIATION.values()}
    for stored, count, total in (
        q.with_entities(DeliveryCollection.reconciliation_status, func.count(DeliveryCollection.id), amount)
        .group_by(DeliveryCollection.reconciliation_status)
        .all()
    ):
        by_status[collection_status(stored)] = CollectionTotals(count=count, amount=round(float(total), 2))
    by_mode = {
        (mode or "unknown"): CollectionTotals(count=count, amount=round(float(total), 2))
        for mode, count, total in (
            q.with_entities(DeliveryCollection.payment_mode, func.count(DeliveryCollection.id), amount)
            .group_by(DeliveryCollection.payment_mode)
            .all()
        )
    }
    return CollectionSummaryOut(
        total=CollectionTotals(
            count=sum(t.count for t in by_status.values()),
            amount=round(sum(t.amount for t in by_status.values()), 2),
        ),
        by_status=by_status,
        by_payment_mode=by_mode,
    )


_COLLECTIONS_RESPONSE = Union[
    PaginatedResponse[DeliveryCollectionOut], PendingCollectionsViewOut, CollectedCollectionsViewOut,
]
_BANK_MODES = ("bank_transfer", "bank", "neft", "rtgs", "imps", "netbanking", "net_banking", "online")


def _customer_card(c: Customer | None) -> dict:
    """Name, contact, photo and location of a customer, as both collection views show them."""
    if c is None:
        return {}
    from app.core.files import normalize_file_url
    photo = str(c.profile_image_id).strip() if c.profile_image_id else None
    return {
        "customer_name": c.name, "customer_code": c.customer_id, "contact_name": c.primary_contact_person,
        "phone": c.phone, "profile_photo_url": normalize_file_url(photo) if photo else None,
        "latitude": c.maps_latitude, "longitude": c.maps_longitude,
    }


def _view_page(page: int, page_size: int, total: int) -> tuple[int, int, int]:
    total_pages = max((total + page_size - 1) // page_size, 1)
    return page, page_size, total_pages


def _pending_collections_view(db, user, *, search, customer_id, page, page_size, sort_by, sort_order):  # noqa: ANN001
    """Customers something can still be collected from (To Collect): active, and
    collectable_amount = outstanding - collections awaiting completion > 0. The same
    amount the collect endpoints cap at (409 above it). Same customers GET /customers
    shows this user, filtered in SQL before the count, the summary and the page."""
    if not has_permission(db, user, "customers", "view"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You don't have permission to view customers")
    org_id = _org_id(user)
    # Collected but not completed yet (voided and completed ones do not count: a completed
    # one is already off outstanding_balance).
    in_hand = (
        select(func.coalesce(func.sum(DeliveryCollection.amount), 0.0))
        .where(DeliveryCollection.organization_id == Customer.organization_id,
               DeliveryCollection.customer_id == Customer.id,
               DeliveryCollection.reconciliation_status == "recorded")
        .correlate(Customer)
        .scalar_subquery()
    )
    outstanding = func.coalesce(Customer.outstanding_balance, 0.0)
    collectable = outstanding - in_hand
    q = db.query(Customer).filter(
        Customer.organization_id == org_id,
        Customer.is_active.is_(True),
        collectable > 0.005,
    )
    if not _is_delivery_partner(user):
        q = scoping.owned_by(q, db, user, Customer.assigned_sales_officer_id)
    if customer_id:
        q = q.filter(Customer.id == customer_id)
    if search and search.strip():
        s = f"%{search.strip()}%"
        q = q.filter(or_(Customer.name.ilike(s), Customer.business_name.ilike(s), Customer.phone.ilike(s),
                         Customer.customer_id.ilike(s), Customer.primary_contact_person.ilike(s)))

    agg = q.with_entities(
        Customer.id.label("cid"), outstanding.label("out"), in_hand.label("in_hand"),
    ).order_by(None).subquery()
    count, sum_out, sum_in_hand = db.query(
        func.count(agg.c.cid), func.coalesce(func.sum(agg.c.out), 0.0), func.coalesce(func.sum(agg.c.in_hand), 0.0),
    ).one()
    total = int(count or 0)
    sum_out, sum_in_hand = float(sum_out or 0.0), float(sum_in_hand or 0.0)

    q = apply_sorting(
        q, sort_by=sort_by, sort_order=sort_order,
        sortable_fields={"customer_name": Customer.name, "name": Customer.name,
                         "outstanding_amount": Customer.outstanding_balance,
                         "collectable_amount": collectable,
                         "pending_collection_amount": in_hand,
                         "outstanding_balance": Customer.outstanding_balance},
        default_order=[Customer.name.asc(), Customer.id.asc()],
        secondary_key=Customer.id.asc(),
    )
    q = q.options(lazyload("*"))
    rows = q.add_columns(in_hand).offset((page - 1) * page_size).limit(page_size).all()
    page, page_size, total_pages = _view_page(page, page_size, total)
    items = []
    for c, held in rows:
        out_amt = round(float(c.outstanding_balance or 0.0), 2)
        held = round(float(held or 0.0), 2)
        items.append(PendingCollectionCustomerOut(
            id=c.id, customer_id=c.id, business_name=c.business_name, **_customer_card(c),
            address=getattr(c, "delivery_address", None) or getattr(c, "billing_address", None),
            outstanding_amount=out_amt, pending_collection_amount=held,
            collectable_amount=round(max(out_amt - held, 0.0), 2),
        ))
    return PendingCollectionsViewOut(
        items=items, total=total, page=page, page_size=page_size, total_pages=total_pages,
        pagination=CollectionsPagination(page=page, page_size=page_size, total=total, total_pages=total_pages),
        summary=CollectionsViewSummary(
            total_pending=total, total_pending_amount=round(sum_out - sum_in_hand, 2),
            total_outstanding_amount=round(sum_out, 2), total_pending_collection_amount=round(sum_in_hand, 2),
        ),
    )


def _collected_collections_view(db, user, q, *, page, page_size, sort_by, sort_order):  # noqa: ANN001
    """Collection records awaiting approval or approved (voided are in neither view). `q` is
    the history query with the caller's scope and filters already applied."""
    q = q.filter(DeliveryCollection.reconciliation_status.in_(("recorded", "reconciled")))
    ids = q.with_entities(DeliveryCollection.id).order_by(None).subquery()
    mode = func.lower(func.coalesce(DeliveryCollection.payment_mode, "other"))
    agg = (
        db.query(DeliveryCollection.reconciliation_status, mode, func.count(DeliveryCollection.id),
                 func.coalesce(func.sum(DeliveryCollection.amount), 0.0))
        .filter(DeliveryCollection.id.in_(select(ids.c.id)))
        .group_by(DeliveryCollection.reconciliation_status, mode)
        .all()
    )
    summary = CollectionsViewSummary()
    by_mode: dict[str, float] = {}
    for stored, m, n, amt in agg:
        amt = float(amt or 0.0)
        summary.collected_count += int(n)
        summary.total_collected += amt
        if stored == "recorded":
            summary.awaiting_approval_count += int(n)
            summary.awaiting_approval_amount += amt
        else:
            summary.approved_count += int(n)
            summary.approved_amount += amt
        by_mode[m] = by_mode.get(m, 0.0) + amt
        if m == "cash":
            summary.cash_total += amt
        elif m in _BANK_MODES:
            summary.bank_transfer_total += amt
        elif m == "upi":
            summary.upi_total += amt
        elif m == "cheque":
            summary.cheque_total += amt
        else:
            summary.other_total += amt
    for f in ("total_collected", "awaiting_approval_amount", "approved_amount", "cash_total",
              "bank_transfer_total", "upi_total", "cheque_total", "other_total"):
        setattr(summary, f, round(getattr(summary, f), 2))
    # Same numbers under the app's status names (collected = not completed yet).
    summary.collected_amount, summary.collected_only_count = summary.awaiting_approval_amount, summary.awaiting_approval_count
    summary.completed_amount, summary.completed_count = summary.approved_amount, summary.approved_count
    summary.by_payment_mode = {k: round(v, 2) for k, v in sorted(by_mode.items())}
    total = summary.collected_count

    q = apply_sorting(
        q, sort_by=sort_by, sort_order=sort_order,
        sortable_fields=DELIVERY_COLLECTION_SORTABLE_FIELDS,
        default_order=[DeliveryCollection.collected_at.desc().nullslast(), DeliveryCollection.id.desc()],
        secondary_key=DeliveryCollection.id.desc(),
    )
    rows = q.offset((page - 1) * page_size).limit(page_size).all()
    items = _collections_out(db, rows, CollectedCollectionOut)
    page, page_size, total_pages = _view_page(page, page_size, total)
    return CollectedCollectionsViewOut(
        items=items, total=total, page=page, page_size=page_size, total_pages=total_pages,
        pagination=CollectionsPagination(page=page, page_size=page_size, total=total, total_pages=total_pages),
        summary=summary,
    )


@router.get("/collections", response_model=_COLLECTIONS_RESPONSE)
@router.get("/collections/all", response_model=_COLLECTIONS_RESPONSE)
def list_delivery_collections(
    view: str | None = Query(
        default=None,
        description="Omit for the full history (unchanged). `pending` (To Collect): one row per customer "
                    "with collectable_amount = outstanding - collections not completed yet > 0; `id` is the "
                    "customer id. `collected`: collection records with collection_status `collected` (money "
                    "taken, not completed yet) or `completed` (reconciled); voided in neither. Each view has "
                    "its own total / pagination / summary, computed after its filters.",
    ),
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1, le=100),
    sort_by: str | None = Query(default=None, description="Field to sort by"),
    sort_order: str = Query(default="asc", description="Sort order: asc or desc"),
    search: str | None = None,
    status: str | None = Query(
        default=None,
        description="collected | completed | voided, or the older names awaiting_reconciliation | "
                    "pending_approval | approved and the stored reconciliation_status recorded | reconciled",
    ),
    collection_status_q: str | None = Query(default=None, alias="collection_status", description=_COLLECTION_STATUS_DOC),
    payment_mode: str | None = Query(default=None, description=_PAYMENT_MODE_DOC),
    delivery_partner_id: str | None = None,
    collected_by_user_id: str | None = Query(default=None, description="The collector (same as delivery_partner_id)"),
    customer_id: str | None = Query(default=None, description="Only this customer's collections"),
    order_id: str | None = Query(default=None, description="Only collections linked to this sales order"),
    date_from: date | None = Query(default=None, description=_DATE_DOC),
    date_to: date | None = Query(default=None, description=_DATE_DOC),
    user: User = Depends(require_collections_view),
    db: Session = Depends(get_db),
) -> PaginatedResponse[DeliveryCollectionOut]:
    """List organization delivery collections.

    Delivery Partners only see collections where they are the assigned partner.
    Accountants and Admins see all collections for their organization.
    """
    if view is not None:
        v = view.strip().lower()
        if v not in ("pending", "collected"):
            # (`status` here is the query parameter, not fastapi.status)
            raise HTTPException(status_code=400, detail="view must be one of: pending, collected")
        if v == "pending":
            return _pending_collections_view(db, user, search=search, customer_id=customer_id, page=page,
                                             page_size=page_size, sort_by=sort_by, sort_order=sort_order)
        q = _filtered_collections(
            db, user, status_filter=status, search=search, delivery_partner_id=delivery_partner_id,
            collected_by_user_id=collected_by_user_id, customer_id=customer_id, order_id=order_id,
            date_from=date_from, date_to=date_to, collection_status_filter=collection_status_q,
            payment_mode=payment_mode,
        )
        return _collected_collections_view(db, user, q, page=page, page_size=page_size,
                                           sort_by=sort_by, sort_order=sort_order)
    q = _filtered_collections(
        db, user, status_filter=status, search=search, delivery_partner_id=delivery_partner_id,
        collected_by_user_id=collected_by_user_id, customer_id=customer_id, order_id=order_id,
        date_from=date_from, date_to=date_to, collection_status_filter=collection_status_q,
        payment_mode=payment_mode,
    )

    q = apply_sorting(
        q,
        sort_by=sort_by,
        sort_order=sort_order,
        sortable_fields=DELIVERY_COLLECTION_SORTABLE_FIELDS,
        default_order=[DeliveryCollection.collected_at.desc().nullslast(), DeliveryCollection.id.desc()],
        secondary_key=DeliveryCollection.id.desc(),
    )
    result = paginate(q, page=page, page_size=page_size)
    result.items = _collections_out(db, list(result.items))
    return result


@router.get("/collections/{collection_id}", response_model=DeliveryCollectionOut)
def get_delivery_collection(
    collection_id: str,
    user: User = Depends(require_collections_view),
    db: Session = Depends(get_db),
) -> DeliveryCollection:
    """Retrieve a single delivery collection by ID."""
    org_id = _org_id(user)
    coll = db.get(DeliveryCollection, collection_id)
    if coll is None or coll.organization_id != org_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Collection not found")

    data_scope = getattr(user.role_detail, "data_scope", "all") if user.role_detail else "all"
    if _is_delivery_partner(user) or data_scope == "own":
        if user.system_role != "admin" and user.effective_system_role != "admin":
            if coll.delivery_partner_id != user.id:
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access to this collection is unauthorized")

    return _collections_out(db, [coll])[0]   # with names, confirmer and receipt number (detail drawer)


COLLECTION_CREATE_DETAIL = (
    "You don't have permission to record a collection: this needs Deliveries > Create or "
    "Payments > Create on your role."
)


def require_collection_create(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> User:
    """Record a field collection: Deliveries > Create (delivery partners) or Payments > Create
    (accounts staff). Admins / owners are not permission-checked."""
    if has_permission(db, user, "deliveries", "create") or has_permission(db, user, "payments", "create"):
        return user
    if not user.role_id:
        # A legacy account with no role assigned (predates roles): judge it by the default
        # matrix of its legacy role (Delivery Partner / Accountant / ...), so it keeps working.
        from app.core.permissions import default_role_matrices
        from app.models.enums import STAFF_ROLE_NAME
        try:
            legacy = UserRole(user.role) if user.role else None
        except ValueError:
            legacy = None
        matrix = default_role_matrices().get(STAFF_ROLE_NAME.get(legacy, ""), {})
        if matrix.get("deliveries", {}).get("create") or matrix.get("payments", {}).get("create"):
            return user
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=COLLECTION_CREATE_DETAIL)


@customer_payments_router.post("/collections", response_model=DeliveryCollectionOut, status_code=status.HTTP_201_CREATED)
@router.post("/collections/customer", response_model=DeliveryCollectionOut, status_code=status.HTTP_201_CREATED)
def record_general_customer_collection(
    payload: CustomerCollectionCreate,
    user: User = Depends(require_collection_create),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> DeliveryCollection:
    """Delivery Partner / Staff records a general field payment collection for a customer.

    Can be recorded with or without a delivery_id (delivery_id = None for general field collection).
    Does NOT require delivery assignment when delivery_id is None.
    Validates tenant isolation, customer existence, amounts, and invoice allocations.
    Collector identity is derived from authenticated user context.
    """
    org_id = _org_id(user)

    # 1. Validate customer existence and organization. The row is locked for the rest of
    # the transaction so two partners submitting at the same moment are checked one
    # after the other against the same pending total (no double collection).
    customer = (
        db.query(Customer)
        .filter(Customer.id == payload.customer_id, Customer.organization_id == org_id)
        .with_for_update(of=Customer)  # the customer row only, not its eager-loaded joins
        .populate_existing()
        .first()
    )
    if customer is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Customer not found in your organization")

    # 2. Validate amount > 0
    if payload.amount <= 0:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Collection amount must be greater than zero")

    # 3. Validate collection amount <= customer's total outstanding balance
    cust_outstanding = round(float(customer.outstanding_balance or 0.0), 2)
    if round(payload.amount, 2) > round(cust_outstanding + 0.01, 2):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Collection amount ({payload.amount:.2f}) exceeds customer's total outstanding balance ({cust_outstanding:.2f})",
        )

    # 3b. ...less what has already been collected and is waiting for admin approval.
    # outstanding_balance only drops on approval, so without this a second partner
    # (or the same one, twice) could collect the same balance again.
    pending = round(float(
        db.query(func.coalesce(func.sum(DeliveryCollection.amount), 0.0))
        .filter(
            DeliveryCollection.organization_id == org_id,
            DeliveryCollection.customer_id == customer.id,
            DeliveryCollection.reconciliation_status == "recorded",
        )
        .scalar() or 0.0
    ), 2)
    collectable = round(max(cust_outstanding - pending, 0.0), 2)
    if pending and round(payload.amount, 2) > round(collectable + 0.01, 2):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"{pending:.2f} has already been collected from this customer and is awaiting admin "
                f"approval; at most {collectable:.2f} can still be collected"
            ),
        )

    # 4. Validate delivery_id if provided
    delivery = None
    if payload.delivery_id:
        delivery = db.get(Delivery, payload.delivery_id)
        if delivery is None or delivery.organization_id != org_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Delivery not found in your organization")

        data_scope = getattr(user.role_detail, "data_scope", "all") if user.role_detail else "all"
        if _is_delivery_partner(user) or data_scope == "own":
            if user.system_role != "admin" and user.effective_system_role != "admin":
                if delivery.delivery_partner_id != user.id:
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN,
                        detail="Cannot record collection for a delivery not assigned to you",
                    )

    # 4b. Validate order_id if provided. It used to be dropped silently (the schema had no
    # such field), so a collection made against an order was saved with no order link.
    sales_order_id = delivery.sales_order_id if delivery else None
    if payload.order_id:
        order = db.get(SalesOrder, payload.order_id)
        if order is None or order.organization_id != org_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Order not found in your organization")
        if order.customer_id != customer.id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Order {order.order_number} does not belong to customer {customer.name}",
            )
        if sales_order_id and sales_order_id != order.id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="order_id does not match the order of the given delivery_id",
            )
        sales_order_id = order.id

    # 5. Validate allocations if provided
    allocations_to_create = []
    if payload.allocations:
        alloc_sum = 0.0
        for alloc in payload.allocations:
            if alloc.amount <= 0:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Allocation amount must be greater than zero")

            invoice = db.get(Invoice, alloc.invoice_id)
            if invoice is None or invoice.organization_id != org_id:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Invoice {alloc.invoice_id} not found in your organization")
            if invoice.customer_id != customer.id:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Invoice {invoice.invoice_number} does not belong to customer {customer.name}",
                )
            if invoice.is_credit_note:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Credit note {invoice.invoice_number} cannot receive payment allocations")
            if invoice.status == "cancelled":
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Cancelled invoice {invoice.invoice_number} cannot receive payment allocations")

            inv_due = payment_service.outstanding(invoice)
            if inv_due <= 0:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Invoice {invoice.invoice_number} is already fully paid")
            if round(alloc.amount, 2) > round(inv_due + 0.01, 2):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Allocation amount ({alloc.amount:.2f}) exceeds outstanding balance ({inv_due:.2f}) for invoice {invoice.invoice_number}",
                )

            alloc_sum += alloc.amount
            allocations_to_create.append((invoice, alloc.amount))

        if round(alloc_sum, 2) > round(payload.amount + 0.01, 2):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Sum of allocations ({alloc_sum:.2f}) exceeds collection amount ({payload.amount:.2f})",
            )
    else:
        # Automatic FIFO allocation across open customer invoices
        open_invoices = (
            db.query(Invoice)
            .filter(
                Invoice.organization_id == org_id,
                Invoice.customer_id == customer.id,
                Invoice.status != "cancelled",
                Invoice.is_credit_note.is_(False),
                Invoice.status != "paid",
            )
            .order_by(Invoice.invoice_date.asc(), Invoice.created_at.asc())
            .all()
        )
        remaining = round(payload.amount, 2)
        for inv in open_invoices:
            inv_due = round(payment_service.outstanding(inv), 2)
            if inv_due <= 0:
                continue
            alloc_amt = min(remaining, inv_due)
            allocations_to_create.append((inv, alloc_amt))
            remaining = round(remaining - alloc_amt, 2)
            if remaining <= 0:
                break

    mode = payload.payment_mode or payload.payment_method or "cash"

    # collected_at = when it is recorded (now); payment_date = the business day picked.
    collected_at, pay_day = collection_dates.stamp(db, org_id, payload.payment_date)

    # A day whose Day-End is submitted / approved takes no new collections.
    day_end_service.require_day_open(db, org_id, user.id, collection_dates.day_moment(db, org_id, pay_day))

    coll = DeliveryCollection(
        organization_id=org_id,
        delivery_id=delivery.id if delivery else None,
        sales_order_id=sales_order_id,
        customer_id=customer.id,
        delivery_partner_id=user.id,
        amount=round(payload.amount, 2),
        payment_mode=mode,
        reference=payload.reference,
        notes=payload.notes,
        payment_proof_url=payload.payment_proof_url,
        reconciliation_status="recorded",
        collected_at=collected_at,
    )
    collection_dates.apply(db, coll, pay_day)
    db.add(coll)
    db.flush()

    for inv, a_amt in allocations_to_create:
        coll_alloc = DeliveryCollectionAllocation(
            organization_id=org_id,
            delivery_collection_id=coll.id,
            invoice_id=inv.id,
            amount=round(a_amt, 2),
        )
        db.add(coll_alloc)

    db.commit()
    db.refresh(coll)
    return _collections_out(db, [coll])[0]


@router.post("/collections/{collection_id}/reconcile", response_model=DeliveryCollectionOut)
@router.patch("/collections/{collection_id}/reconcile", response_model=DeliveryCollectionOut)
def reconcile_delivery_collection(
    collection_id: str,
    user: User = Depends(require_collections_approve),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> DeliveryCollection:
    """Accountant / Admin action to reconcile a recorded collection.

    Transitions reconciliation_status: recorded -> reconciled.
    """
    org_id = _org_id(user)
    if not _is_accountant_or_admin(user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only Accountants or Admins can reconcile delivery collections",
        )

    coll = _lock_collection(db, collection_id, org_id)

    if coll.reconciliation_status != "recorded":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot reconcile collection in state '{coll.reconciliation_status}'. Only 'recorded' collections can be reconciled.",
        )

    # Accounting handoff: create exactly ONE CustomerPayment using existing payment_service
    if not coll.customer_payment_id:
        customer = db.get(Customer, coll.customer_id) if coll.customer_id else None

        alloc_list = [
            {"invoice_id": a.invoice_id, "amount": a.amount}
            for a in coll.allocations
        ] if coll.allocations else None

        invoice = None
        if not alloc_list and coll.sales_order_id:
            invoice = (
                db.query(Invoice)
                .filter(
                    Invoice.order_id == coll.sales_order_id,
                    Invoice.organization_id == org_id,
                )
                .first()
            )

        # Lock + re-read the customer and the candidate invoices (same order as record()) so
        # the choice below is made on committed figures, not on a copy read before the lock.
        payment_service.lock_for_payment(
            db, customer.id if customer is not None else None,
            [invoice.id if invoice is not None else None] + [a["invoice_id"] for a in (alloc_list or [])],
        )
        try:
            inv_to_use = invoice if (invoice and payment_service.outstanding(invoice) >= coll.amount) else None
            payment = payment_service.record(
                db,
                org_id,
                customer=customer,
                invoice=inv_to_use,
                amount=coll.amount,
                payment_mode=coll.payment_mode,
                reference=coll.reference,
                note=f"Reconciled Delivery Collection {coll.id}" + (f" ({coll.notes})" if coll.notes else ""),
                order_id=coll.sales_order_id,
                received_on=collection_dates.moment_of(db, coll),
                allocations=alloc_list,
                collected_by_user_id=coll.delivery_partner_id,
                payment_proof_url=coll.payment_proof_url,
            )
            coll.customer_payment_id = payment.id
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(exc),
            )

    coll.reconciliation_status = "reconciled"
    coll.reconciled_at = datetime.now(timezone.utc)
    coll.reconciled_by_id = user.id

    db.commit()
    db.refresh(coll)
    return _collections_out(db, [coll])[0]


@router.post("/collections/{collection_id}/void", response_model=DeliveryCollectionOut)
@router.patch("/collections/{collection_id}/void", response_model=DeliveryCollectionOut)
def void_delivery_collection(
    collection_id: str,
    user: User = Depends(require_collections_approve),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> DeliveryCollection:
    """Accountant / Admin action to void a recorded collection.

    Transitions reconciliation_status: recorded -> voided or reconciled -> voided.
    """
    org_id = _org_id(user)
    if not _is_accountant_or_admin(user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only Accountants or Admins can void delivery collections",
        )

    coll = _lock_collection(db, collection_id, org_id)

    if coll.reconciliation_status not in ("recorded", "reconciled"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot void collection in state '{coll.reconciliation_status}'. Only 'recorded' or 'reconciled' collections can be voided.",
        )
    # Part of a submitted / approved Day-End: the reviewed figures must stay true.
    day_end_service.require_day_open(
        db, org_id, coll.delivery_partner_id, collection_dates.day_moment(db, org_id, collection_dates.day_of(db, coll))
    )

    if coll.customer_payment_id:
        payment = db.get(CustomerPayment, coll.customer_payment_id)
        if payment:
            payment_service.void(db, payment, voided_by_id=user.id, source="delivery_collection")
            coll.customer_payment_id = None

    coll.reconciliation_status = "voided"

    db.commit()
    db.refresh(coll)
    return _collections_out(db, [coll])[0]


@router.post("/{delivery_id}/collections", response_model=DeliveryCollectionOut, status_code=status.HTTP_201_CREATED)
def record_delivery_collection(
    delivery_id: str,
    payload: DeliveryCollectionCreate,
    user: User = Depends(require_collection_create),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> DeliveryCollection:
    """Delivery Partner / Staff records a payment collection for a Delivery.

    Validates that the delivery is assigned to the calling delivery partner.
    Backend derives sensitive relationship fields (customer_id, delivery_partner_id, order_id)
    from authenticated context and Delivery state.
    """
    org_id = _org_id(user)
    delivery = db.get(Delivery, delivery_id)
    if delivery is None or delivery.organization_id != org_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Delivery not found")

    # Delivery Partner assignment check & tenant/ownership protection
    data_scope = getattr(user.role_detail, "data_scope", "all") if user.role_detail else "all"
    if _is_delivery_partner(user) or data_scope == "own":
        if user.system_role != "admin" and user.effective_system_role != "admin":
            if delivery.delivery_partner_id != user.id:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Cannot record collection for a delivery not assigned to you",
                )

    # Lock the customer row for the rest of the transaction, as POST /collections/customer
    # does: two collections for the same customer (from either endpoint) are then checked
    # one after the other against the same pending total.
    customer = None
    if delivery.customer_id:
        customer = (
            db.query(Customer)
            .filter(Customer.id == delivery.customer_id, Customer.organization_id == org_id)
            .with_for_update(of=Customer)
            .populate_existing()
            .first()
        )

    # Validate collection amount does not exceed remaining outstanding balance on linked order/invoice
    if delivery.sales_order_id:
        so = db.get(SalesOrder, delivery.sales_order_id)
        if so:
            inv = (
                db.query(Invoice)
                .filter(
                    Invoice.order_id == so.id,
                    Invoice.organization_id == org_id,
                    Invoice.is_credit_note.is_(False),
                )
                .order_by(Invoice.created_at.desc())
                .first()
            )
            if inv:
                remaining = payment_service.outstanding(inv)
            else:
                remaining = round(float(so.total or 0.0), 2)

            unreconciled = (
                db.query(func.sum(DeliveryCollection.amount))
                .filter(
                    DeliveryCollection.organization_id == org_id,
                    DeliveryCollection.sales_order_id == so.id,
                    DeliveryCollection.reconciliation_status == "recorded",
                )
                .scalar()
                or 0.0
            )
            effective_remaining = round(max(remaining - float(unreconciled), 0.0), 2)

            if round(payload.amount, 2) > round(effective_remaining + 0.01, 2):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Collection amount exceeds remaining balance",
                )

            # Invoiced order: its amount is in the customer's outstanding_balance, so also
            # count collections awaiting approval from the customer's other orders and the
            # general customer collections -- the same block as POST /collections/customer.
            # (An order not invoiced yet is not in outstanding_balance; the check above covers it.)
            if inv and customer is not None:
                pending = round(float(
                    db.query(func.coalesce(func.sum(DeliveryCollection.amount), 0.0))
                    .filter(
                        DeliveryCollection.organization_id == org_id,
                        DeliveryCollection.customer_id == customer.id,
                        DeliveryCollection.reconciliation_status == "recorded",
                    )
                    .scalar() or 0.0
                ), 2)
                collectable = round(max(float(customer.outstanding_balance or 0.0) - pending, 0.0), 2)
                if pending and round(payload.amount, 2) > round(collectable + 0.01, 2):
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail=(
                            f"{pending:.2f} has already been collected from this customer and is awaiting "
                            f"admin approval; at most {collectable:.2f} can still be collected"
                        ),
                    )

    # A day whose Day-End is submitted / approved takes no new collections.
    collected_at, pay_day = collection_dates.stamp(db, org_id, payload.payment_date)
    # The collector (and so the Day-End it belongs to) is whoever records it, not the partner
    # the delivery is assigned to; the assignment stays on the delivery.
    day_end_service.require_day_open(db, org_id, user.id, collection_dates.day_moment(db, org_id, pay_day))

    # Derive audit links directly from Delivery & User (never trust client)
    coll = DeliveryCollection(
        organization_id=org_id,
        delivery_id=delivery.id,
        sales_order_id=delivery.sales_order_id,
        customer_id=delivery.customer_id,
        delivery_partner_id=user.id,
        amount=payload.amount,
        payment_mode=payload.payment_mode,
        reference=payload.reference,
        notes=payload.notes,
        payment_proof_url=payload.payment_proof_url,
        reconciliation_status="recorded",
        collected_at=collected_at,
    )
    collection_dates.apply(db, coll, pay_day)

    db.add(coll)
    db.commit()
    db.refresh(coll)
    return _collections_out(db, [coll])[0]


@router.get("/{delivery_id}/collections", response_model=list[DeliveryCollectionOut])
def get_collections_for_delivery(
    delivery_id: str,
    user: User = Depends(require_collections_view),
    db: Session = Depends(get_db),
) -> list[DeliveryCollection]:
    """Retrieve all collections recorded against a specific delivery."""
    org_id = _org_id(user)
    delivery = db.get(Delivery, delivery_id)
    if delivery is None or delivery.organization_id != org_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Delivery not found")

    data_scope = getattr(user.role_detail, "data_scope", "all") if user.role_detail else "all"
    if _is_delivery_partner(user) or data_scope == "own":
        if user.system_role != "admin" and user.effective_system_role != "admin":
            if delivery.delivery_partner_id != user.id:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Access to this delivery is unauthorized",
                )

    return db.query(DeliveryCollection).filter(
        DeliveryCollection.organization_id == org_id,
        DeliveryCollection.delivery_id == delivery.id,
    ).order_by(DeliveryCollection.collected_at.desc()).all()

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
        "status": workflow.public_order_status(order.status),
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
    user: User = Depends(_edit),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> dict:
    """Update delivery outcome. Syncs with delivery partner's vehicle stock if source is vehicle.

    Legacy route, kept for backward compatibility — it serves a distinct, already
    simplified flow (field/vehicle-stock sales completing a delivery in one step,
    without the plan -> accept -> pick -> ready -> load -> dispatch -> confirm
    sequence a warehouse delivery goes through). New integrations should use the
    Delivery-id based endpoints above, which enforce the full transition rules.
    This route now requires `deliveries:edit` (previously any authenticated user
    could call it) — organization/ownership scoping was already enforced via
    `_owned_order`.
    """
    org_id = _org_id(user)
    order = _owned_order(db, id, user)

    # Deliver outcome mapping
    # choices: Delivered | Partial | Failed | Rescheduled
    previous_order_status = order.status
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

    notification_service.notify_order_status_changed(db, order, previous_order_status, actor_id=user.id)
    db.commit()
    db.refresh(order)
    return {
        "status": "success",
        "order_status": workflow.public_order_status(order.status),
        "fulfilment_status": order.fulfilment_status,
        "reject_reason": order.reject_reason,
    }


def _owned_delivery_or_from_order(db: Session, id: str, user: User) -> tuple[Delivery, SalesOrder | None]:
    org_id = _org_id(user)
    # 1. Direct Delivery lookup
    delivery = (
        db.query(Delivery)
        .filter(
            Delivery.organization_id == org_id,
            or_(Delivery.id == id, Delivery.delivery_note_number == id),
        )
        .first()
    )
    if delivery is not None:
        if not scoping.owns_record(db, user, delivery, "delivery_partner_id", team_attributes=()):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="This delivery is not assigned to you")
        return delivery, delivery.sales_order

    # 2. Backward compatibility fallback: check if id is a SalesOrder
    order = (
        db.query(SalesOrder)
        .filter(
            SalesOrder.organization_id == org_id,
            or_(SalesOrder.id == id, SalesOrder.order_number == id),
        )
        .first()
    )
    if order is not None:
        if not scoping.owns_record(db, user, order, "assigned_delivery_partner_id", team_attributes=()):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="This delivery is not assigned to you")
        delivery = (
            db.query(Delivery)
            .filter(
                Delivery.organization_id == org_id,
                Delivery.sales_order_id == order.id,
                Delivery.status == "delivered",
            )
            .order_by(Delivery.created_at.desc())
            .first()
        )
        if delivery is None:
            delivery = (
                db.query(Delivery)
                .filter(
                    Delivery.organization_id == org_id,
                    Delivery.sales_order_id == order.id,
                )
                .order_by(Delivery.created_at.desc())
                .first()
            )
        if delivery is not None:
            return delivery, order

    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Delivery not found")


@router.get("/{id}/receipt")
def get_delivery_receipt(
    id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Response:
    """Download delivery receipt PDF based on completed Delivery and delivered_quantity."""
    delivery, order = _owned_delivery_or_from_order(db, id, user)
    if delivery.status != "delivered":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Delivery receipt is only available for completed deliveries (status: delivered)",
        )

    customer = delivery.customer or (order.customer if order else None)
    partner = db.get(User, delivery.delivery_partner_id) if delivery.delivery_partner_id else None
    vehicle = db.get(Vehicle, delivery.vehicle_id) if delivery.vehicle_id else None

    from app.core.pdf_docs import delivery_receipt_pdf  # lazy: fpdf is only loaded when a PDF is actually built

    pdf_bytes = delivery_receipt_pdf(
        user.organization,
        customer,
        delivery,
        order=order,
        partner=partner,
        vehicle=vehicle,
    )
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": content_disposition(
            document_filename(f"delivery-receipt-{delivery.delivery_note_number}.pdf"), "attachment")},
    )



