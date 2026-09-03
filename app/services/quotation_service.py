"""Quotation lifecycle: creation, party validation, status workflow, editing
rules, conversion to Sales Order, and deletion — the business rules
app/routers/quotations.py enforces.
"""

from datetime import datetime, timezone

from fastapi import HTTPException, status
from sqlalchemy import update as sa_update
from sqlalchemy.orm import Session

from app.core import scoping, workflow
from app.core.workflow import QuotationTransitionError, validate_quotation_transition
from app.models import Customer, Lead, Product, ProductVariant, Quotation, QuotationItem, User
from app.schemas.quotation import ConversionOut, ConvertedOrderBrief, ConvertToOrder, QuotationCreate, QuotationUpdate
from app.services import lookup_service, numbering_service, order_service


def _require_quotation_transition(current: str, new: str) -> None:
    try:
        validate_quotation_transition(current, new)
    except QuotationTransitionError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=exc.message) from exc


def _validate_party_for_create(customer_id: str | None, lead_id: str | None) -> None:
    """A new quotation must start with exactly one party. (An existing
    quotation legitimately ends up with both set later — see the Lead
    conversion auto-link in lead_service.convert_lead_to_customer, which
    preserves lead_id for history while adding customer_id — but that state
    is never a valid *starting* point.)"""
    if customer_id and lead_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="A quotation cannot have both customer_id and lead_id — choose one",
        )
    if not customer_id and not lead_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="A quotation needs exactly one party: customer_id or lead_id",
        )


def _validate_party_for_update(data: dict, quotation: Quotation) -> None:
    """Same "exactly one party" rule, but tolerant of a quotation that already
    carries both customer_id and lead_id (the Lead-conversion history case):
    only reject "neither" against the state this PATCH would *result* in, and
    only reject "both" when this specific request is the one setting both to
    truthy values together — editing an unrelated field, or clearing/changing
    just one side of an already-both-set quotation, must not be blocked by
    the other side's pre-existing historical value."""
    if "customer_id" not in data and "lead_id" not in data:
        return
    resulting_customer_id = data["customer_id"] if "customer_id" in data else quotation.customer_id
    resulting_lead_id = data["lead_id"] if "lead_id" in data else quotation.lead_id
    if not resulting_customer_id and not resulting_lead_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="A quotation needs exactly one party: customer_id or lead_id",
        )
    if "customer_id" in data and "lead_id" in data and data["customer_id"] and data["lead_id"]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="A quotation cannot have both customer_id and lead_id — choose one",
        )


def _validate_customer(db: Session, org_id: str, customer_id: str) -> Customer:
    customer = db.get(Customer, customer_id)
    if customer is None or customer.organization_id != org_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid customer_id")
    return customer


def _validate_lead(db: Session, org_id: str, lead_id: str) -> Lead:
    lead = db.get(Lead, lead_id)
    if lead is None or lead.organization_id != org_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid lead_id")
    return lead


def _validate_salesperson(db: Session, org_id: str, salesperson_id: str) -> User:
    sales = db.get(User, salesperson_id)
    if sales is None or sales.organization_id != org_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid salesperson_id")
    return sales


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


def get_quotation(db: Session, org_id: str, quotation_id: str, user: User | None = None) -> Quotation:
    """Accepts the UUID or the human-facing code (quotation_number)."""
    record = lookup_service.by_id_or_code(db, Quotation, quotation_id, org_id, Quotation.quotation_number)
    if record is None or (
        user is not None and not scoping.owns_record(db, user, record, "salesperson_id")
    ):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Quotation not found")
    return record


def create_quotation(db: Session, org_id: str, user: User, payload: QuotationCreate) -> Quotation:
    _validate_party_for_create(payload.customer_id, payload.lead_id)

    customer = _validate_customer(db, org_id, payload.customer_id) if payload.customer_id else None
    if payload.lead_id:
        _validate_lead(db, org_id, payload.lead_id)
    if payload.salesperson_id:
        _validate_salesperson(db, org_id, payload.salesperson_id)

    quotation = Quotation(
        organization_id=org_id,
        quotation_number=payload.quotation_number or numbering_service.next_number(
            db, org_id, Quotation.quotation_number, "QT"
        ),
        quotation_date=payload.quotation_date or datetime.now(timezone.utc),
        valid_until=payload.valid_until,
        customer_id=payload.customer_id,
        lead_id=payload.lead_id,
        # Sheet marks both addresses "Auto-filled" — default them from the
        # customer the quotation is for, rather than making the user retype them.
        # A Lead carries no address of its own, so a Lead quotation only gets
        # what the caller explicitly sends.
        billing_address=payload.billing_address or (customer.billing_address if customer else None),
        shipping_address=payload.shipping_address or (customer.delivery_address if customer else None),
        # Default to the creator for a field role — see customers.create_customer.
        salesperson_id=payload.salesperson_id
        or (user.id if scoping.scope_to_own(db, user) else None),
        currency=payload.currency,
        status="draft",
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


def list_quotations(db: Session, org_id: str, user: User) -> list[Quotation]:
    query = db.query(Quotation).filter(Quotation.organization_id == org_id)
    # A field role sees only their own quotations.
    query = scoping.owned_by(query, db, user, Quotation.salesperson_id)
    return query.order_by(Quotation.created_at.desc()).all()


# Fields whose change alone does not count as a "meaningful edit" for the
# sent/rejected -> draft reset below: reassigning who owns the quotation, or
# resending the same status, doesn't change what was quoted to the buyer.
_ADMIN_ONLY_FIELDS = {"status", "salesperson_id"}

# Statuses a meaningful content edit resets to "draft" from. `expired` isn't
# listed here on purpose: it's never the stored value — a derived-expired
# quotation's real `status` column is still "sent", so it's already covered.
_RESET_TO_DRAFT_FROM = {"sent", "rejected"}


def update_quotation(db: Session, org_id: str, quotation_id: str, user: User, payload: QuotationUpdate) -> Quotation:
    """Edit a quotation. Only the fields you send change; sending `items` replaces
    the whole line set, which is what the edit screen holds.

    Workflow consequences of editing:
      - `converted`: frozen completely — the order already exists.
      - `accepted`: frozen completely except re-sending the same status as a
        no-op — the only way forward from here is POST .../convert-to-order.
      - `sent` / `rejected`: a meaningful content edit (anything besides
        `status`/`salesperson_id`) silently resets `status` to `draft`, unless
        this same request also explicitly asks for a different, validly
        reachable status (e.g. `sent` -> `accepted`), in which case the
        explicit request wins and is checked against the transition table
        instead.
    """
    quotation = get_quotation(db, org_id, quotation_id, user)

    if quotation.status == "converted":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"This quotation became order {quotation.converted_order_id} and can no longer be edited",
        )

    data = payload.model_dump(exclude_unset=True)
    if not data:
        return quotation

    if quotation.status == "accepted":
        touches_content = bool(set(data.keys()) - {"status"})
        wants_different_status = "status" in data and data["status"] != "accepted"
        if touches_content or wants_different_status:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="This quotation has been accepted and can no longer be edited — "
                       "use POST /quotations/{id}/convert-to-order to convert it",
            )
        return quotation

    if data.get("status") == "converted":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Use POST /quotations/{id}/convert-to-order to convert a quotation",
        )

    _validate_party_for_update(data, quotation)

    if data.get("customer_id"):
        _validate_customer(db, org_id, data["customer_id"])
    if data.get("lead_id"):
        _validate_lead(db, org_id, data["lead_id"])
    if data.get("salesperson_id"):
        _validate_salesperson(db, org_id, data["salesperson_id"])

    requested_status = data.get("status")
    if requested_status and requested_status != quotation.status:
        _require_quotation_transition(quotation.status, requested_status)
    else:
        meaningful = bool(set(data.keys()) - _ADMIN_ONLY_FIELDS)
        if meaningful and quotation.status in _RESET_TO_DRAFT_FROM:
            data["status"] = "draft"

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


def delete_quotation(db: Session, org_id: str, quotation_id: str, user: User) -> None:
    quotation = get_quotation(db, org_id, quotation_id, user)
    if quotation.status == "converted":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"This quotation became order {quotation.converted_order_id}. Cancel that order instead.",
        )
    if quotation.status == "accepted":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This quotation has been accepted and cannot be deleted",
        )
    db.delete(quotation)
    db.commit()


def convert_to_order(
    db: Session, org_id: str, user: User, quotation: Quotation, payload: ConvertToOrder
) -> ConversionOut:
    """Turn an accepted quotation into a sales order. See app/routers/quotations.py
    for the full endpoint docstring.

    Concurrency: the Quotation row is locked with a real `UPDATE` before its
    status is (re-)read, so two simultaneous conversion attempts on the same
    quotation serialize instead of both reading `status == "accepted"` and
    each creating an Order. A real `UPDATE` is used deliberately, not
    `.with_for_update()`: `Quotation.customer` / `.lead` / `.salesperson` are
    all `lazy="joined"`, and Postgres refuses `FOR UPDATE` on the nullable
    side of the `LEFT OUTER JOIN` a locked SELECT against this model would
    pull in — the same failure class already fixed for WarehouseStock locking
    in stock_service.py and used for Lead conversion in lead_service.py. This
    no-op UPDATE takes a genuine row lock on Postgres, and SQLite's
    file-level write lock, the same pattern proven in both of those.
    """
    db.execute(sa_update(Quotation).where(Quotation.id == quotation.id).values(status=Quotation.status))
    db.refresh(quotation)

    if quotation.status == "converted":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Already converted to order {quotation.converted_order_id}",
        )
    if quotation.status != "accepted":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Only an accepted quotation can be converted to an order "
                   f"(current status: '{quotation.status}')",
        )
    if not quotation.items:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="This quotation has no lines to convert"
        )
    if quotation.lead_id and not quotation.customer_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This is a Lead quotation — convert the Lead to a Customer and link this "
                   "quotation to them before converting it to an order",
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
