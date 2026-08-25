from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import require_permission, require_unlocked_org
from app.models import (
    Delivery,
    Product,
    ProductVariant,
    StockMovement,
    User,
    VehicleLoading,
    VehicleLoadingItem,
    VehicleReconciliationItem,
    VehicleStockReconciliation,
)
from app.services import delivery_service
from app.schemas.vehicle_stock import (
    DeliveryLoadOut,
    EndOfDayBody,
    ExtraLoadBody,
    LoadedLineOut,
    ReconcileBody,
    VehicleLoadingCreate,
    VehicleLoadingOut,
    VehicleReconciliationOut,
)

router = APIRouter(prefix="/vehicle-stock", tags=["vehicle_stock"])

_view = require_permission("vehicle_stock", "view")
_create = require_permission("vehicle_stock", "create")
_edit = require_permission("vehicle_stock", "edit")


def _org_id(user: User) -> str:
    if not user.organization_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No organization on this account")
    return user.organization_id


def _owned(db: Session, id: str, org_id: str) -> VehicleLoading:
    loading = db.get(VehicleLoading, id)
    if loading is None or loading.organization_id != org_id:
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

    if payload.delivery_id:
        delivery = db.get(Delivery, payload.delivery_id)
        if delivery is None or delivery.organization_id != org_id:
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

    loading = VehicleLoading(
        organization_id=org_id,
        delivery_partner_id=partner.id,
        vehicle_id=payload.vehicle_id,
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

        # Check stock in main warehouse
        current_inv = variant.inventory if variant else product.total_inventory
        if current_inv < it.loaded_qty:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Insufficient warehouse stock for {product.name}",
            )

        # Deduct warehouse stock
        new_inv = current_inv - it.loaded_qty
        if variant:
            variant.inventory = new_inv
        else:
            product.total_inventory = new_inv

        db.add(
            StockMovement(
                organization_id=org_id,
                product_id=product.id,
                variant_id=it.variant_id,
                movement_type="delivery_out",
                quantity=-it.loaded_qty,
                balance_after=new_inv,
                note=f"Vehicle Loading for partner {partner.name}",
                created_by=user.id,
            )
        )

        loading.items.append(
            VehicleLoadingItem(
                product_id=product.id,
                variant_id=it.variant_id,
                product_name=product.name if not variant else f"{product.name} ({variant.name})",
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
    loading = _owned(db, id, org_id)
    if loading.status != "active":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Loading session is not active")

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
        current_inv = variant.inventory if variant else product.total_inventory
        if current_inv < it.quantity:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Insufficient warehouse stock for {product.name}",
            )

        # Deduct warehouse stock
        new_inv = current_inv - it.quantity
        if variant:
            variant.inventory = new_inv
        else:
            product.total_inventory = new_inv

        db.add(
            StockMovement(
                organization_id=org_id,
                product_id=product.id,
                variant_id=it.variant_id,
                movement_type="delivery_out",
                quantity=-it.quantity,
                balance_after=new_inv,
                note=f"Vehicle Extra Load (Session: {id[:8]})",
                created_by=user.id,
            )
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
                    product_name=product.name if not variant else f"{product.name} ({variant.name})",
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
    loading = _owned(db, id, org_id)
    if loading.status != "active":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Loading session is not active")

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

        # Return stock to warehouse
        if it.variant_id:
            variant = db.get(ProductVariant, it.variant_id)
            if variant:
                variant.inventory = (variant.inventory or 0) + it.returned_qty
                new_inv = variant.inventory
        else:
            product = db.get(Product, it.product_id)
            if product:
                product.total_inventory = (product.total_inventory or 0) + it.returned_qty
                new_inv = product.total_inventory

        db.add(
            StockMovement(
                organization_id=org_id,
                product_id=it.product_id,
                variant_id=it.variant_id,
                movement_type="adjustment",
                quantity=it.returned_qty,
                balance_after=new_inv,
                note=f"Vehicle End-of-Day Return (Session: {id[:8]})",
                created_by=user.id,
            )
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
    loading = _owned(db, id, org_id)

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
    loading = _owned(db, id, org_id)
    return (
        db.query(VehicleStockReconciliation)
        .filter(
            VehicleStockReconciliation.loading_id == loading.id,
            VehicleStockReconciliation.organization_id == org_id,
        )
        .order_by(VehicleStockReconciliation.created_at.desc())
        .all()
    )


@router.get("", response_model=list[VehicleLoadingOut])
def list_all_loadings(
    user: User = Depends(_view),
    delivery_partner_id: str | None = Query(default=None),
    status_filter: str | None = Query(default=None, alias="status"),
    db: Session = Depends(get_db),
) -> list[VehicleLoading]:
    """Admin overview across all delivery partners and sessions."""
    org_id = _org_id(user)
    q = db.query(VehicleLoading).filter(VehicleLoading.organization_id == org_id)
    if delivery_partner_id:
        q = q.filter(VehicleLoading.delivery_partner_id == delivery_partner_id)
    if status_filter:
        q = q.filter(VehicleLoading.status == status_filter)
    return q.order_by(VehicleLoading.date.desc()).all()

