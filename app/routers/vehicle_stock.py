from datetime import date, datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core import scoping
from app.core.database import get_db
from app.core.deps import require_entitlement, require_permission, require_unlocked_org
from app.core.sorting import apply_sorting
from app.models import (
    Delivery,
    Product,
    ProductVariant,
    StockMovement,
    User,
    Vehicle,
    VehicleLoading,
    VehicleLoadingItem,
    VehicleReconciliationItem,
    VehicleStockReconciliation,
)
from app.core.pagination import paginate
from app.schemas.pagination import PaginatedResponse
from app.services import delivery_service, stock_service
from app.schemas.vehicle_stock import (
    DeliveryLoadOut,
    EndOfDayBody,
    ExtraLoadBody,
    LoadedLineOut,
    ReconcileBody,
    VehicleLoadingCreate,
    VehicleLoadSummaryOut,
    VehicleLoadingOut,
    VehicleReconciliationOut,
)

router = APIRouter(
    prefix="/vehicle-stock",
    tags=["vehicle_stock"],
    dependencies=[Depends(require_entitlement("sales.vehicle_stock"))],
)

_view = require_permission("vehicle_stock", "view")
_create = require_permission("vehicle_stock", "create")
_edit = require_permission("vehicle_stock", "edit")

VEHICLE_LOADING_SORTABLE_FIELDS = {
    "date": VehicleLoading.date,
    "session_date": VehicleLoading.date,
    "status": VehicleLoading.status,
    "vehicle": select(Vehicle.vehicle_number).where(Vehicle.id == VehicleLoading.vehicle_id).scalar_subquery(),
    "vehicle_number": select(Vehicle.vehicle_number).where(Vehicle.id == VehicleLoading.vehicle_id).scalar_subquery(),
    "delivery_partner": select(User.name).where(User.id == VehicleLoading.delivery_partner_id).scalar_subquery(),
    "created_at": VehicleLoading.created_at,
    "updated_at": VehicleLoading.updated_at,
    "updated_time": VehicleLoading.updated_at,
}


def _org_id(user: User) -> str:
    if not user.organization_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No organization on this account")
    return user.organization_id


def _owned(db: Session, id: str, org_id: str, user: User) -> VehicleLoading:
    """A loading session by its own id. An "own"-scope user (Delivery Partner,
    or any custom role with data_scope=="own") may only reach a session that
    is actually theirs — the same dynamic scope check every other module
    uses (see app.core.scoping), not a hardcoded role-name check. Knowing
    another partner's session UUID does not grant access: out of scope reads
    as not found, same as the org check above. "all"-scope (Admin, or a
    custom org-wide role) is unaffected.
    """
    loading = db.get(VehicleLoading, id)
    if loading is None or loading.organization_id != org_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Vehicle loading session not found")
    # team_attributes=(): Vehicle Stock is explicitly excluded from Team Scope.
    if not scoping.owns_record(db, user, loading, "delivery_partner_id", team_attributes=()):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Vehicle loading session not found")
    return loading


@router.post("/loading", status_code=status.HTTP_201_CREATED,
             response_model=DeliveryLoadOut | VehicleLoadingOut)
def load_vehicle(
    payload: VehicleLoadingCreate,
    user: User = Depends(_create),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> object:
    """Load a delivery vehicle.

    **Against a planned delivery** — send `delivery_id`, and per item a
    `delivery_item_id` with its `loaded_quantity`. One transaction moves all four
    figures:

        warehouse on hand ↓   reservation consumed ↓   vehicle stock ↑   loaded_quantity ↑

    Safe to call twice: a line can only ever be loaded up to its planned quantity, so
    a repeat call loads whatever is left and then nothing. The same units are never
    deducted twice. Returns the loading, the delivery's new status, and per line the
    resulting warehouse and vehicle figures.

    **Without a delivery** — send `delivery_partner_id` and plain product quantities
    to stock a van for ad-hoc field sales, as before.
    """
    org_id = _org_id(user)

    if payload.vehicle_id:
        v_check = db.get(Vehicle, payload.vehicle_id)
        if v_check is None or v_check.organization_id != org_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="vehicle_id is not a vehicle in your firm",
            )
        if not v_check.is_active or v_check.status != "active":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Vehicle '{v_check.vehicle_number}' is {v_check.status} and cannot be used for new vehicle loading",
            )

    if payload.delivery_id:
        delivery = db.get(Delivery, payload.delivery_id)
        if delivery is None or delivery.organization_id != org_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="delivery_id is not a delivery in your firm",
            )
        # An "own"-scope user may only load stock onto a Delivery actually
        # assigned to them — same dynamic scope check as everywhere else in
        # this module. Without this, any Delivery Partner could load (and
        # thereby deduct warehouse stock for) any delivery in the org just by
        # knowing its id.
        if not scoping.owns_record(db, user, delivery, "delivery_partner_id", team_attributes=()):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="delivery_id is not a delivery in your firm",
            )
        # Enforce workflow: deliveries must be marked `ready` before initial loading.
        if delivery.status not in ("ready", "loaded"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    "Delivery must be marked ready before loading. "
                    f"Current status: {delivery.status}. Call POST /deliveries/{payload.delivery_id}/ready first."
                ),
            )
        if payload.delivery_partner_id and not delivery.delivery_partner_id:
            delivery.delivery_partner_id = payload.delivery_partner_id
        if payload.vehicle_id:
            delivery.vehicle_id = payload.vehicle_id
        wanted = (
            [
                {"delivery_item_id": it.delivery_item_id, "loaded_quantity": it.loaded_qty}
                for it in payload.items
                if it.delivery_item_id
            ]
            or None
        )
        loading, results = delivery_service.load(db, user, delivery, wanted=wanted)
        db.commit()
        db.refresh(delivery)
        return DeliveryLoadOut(
            loading_id=loading.id,
            delivery_id=delivery.id,
            status=delivery.status,
            items=[LoadedLineOut(**row) for row in results],
        )

    partner = db.get(User, payload.delivery_partner_id)
    if partner is None or partner.organization_id != org_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="delivery_partner_id is not a user in your firm")
    # An "own"-scope user may only start a loading session for themselves —
    # not stock a van "for" a colleague.
    if scoping.scope_to_own(db, user) and partner.id != user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You may only start a vehicle loading session for yourself",
        )

    # Check for active session
    active = (
        db.query(VehicleLoading)
        .filter(
            VehicleLoading.delivery_partner_id == partner.id,
            VehicleLoading.status == "active",
        )
        .first()
    )
    if active:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Delivery partner already has an active vehicle loading session.",
        )

    warehouse = stock_service.owned_warehouse(db, getattr(payload, "warehouse_id", None), org_id, require_active=True)
    if warehouse is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid warehouse")

    loading = VehicleLoading(
        organization_id=org_id,
        delivery_partner_id=partner.id,
        vehicle_id=payload.vehicle_id,
        warehouse_id=warehouse.id,
        date=payload.date or datetime.now(timezone.utc),
        status="active",
    )

    for it in payload.items:
        product = db.get(Product, it.product_id)
        if product is None or product.organization_id != org_id:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="An item's product is not in your firm")
        
        variant = None
        if it.variant_id:
            variant = db.get(ProductVariant, it.variant_id)
            if variant is None or variant.product_id != product.id:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="An item's variant is invalid")

        # Check available stock in warehouse
        current_avail = stock_service.available(db, warehouse.id, product.id, it.variant_id)
        if current_avail < it.loaded_qty:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Insufficient warehouse stock for {product.name}",
            )

        # Deduct warehouse stock safely
        stock_service.adjust_on_hand(
            db, org_id, warehouse.id, product.id, it.variant_id,
            -it.loaded_qty, movement_type="delivery_out",
            note=f"Vehicle Loading for partner {partner.name}",
            created_by=user.id,
        )

        loading.items.append(
            VehicleLoadingItem(
                product_id=product.id,
                variant_id=it.variant_id,
                product_name=product.line_item_name(variant),
                loaded_qty=it.loaded_qty,
                extra_qty=0,
                returned_qty=0,
                delivered_qty=0,
            )
        )

    db.add(loading)
    db.commit()
    db.refresh(loading)
    return loading


@router.get("/current/{delivery_partner_id}", response_model=VehicleLoadingOut)
def get_current_stock(
    delivery_partner_id: str,
    user: User = Depends(_view),
    db: Session = Depends(get_db),
) -> VehicleLoading:
    """Retrieve what's currently loaded on a delivery partner's vehicle (active session)."""
    org_id = _org_id(user)
    # An "own"-scope user may only look up their own current stock — the path
    # param alone (any other partner's user id) must not grant access.
    if scoping.scope_to_own(db, user) and delivery_partner_id != user.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No active loading session found for this delivery partner.",
        )
    loading = (
        db.query(VehicleLoading)
        .filter(
            VehicleLoading.delivery_partner_id == delivery_partner_id,
            VehicleLoading.organization_id == org_id,
            VehicleLoading.status == "active",
        )
        .first()
    )
    if not loading:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No active loading session found for this delivery partner.",
        )
    return loading


@router.post("/{id}/extra-load", response_model=VehicleLoadingOut)
def add_extra_load(
    id: str,
    payload: ExtraLoadBody,
    user: User = Depends(_edit),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> VehicleLoading:
    """Add extra stock mid-day. Deducts from warehouse."""
    org_id = _org_id(user)
    loading = _owned(db, id, org_id, user)
    if loading.status != "active":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Loading session is not active")

    wh_id = loading.warehouse_id or stock_service.default_warehouse(db, org_id).id
    warehouse = stock_service.owned_warehouse(db, wh_id, org_id, require_active=True)
    if warehouse is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid source warehouse for extra load")

    for it in payload.items:
        product = db.get(Product, it.product_id)
        if product is None or product.organization_id != org_id:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="An item's product is not in your firm")
        
        variant = None
        if it.variant_id:
            variant = db.get(ProductVariant, it.variant_id)
            if variant is None or variant.product_id != product.id:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="An item's variant is invalid")

        # Check warehouse stock
        current_avail = stock_service.available(db, warehouse.id, product.id, it.variant_id)
        if current_avail < it.quantity:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Insufficient warehouse stock for {product.name}",
            )

        # Deduct warehouse stock
        stock_service.adjust_on_hand(
            db, org_id, warehouse.id, product.id, it.variant_id,
            -it.quantity, movement_type="delivery_out",
            note=f"Vehicle Extra Load (Session: {id[:8]})",
            created_by=user.id,
        )

        # Find existing item in loading session
        match = next(
            (
                x
                for x in loading.items
                if x.product_id == it.product_id and x.variant_id == it.variant_id
            ),
            None,
        )
        if match:
            match.extra_qty += it.quantity
        else:
            loading.items.append(
                VehicleLoadingItem(
                    product_id=product.id,
                    variant_id=it.variant_id,
                    product_name=product.line_item_name(variant),
                    loaded_qty=0,
                    extra_qty=it.quantity,
                    returned_qty=0,
                    delivered_qty=0,
                )
            )

    db.commit()
    db.refresh(loading)
    return loading


@router.post("/{id}/end-of-day", response_model=VehicleLoadingOut)
def record_returns(
    id: str,
    payload: EndOfDayBody,
    user: User = Depends(_edit),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> VehicleLoading:
    """Record actual returned stock at the end of the day. Closes the loading session."""
    org_id = _org_id(user)
    loading = _owned(db, id, org_id, user)
    if loading.status != "active":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Loading session is not active")

    wh_id = loading.warehouse_id or stock_service.default_warehouse(db, org_id).id
    warehouse = stock_service.owned_warehouse(db, wh_id, org_id, require_active=True)
    if warehouse is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid source warehouse for EOD return")

    for it in payload.items:
        match = next(
            (
                x
                for x in loading.items
                if x.product_id == it.product_id and x.variant_id == it.variant_id
            ),
            None,
        )
        if not match:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Item not found in this loading session.",
            )

        available_stock = (match.loaded_qty or 0) + (match.extra_qty or 0) - (match.delivered_qty or 0)
        if it.returned_qty > available_stock:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"Returned quantity ({it.returned_qty}) cannot exceed available vehicle stock ({available_stock}) "
                    f"for {match.product_name}."
                ),
            )

        match.returned_qty = it.returned_qty

        # Return stock to exact source warehouse
        stock_service.adjust_on_hand(
            db, org_id, warehouse.id, it.product_id, it.variant_id,
            it.returned_qty, movement_type="adjustment",
            note=f"Vehicle End-of-Day Return (Session: {id[:8]})",
            created_by=user.id,
        )

    loading.status = "closed"
    db.commit()
    db.refresh(loading)
    return loading


@router.post("/{id}/reconcile", response_model=VehicleReconciliationOut, status_code=status.HTTP_201_CREATED)
def reconcile_vehicle_stock(
    id: str,
    payload: ReconcileBody,
    user: User = Depends(_edit),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> VehicleStockReconciliation:
    """Record physical stock count and calculate variance against expected vehicle closing stock.

    Authoritative formulas:
        expected_closing_qty = loaded_qty + extra_qty - delivered_qty - returned_qty
        variance_qty = physical_qty - expected_closing_qty

    Variance interpretation:
        0: Reconciled exactly
        < 0: Physical shortage (missing items)
        > 0: Physical surplus (extra items)
    """
    org_id = _org_id(user)
    loading = _owned(db, id, org_id, user)

    reconciliation = VehicleStockReconciliation(
        organization_id=org_id,
        loading_id=loading.id,
        reconciled_by_id=user.id,
        status="reconciled",
        notes=payload.notes,
    )

    for it in payload.items:
        match = None
        if it.loading_item_id:
            match = next((x for x in loading.items if x.id == it.loading_item_id), None)
        if not match and it.product_id:
            match = next(
                (x for x in loading.items if x.product_id == it.product_id and x.variant_id == it.variant_id),
                None,
            )

        if not match:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Item not found in this loading session for reconciliation.",
            )

        l_qty = match.loaded_qty or 0
        e_qty = match.extra_qty or 0
        d_qty = match.delivered_qty or 0
        r_qty = match.returned_qty or 0
        expected_closing = max((l_qty + e_qty) - d_qty - r_qty, 0)
        variance = it.physical_qty - expected_closing

        reconciliation.items.append(
            VehicleReconciliationItem(
                loading_item_id=match.id,
                product_id=match.product_id,
                variant_id=match.variant_id,
                product_name=match.product_name,
                loaded_qty=l_qty,
                extra_qty=e_qty,
                delivered_qty=d_qty,
                returned_qty=r_qty,
                expected_closing_qty=expected_closing,
                physical_qty=it.physical_qty,
                variance_qty=variance,
                notes=it.notes,
            )
        )

    db.add(reconciliation)
    db.commit()
    db.refresh(reconciliation)
    return reconciliation


@router.get("/{id}/reconciliations", response_model=list[VehicleReconciliationOut])
def get_session_reconciliations(
    id: str,
    user: User = Depends(_view),
    db: Session = Depends(get_db),
) -> list[VehicleStockReconciliation]:
    """List historical stock reconciliation records for a vehicle loading session."""
    org_id = _org_id(user)
    loading = _owned(db, id, org_id, user)
    return (
        db.query(VehicleStockReconciliation)
        .filter(
            VehicleStockReconciliation.loading_id == loading.id,
            VehicleStockReconciliation.organization_id == org_id,
        )
        .order_by(VehicleStockReconciliation.created_at.desc())
        .all()
    )


@router.get("", response_model=PaginatedResponse[VehicleLoadingOut])
def list_all_loadings(
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1, le=100),
    sort_by: str | None = Query(default=None, description="Field to sort by"),
    sort_order: str = Query(default="asc", description="Sort order: asc or desc"),
    search: str | None = Query(default=None),
    delivery_partner_id: str | None = Query(default=None),
    status_filter: str | None = Query(default=None, alias="status"),
    user: User = Depends(_view),
    db: Session = Depends(get_db),
) -> PaginatedResponse[VehicleLoadingOut]:
    """Organization-wide overview for an "all"-scope user (Admin, or a custom
    org-wide role); an "own"-scope user (e.g. Delivery Partner) sees only
    their own sessions — same dynamic list-scoping helper as everywhere else.
    """
    org_id = _org_id(user)
    q = db.query(VehicleLoading).filter(VehicleLoading.organization_id == org_id)
    if delivery_partner_id:
        q = q.filter(VehicleLoading.delivery_partner_id == delivery_partner_id)
    if status_filter:
        q = q.filter(VehicleLoading.status == status_filter)
    # team_columns=(): Vehicle Stock is explicitly excluded from Team Scope.
    q = scoping.owned_by(q, db, user, VehicleLoading.delivery_partner_id, team_columns=())
    q = apply_sorting(
        q,
        sort_by=sort_by,
        sort_order=sort_order,
        sortable_fields=VEHICLE_LOADING_SORTABLE_FIELDS,
        default_order=[VehicleLoading.date.desc(), VehicleLoading.created_at.desc(), VehicleLoading.id.desc()],
        secondary_key=VehicleLoading.id.desc(),
    )
    return paginate(q, page=page, page_size=page_size)



# ------------------------------------------------------------------ Vehicle Stock page tables


def _summary(loading: VehicleLoading) -> VehicleLoadSummaryOut:
    items = loading.items or []
    order_units = sum(i.loaded_qty or 0 for i in items)
    loaded = order_units + sum(i.extra_qty or 0 for i in items)
    delivered = sum(i.delivered_qty or 0 for i in items)
    returned = sum(i.returned_qty or 0 for i in items)
    return VehicleLoadSummaryOut(
        id=loading.id,
        date=loading.date,
        vehicle_id=loading.vehicle_id,
        vehicle_number=loading.vehicle.vehicle_number if loading.vehicle is not None else None,
        delivery_partner_id=loading.delivery_partner_id,
        driver_name=loading.delivery_partner.name if loading.delivery_partner is not None else None,
        warehouse_id=loading.warehouse_id,
        order_units=order_units,
        loaded_units=loaded,
        delivered_units=delivered,
        returned_units=returned,
        available_units=max(loaded - delivered - returned, 0),
        item_count=len(items),
        status=loading.status,
        updated_at=loading.updated_at,
    )


def _scoped_loadings(db: Session, user: User, org_id: str):  # noqa: ANN202
    q = db.query(VehicleLoading).filter(VehicleLoading.organization_id == org_id)
    # Same scope rule as GET /vehicle-stock: an "own"-scope user (Delivery Partner)
    # sees only their own sessions; Vehicle Stock is excluded from Team Scope.
    return scoping.owned_by(q, db, user, VehicleLoading.delivery_partner_id, team_columns=())


@router.get("/today", response_model=list[VehicleLoadSummaryOut])
def todays_vehicle_loads(
    user: User = Depends(_view),
    db: Session = Depends(get_db),
) -> list[VehicleLoadSummaryOut]:
    """"Today's Vehicle Load Information": every vehicle that is loaded right now
    (status active, whatever day it was loaded) plus every session started today
    (business day in the organization's timezone, default Asia/Kolkata), closed or not.
    Active sessions first, then newest first. Not paginated (one row per vehicle run)."""
    from sqlalchemy import or_

    from app.core import business_time

    org_id = _org_id(user)
    tz = business_time.org_timezone(db, org_id)
    day = business_time.today(tz)
    q = _scoped_loadings(db, user, org_id).filter(
        or_(
            VehicleLoading.status == "active",
            VehicleLoading.date.between(business_time.day_start(day, tz), business_time.day_end(day, tz)),
        )
    )
    rows = q.order_by(VehicleLoading.date.desc(), VehicleLoading.id.desc()).all()
    rows.sort(key=lambda r: r.status != "active")  # stable: active first, newest first within
    return [_summary(r) for r in rows]


@router.get("/history", response_model=PaginatedResponse[VehicleLoadSummaryOut])
def vehicle_load_history(
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1, le=100),
    search: str | None = Query(default=None, description="Vehicle number or driver name (partial, case-insensitive)"),
    date_from: date | None = Query(default=None, description="Loaded on/after (YYYY-MM-DD, organization timezone)"),
    date_to: date | None = Query(default=None, description="Loaded on/before (YYYY-MM-DD, organization timezone)"),
    vehicle_id: str | None = Query(default=None),
    delivery_partner_id: str | None = Query(default=None),
    status_filter: str | None = Query(default=None, alias="status", description="active or closed"),
    sort_by: str | None = Query(default=None, description="date, vehicle_number, delivery_partner, status, updated_at"),
    sort_order: str = Query(default="desc", description="asc or desc"),
    user: User = Depends(_view),
    db: Session = Depends(get_db),
) -> PaginatedResponse[VehicleLoadSummaryOut]:
    """"Vehicle Load History": every loading session, newest first, paginated, with
    `total`. Open a row with GET /vehicle-stock/{id}."""
    from sqlalchemy import or_

    from app.core import business_time

    org_id = _org_id(user)
    tz = business_time.org_timezone(db, org_id)
    q = _scoped_loadings(db, user, org_id)
    if vehicle_id:
        q = q.filter(VehicleLoading.vehicle_id == vehicle_id)
    if delivery_partner_id:
        q = q.filter(VehicleLoading.delivery_partner_id == delivery_partner_id)
    if status_filter:
        q = q.filter(VehicleLoading.status == status_filter.strip().lower())
    if date_from:
        q = q.filter(VehicleLoading.date >= business_time.day_start(date_from, tz))
    if date_to:
        q = q.filter(VehicleLoading.date <= business_time.day_end(date_to, tz))
    if search and search.strip():
        term = f"%{search.strip()}%"
        q = q.filter(or_(
            VehicleLoading.vehicle_id.in_(
                select(Vehicle.id).where(Vehicle.organization_id == org_id, Vehicle.vehicle_number.ilike(term))
            ),
            VehicleLoading.delivery_partner_id.in_(
                select(User.id).where(User.organization_id == org_id, User.name.ilike(term))
            ),
        ))
    q = apply_sorting(
        q,
        sort_by=sort_by,
        sort_order=sort_order,
        sortable_fields=VEHICLE_LOADING_SORTABLE_FIELDS,
        default_order=[VehicleLoading.date.desc(), VehicleLoading.created_at.desc(), VehicleLoading.id.desc()],
        secondary_key=VehicleLoading.id.desc(),
    )
    result = paginate(q, page=page, page_size=page_size)
    return PaginatedResponse[VehicleLoadSummaryOut](
        items=[_summary(r) for r in result.items],
        total=result.total,
        page=result.page,
        page_size=result.page_size,
        total_pages=result.total_pages,
    )


@router.get("/{id}", response_model=VehicleLoadingOut)
def get_loading_session(
    id: str,
    user: User = Depends(_view),
    db: Session = Depends(get_db),
) -> VehicleLoading:
    """One loading session by id (active or closed) -- the detail page for a row of
    /today or /history. An "own"-scope user only reaches their own sessions."""
    return _owned(db, id, _org_id(user), user)
