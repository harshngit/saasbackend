from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import require_permission, require_unlocked_org
from app.models import Customer, Lead, Quotation, User
from app.core.pdf_docs import quotation_pdf
from app.services import quotation_service
from app.schemas.quotation import (
    ConversionOut,
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


@router.post("", response_model=QuotationOut, status_code=status.HTTP_201_CREATED)
def create_quotation(
    payload: QuotationCreate,
    user: User = Depends(_create),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> Quotation:
    return quotation_service.create_quotation(db, _org_id(user), user, payload)


@router.get("", response_model=list[QuotationListItem])
def list_quotations(
    user: User = Depends(_view),
    db: Session = Depends(get_db),
) -> list[Quotation]:
    return quotation_service.list_quotations(db, _org_id(user), user)


@router.get("/{id}", response_model=QuotationOut)
def get_quotation_detail(
    id: str,
    user: User = Depends(_view),
    db: Session = Depends(get_db),
) -> Quotation:
    return quotation_service.get_quotation(db, _org_id(user), id, user)


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
    the two disagree. An `accepted` quotation is frozen too: the only way forward
    from there is POST /quotations/{id}/convert-to-order. `status` otherwise moves
    through draft -> sent -> accepted / rejected, and a meaningful content edit to a
    `sent` or `rejected` quotation silently resets it to `draft`. `converted` is set
    only by the conversion endpoint, never sent in. See app.core.workflow for the
    full transition table.
    """
    return quotation_service.update_quotation(db, _org_id(user), id, user, payload)


@router.post("/{id}/convert-to-order", response_model=ConversionOut, status_code=status.HTTP_201_CREATED)
def convert_to_order(
    id: str,
    payload: ConvertToOrder,
    user: User = Depends(_edit),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> ConversionOut:
    """Turn an accepted quotation into a sales order.

    Requires the quotation to be `accepted` and linked to a real Customer — a Lead
    quotation must be linked to a Customer first (e.g. by converting the Lead).
    Send no lines: the customer, products, variants, quantities, rates, discounts,
    taxes, terms and salesperson are all copied from the quotation. Only the
    fulfilment terms the order needs — warehouse, delivery date, fulfilment method,
    payment type and terms — come in the body.

    The order is placed through exactly the same path as POST /orders: if the firm's
    `draft_orders_enabled` is off, its stock is reserved and its status is `placed`
    (or `awaiting_approval` if the firm asks for approval). If `draft_orders_enabled`
    is on, the order lands as `draft` — no stock check, no reservation — until
    POST /orders/{id}/confirm reserves it. The quotation becomes `converted` and is
    frozen either way; converting twice, or converting concurrently, is refused —
    exactly one Order is ever created for a given quotation, see
    quotation_service.convert_to_order for the locking strategy.

    A shortage refuses the conversion with `INSUFFICIENT_STOCK` and leaves the
    quotation untouched — the same rule as placing an order by hand. A draft
    conversion skips that check, same as POST /orders does for a draft.
    """
    org_id = _org_id(user)
    quotation = quotation_service.get_quotation(db, org_id, id, user)
    return quotation_service.convert_to_order(db, org_id, user, quotation, payload)


@router.get("/{id}/pdf")
def quotation_pdf_download(
    id: str,
    user: User = Depends(_view),
    db: Session = Depends(get_db),
) -> Response:
    """The quotation as a PDF, to send to the customer — or, for a Lead
    quotation, the Lead."""
    quotation = quotation_service.get_quotation(db, _org_id(user), id, user)
    customer = db.get(Customer, quotation.customer_id) if quotation.customer_id else None
    lead = db.get(Lead, quotation.lead_id) if quotation.lead_id else None
    body = quotation_pdf(user.organization, customer, quotation, lead=lead)
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
    quotation_service.delete_quotation(db, _org_id(user), id, user)
