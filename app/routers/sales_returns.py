from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import require_permission, require_unlocked_org
from app.models import (
    Customer,
    Invoice,
    Product,
    ProductVariant,
    ReturnItem,
    SalesReturn,
    StockMovement,
    User,
)
from app.services import numbering_service
from app.schemas.sales_return import SalesReturnCreate, SalesReturnOut

router = APIRouter(prefix="/sales-returns", tags=["sales_returns"])

_view = require_permission("sales_returns", "view")
_create = require_permission("sales_returns", "create")


def _org_id(user: User) -> str:
    if not user.organization_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No organization on this account")
    return user.organization_id


@router.post("", response_model=SalesReturnOut, status_code=status.HTTP_201_CREATED)
def create_sales_return(
    payload: SalesReturnCreate,
    user: User = Depends(_create),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> SalesReturn:
    org_id = _org_id(user)

    # Validate customer
    customer = db.get(Customer, payload.customer_id)
    if customer is None or customer.organization_id != org_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid customer_id")

    invoice = None
    if payload.invoice_reference_id:
        invoice = db.get(Invoice, payload.invoice_reference_id)
        if invoice is None or invoice.organization_id != org_id:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid invoice_reference_id")

    sales_return = SalesReturn(
        organization_id=org_id,
        return_number=payload.return_number or numbering_service.next_number(
            db, org_id, SalesReturn.return_number, "RET"
        ),
        return_date=payload.return_date or datetime.now(timezone.utc),
        customer_id=payload.customer_id,
        invoice_reference_id=payload.invoice_reference_id,
        return_reason=payload.return_reason,
        return_type=payload.return_type or "Credit Note",
        return_status=payload.return_status or "requested",
    )

    total_return_value = 0.0

    for item in payload.items:
        product = db.get(Product, item.product_id)
        if product is None or product.organization_id != org_id:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Product {item.product_id} not found")

        # Resolve unit price (invoice item price if linked, else product price)
        unit_price = product.price
        if invoice:
            match_inv_item = next(
                (
                    x
                    for x in invoice.items
                    if x.product_id == item.product_id and x.variant_id == item.variant_id
                ),
                None,
            )
            if match_inv_item:
                unit_price = match_inv_item.unit_price

        total_return_value += unit_price * item.quantity_returned

        # Return stock to warehouse
        if item.variant_id:
            variant = db.get(ProductVariant, item.variant_id)
            if variant:
                variant.inventory = (variant.inventory or 0) + int(item.quantity_returned)
                new_inv = variant.inventory
        else:
            product.total_inventory = (product.total_inventory or 0) + int(item.quantity_returned)
            new_inv = product.total_inventory

        db.add(
            StockMovement(
                organization_id=org_id,
                product_id=product.id,
                variant_id=item.variant_id,
                movement_type="sales_return",
                quantity=int(item.quantity_returned),
                balance_after=new_inv,
                note=f"Sales Return {sales_return.return_number}",
                created_by=user.id,
            )
        )

        variant_name = ""
        if item.variant_id:
            variant = db.get(ProductVariant, item.variant_id)
            if variant:
                variant_name = f" ({variant.name})"

        sales_return.items.append(
            ReturnItem(
                product_id=product.id,
                variant_id=item.variant_id,
                product_name=f"{product.name}{variant_name}",
                quantity_returned=item.quantity_returned,
            )
        )

    # Recompute customer outstanding receivables if return was approved/completed
    # We reduce customer total_billed by the returned value
    if payload.return_status == "Approved" or payload.return_status is None:
        customer.total_billed = round((customer.total_billed or 0) - total_return_value, 2)
        customer.recompute_outstanding()

    db.add(sales_return)
    db.commit()
    db.refresh(sales_return)
    return sales_return


@router.get("", response_model=list[SalesReturnOut])
def list_sales_returns(
    user: User = Depends(_view),
    db: Session = Depends(get_db),
) -> list[SalesReturn]:
    return (
        db.query(SalesReturn)
        .filter(SalesReturn.organization_id == _org_id(user))
        .order_by(SalesReturn.return_date.desc())
        .all()
    )


@router.get("/{id}", response_model=SalesReturnOut)
def get_sales_return_detail(
    id: str,
    user: User = Depends(_view),
    db: Session = Depends(get_db),
) -> SalesReturn:
    sr = db.get(SalesReturn, id)
    if sr is None or sr.organization_id != _org_id(user):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Sales return record not found")
    return sr
