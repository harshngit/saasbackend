from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core import scoping
from app.core.deps import require_permission, require_unlocked_org
from app.models import (
    Customer,
    Product,
    ProductVariant,
    Quotation,
    QuotationItem,
    User,
)
from app.services import numbering_service, lookup_service
from app.schemas.quotation import QuotationCreate, QuotationListItem, QuotationOut

router = APIRouter(prefix="/quotations", tags=["quotations"])

_view = require_permission("quotations", "view")
_create = require_permission("quotations", "create")
_edit = require_permission("quotations", "edit")
_delete = require_permission("quotations", "delete")


def _org_id(user: User) -> str:
    if not user.organization_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No organization on this account")
    return user.organization_id


def _owned(db: Session, id: str, org_id: str, user: User | None = None) -> Quotation:
    """Accepts the UUID or the human-facing code (quotation_number)."""
    record = lookup_service.by_id_or_code(
        db, Quotation, id, org_id, Quotation.quotation_number
    )
    if record is None or (
        user is not None and not scoping.owns_record(db, user, record, "salesperson_id")
    ):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Quotation not found")
    return record


@router.post("", response_model=QuotationOut, status_code=status.HTTP_201_CREATED)
def create_quotation(
    payload: QuotationCreate,
    user: User = Depends(_create),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> Quotation:
    org_id = _org_id(user)

    # Validate customer
    customer = db.get(Customer, payload.customer_id)
    if customer is None or customer.organization_id != org_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid customer_id")

    # Validate salesperson if provided
    if payload.salesperson_id:
        sales = db.get(User, payload.salesperson_id)
        if sales is None or sales.organization_id != org_id:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid salesperson_id")

    quotation = Quotation(
        organization_id=org_id,
        quotation_number=payload.quotation_number or numbering_service.next_number(
            db, org_id, Quotation.quotation_number, "QT"
        ),
        quotation_date=payload.quotation_date or datetime.now(timezone.utc),
        valid_until=payload.valid_until,
        customer_id=payload.customer_id,
        # Sheet marks both addresses "Auto-filled" — default them from the
        # customer the quotation is for, rather than making the user retype them.
        billing_address=payload.billing_address or customer.billing_address,
        shipping_address=payload.shipping_address or customer.delivery_address,
        # Default to the creator for a field role — see customers.create_customer.
        salesperson_id=payload.salesperson_id
        or (user.id if scoping.scope_to_own(db, user) else None),
        currency=payload.currency,
        status=payload.status or "draft",
        payment_terms=payload.payment_terms,
        delivery_terms=payload.delivery_terms,
        notes=payload.notes,
        terms_conditions=payload.terms_conditions,
    )

    for item in payload.items:
        product = db.get(Product, item.product_id)
        if product is None or product.organization_id != org_id:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Product {item.product_id} not found")

        variant_name = ""
        if item.variant_id:
            variant = db.get(ProductVariant, item.variant_id)
            if variant is None or variant.product_id != product.id:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid variant_id")
            variant_name = f" ({variant.name})"

        quotation.items.append(
            QuotationItem(
                product_id=product.id,
                variant_id=item.variant_id,
                product_name=f"{product.name}{variant_name}",
                quantity=item.quantity,
                uom=item.uom,
                unit_price=item.unit_price,
            )
        )

    db.add(quotation)
    db.commit()
    db.refresh(quotation)
    return quotation


@router.get("", response_model=list[QuotationListItem])
def list_quotations(
    user: User = Depends(_view),
    db: Session = Depends(get_db),
) -> list[Quotation]:
    query = db.query(Quotation).filter(Quotation.organization_id == _org_id(user))
    # A field role sees only their own quotations.
    query = scoping.owned_by(query, db, user, Quotation.salesperson_id)
    return query.order_by(Quotation.created_at.desc()).all()


@router.get("/{id}", response_model=QuotationOut)
def get_quotation_detail(
    id: str,
    user: User = Depends(_view),
    db: Session = Depends(get_db),
) -> Quotation:
    return _owned(db, id, _org_id(user), user)


@router.delete("/{id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_quotation(
    id: str,
    user: User = Depends(_delete),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> None:
    q = _owned(db, id, _org_id(user))
    db.delete(q)
    db.commit()
