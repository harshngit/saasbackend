from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core import scoping, workflow
from app.core.deps import require_permission, require_unlocked_org
from app.models import (
    Customer,
    Product,
    ProductVariant,
    Quotation,
    QuotationItem,
    User,
)
from app.core.pdf_docs import quotation_pdf
from app.services import lookup_service, numbering_service, order_service
from app.schemas.quotation import (
    ConversionOut,
    ConvertedOrderBrief,
    ConvertToOrder,
    QuotationCreate,
    QuotationListItem,
    QuotationOut,
    QuotationUpdate,
)

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


def _build_items(db: Session, org_id: str, lines) -> list[QuotationItem]:  # noqa: ANN001
    """Validate each quoted line and snapshot the name it was quoted under."""
    built = []
    for item in lines:
        product = db.get(Product, item.product_id)
        if product is None or product.organization_id != org_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Product {item.product_id} not found",
            )
        variant_name = ""
        if item.variant_id:
            variant = db.get(ProductVariant, item.variant_id)
            if variant is None or variant.product_id != product.id:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid variant_id"
                )
            variant_name = f" ({variant.name})"
        built.append(
            QuotationItem(
                product_id=product.id,
                variant_id=item.variant_id,
                product_name=f"{product.name}{variant_name}",
                quantity=item.quantity,
                uom=item.uom,
                unit_price=item.unit_price,
                discount=item.discount,
                discount_percent=item.discount_percent,
                # The line's own rate, else the product's — never a hardcoded figure.
                tax_rate=item.tax_rate if item.tax_rate is not None else product.tax_rate,
            )
        )
    return built


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

    quotation.items.extend(_build_items(db, org_id, payload.items))

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


@router.patch("/{id}", response_model=QuotationOut)
def update_quotation(
    id: str,
    payload: QuotationUpdate,
    user: User = Depends(_edit),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> Quotation:
    """Edit a quotation. Only the fields you send change; sending `items` replaces the
    whole line set, which is what the edit screen holds.

    A quotation that has already become an order is frozen — the order carries the
    agreed terms from that point on, so changing the quotation behind it would make
    the two disagree. `status` moves through draft → sent → accepted / rejected /
    expired; `converted` is set by the conversion endpoint, never sent in.
    """
    org_id = _org_id(user)
    quotation = _owned(db, id, org_id, user)
    if quotation.status == "converted":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"This quotation became order {quotation.converted_order_id} and can no longer be edited",
        )

    data = payload.model_dump(exclude_unset=True)
    if data.get("status") == "converted":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Use POST /quotations/{id}/convert-to-order to convert a quotation",
        )
    if data.get("customer_id"):
        customer = db.get(Customer, data["customer_id"])
        if customer is None or customer.organization_id != org_id:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid customer_id")
    if data.get("salesperson_id"):
        sales = db.get(User, data["salesperson_id"])
        if sales is None or sales.organization_id != org_id:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid salesperson_id")

    items = data.pop("items", None)
    for field, value in data.items():
        setattr(quotation, field, value)
    if items is not None:
        if not items:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="A quotation needs at least one line"
            )
        quotation.items.clear()
        db.flush()
        quotation.items.extend(_build_items(db, org_id, payload.items))

    db.commit()
    db.refresh(quotation)
    return quotation


@router.post("/{id}/convert-to-order", response_model=ConversionOut, status_code=status.HTTP_201_CREATED)
def convert_to_order(
    id: str,
    payload: ConvertToOrder,
    user: User = Depends(_edit),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> ConversionOut:
    """Turn a quotation into a sales order.

    Send no lines: the customer, products, variants, quantities, rates, discounts,
    taxes, terms and salesperson are all copied from the quotation. Only the
    fulfilment terms the order needs — warehouse, delivery date, fulfilment method,
    payment type and terms — come in the body.

    The order is placed through exactly the same path as POST /orders: if the firm's
    `draft_orders_enabled` is off, its stock is reserved and its status is `placed`
    (or `awaiting_approval` if the firm asks for approval). If `draft_orders_enabled`
    is on, the order lands as `draft` — no stock check, no reservation — until
    POST /orders/{id}/confirm reserves it. The quotation becomes `converted` and is
    frozen either way; converting twice is refused, and both records point at each
    other.

    A shortage refuses the conversion with `INSUFFICIENT_STOCK` and leaves the
    quotation untouched — the same rule as placing an order by hand. A draft
    conversion skips that check, same as POST /orders does for a draft.
    """
    org_id = _org_id(user)
    quotation = _owned(db, id, org_id, user)
    if quotation.status == "converted":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Already converted to order {quotation.converted_order_id}",
        )
    if quotation.status == "rejected":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="A rejected quotation cannot be converted"
        )
    if not quotation.items:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="This quotation has no lines to convert"
        )
    customer = db.get(Customer, quotation.customer_id) if quotation.customer_id else None
    if customer is None or customer.organization_id != org_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This quotation has no customer to raise an order for",
        )

    settings = workflow.sales_settings(user.organization)
    resolved_billing = payload.billing_address or quotation.billing_address or customer.billing_address
    resolved_shipping = (
        payload.shipping_address
        or payload.delivery_address
        or quotation.shipping_address
        or customer.delivery_address
    )
    resolved_payment_terms = payload.payment_terms or quotation.payment_terms
    resolved_delivery_terms = payload.delivery_terms or quotation.delivery_terms
    resolved_currency = quotation.currency or "INR"

    order, warnings = order_service.place_order(
        db, user, customer,
        lines=[
            order_service.OrderLine(
                product_id=item.product_id,
                variant_id=item.variant_id,
                quantity=item.quantity,
                unit_price=item.unit_price,
                discount=item.discount or 0,
                discount_percent=item.discount_percent or 0,
                tax_rate=item.tax_rate,
                uom=item.uom,
            )
            for item in quotation.items
            if item.product_id
        ],
        warehouse_id=payload.warehouse_id,
        delivery_date=payload.delivery_date,
        fulfilment_method=payload.fulfilment_method,
        # The quotation's own terms unless the conversion overrides them.
        payment_type=payload.payment_type,
        payment_terms_days=payload.payment_terms_days,
        salesperson_id=quotation.salesperson_id,
        quotation_id=quotation.id,
        notes=quotation.notes,
        # Same shared path POST /orders uses: a draft-enabled firm gets an
        # unreserved draft here too, confirmed later via POST /orders/{id}/confirm.
        create_as_draft=settings["draft_orders_enabled"],
        billing_address=resolved_billing,
        shipping_address=resolved_shipping,
        delivery_address=resolved_shipping,
        payment_terms=resolved_payment_terms,
        delivery_terms=resolved_delivery_terms,
        currency=resolved_currency,
    )
    _ = warnings  # surfaced on the order itself via GET /orders/{id}

    quotation.status = "converted"
    quotation.converted_order_id = order.id
    quotation.converted_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(order)
    db.refresh(quotation)
    return ConversionOut(
        quotation_id=quotation.id,
        quotation_number=quotation.quotation_number,
        quotation_status=quotation.status,
        order=ConvertedOrderBrief.model_validate(order),
    )


@router.get("/{id}/pdf")
def quotation_pdf_download(
    id: str,
    user: User = Depends(_view),
    db: Session = Depends(get_db),
) -> Response:
    """The quotation as a PDF, to send to the customer."""
    quotation = _owned(db, id, _org_id(user), user)
    customer = db.get(Customer, quotation.customer_id) if quotation.customer_id else None
    body = quotation_pdf(user.organization, customer, quotation)
    return Response(
        content=body,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'inline; filename="{quotation.quotation_number}.pdf"'
        },
    )


@router.delete("/{id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_quotation(
    id: str,
    user: User = Depends(_delete),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> None:
    q = _owned(db, id, _org_id(user))
    if q.status == "converted":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"This quotation became order {q.converted_order_id}. Cancel that order instead.",
        )
    db.delete(q)
    db.commit()
