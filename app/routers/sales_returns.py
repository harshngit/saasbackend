"""Sales returns: request, receive, condition check, approve, credit note.

`POST /sales-returns` records a **request** only — the goods have not arrived, so
nothing moves and nothing is credited. What comes back and in what state is decided
later, and only then do saleable goods re-enter stock and a credit note go out.
"""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.core import workflow
from app.core.database import get_db
from app.core.deps import require_permission, require_unlocked_org
from app.models import Customer, Invoice, SalesReturn, User
from app.schemas.sales_return import (
    ReturnApproveBody,
    ReturnReceiveBody,
    ReturnRejectBody,
    SalesReturnCreate,
    SalesReturnOut,
    SalesReturnUpdate,
)
from app.services import lookup_service, numbering_service, return_service, stock_service
from app.services.return_service import ReturnError

router = APIRouter(prefix="/sales-returns", tags=["sales_returns"])

_view = require_permission("sales_returns", "view")
_create = require_permission("sales_returns", "create")
_edit = require_permission("sales_returns", "edit")
_approve = require_permission("sales_returns", "approve")
_delete = require_permission("sales_returns", "delete")


def _org_id(user: User) -> str:
    if not user.organization_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="No organization on this account"
        )
    return user.organization_id


def _owned(db: Session, return_id: str, org_id: str) -> SalesReturn:
    """Accepts the UUID or the human-facing RET- number."""
    record = lookup_service.by_id_or_code(
        db, SalesReturn, return_id, org_id, SalesReturn.return_number
    )
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Sales return record not found"
        )
    return record


def _bad_request(exc: ReturnError) -> HTTPException:
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))


def _resolve_parties(
    db: Session, org_id: str, invoice_id: str | None, customer_id: str | None
) -> tuple[Invoice | None, Customer | None]:
    """The invoice the goods were sold on, and whose account this affects.

    The customer comes from the invoice whenever there is one, so the app never has to
    send who bought the goods twice.
    """
    invoice = None
    if invoice_id:
        invoice = db.get(Invoice, invoice_id)
        if invoice is None or invoice.organization_id != org_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="invoice_reference_id is not an invoice in your firm",
            )
        if invoice.is_credit_note:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="That is a credit note, not an invoice goods were sold on",
            )

    customer = None
    if invoice is not None and invoice.customer_id:
        customer = db.get(Customer, invoice.customer_id)
        if customer_id and customer_id != customer.id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="That invoice was raised for a different customer",
            )
    elif customer_id:
        customer = db.get(Customer, customer_id)
        if customer is None or customer.organization_id != org_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="customer_id is not a customer in your firm",
            )

    if invoice is None and customer is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Send invoice_reference_id, or customer_id when there is no invoice",
        )
    return invoice, customer


@router.post("", response_model=SalesReturnOut, status_code=status.HTTP_201_CREATED)
def create_sales_return(
    payload: SalesReturnCreate,
    user: User = Depends(_create),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> SalesReturn:
    """Raise a return request. Nothing moves yet.

    Name the invoice and the lines coming back off it, and each line is priced at what
    it was billed at. The request comes back `requested`: no stock has re-entered the
    warehouse and no credit has been given, because the goods are not back yet.

    A line can only be returned up to what was invoiced, less whatever is already on
    another open or approved return.
    """
    org_id = _org_id(user)
    invoice, customer = _resolve_parties(
        db, org_id, payload.invoice_reference_id, payload.customer_id
    )

    warehouse = stock_service.owned_warehouse(db, payload.warehouse_id, org_id)
    if payload.warehouse_id and warehouse is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="warehouse_id is not a warehouse in your firm",
        )

    if payload.return_number:
        taken = (
            db.query(SalesReturn)
            .filter(
                SalesReturn.organization_id == org_id,
                SalesReturn.return_number == payload.return_number,
            )
            .first()
        )
        if taken is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="A return with this number already exists",
            )

    sales_return = SalesReturn(
        organization_id=org_id,
        return_number=payload.return_number or numbering_service.next_number(
            db, org_id, SalesReturn.return_number, "RET"
        ),
        return_date=payload.return_date or datetime.now(timezone.utc),
        customer_id=customer.id if customer is not None else None,
        invoice_reference_id=invoice.id if invoice is not None else None,
        return_reason=payload.return_reason or payload.reason,
        return_type=payload.return_type or "Credit Note",
        return_status="requested",
        warehouse_id=warehouse.id if warehouse is not None else None,
        notes=payload.notes,
        created_by=user.id,
    )
    try:
        sales_return.items = return_service.build_items(db, org_id, payload.items, invoice)
    except ReturnError as exc:
        raise _bad_request(exc) from exc

    db.add(sales_return)
    db.commit()
    db.refresh(sales_return)
    return sales_return


@router.get("", response_model=list[SalesReturnOut])
def list_sales_returns(
    user: User = Depends(_view),
    status_filter: str | None = Query(default=None, alias="status"),
    customer_id: str | None = Query(default=None),
    invoice_id: str | None = Query(default=None, alias="invoice_reference_id"),
    db: Session = Depends(get_db),
) -> list[SalesReturn]:
    query = db.query(SalesReturn).filter(SalesReturn.organization_id == _org_id(user))
    if status_filter:
        query = query.filter(SalesReturn.return_status == status_filter)
    if customer_id:
        query = query.filter(SalesReturn.customer_id == customer_id)
    if invoice_id:
        query = query.filter(SalesReturn.invoice_reference_id == invoice_id)
    return query.order_by(SalesReturn.return_date.desc(), SalesReturn.id.desc()).all()


@router.get("/{id}", response_model=SalesReturnOut)
def get_sales_return_detail(
    id: str, user: User = Depends(_view), db: Session = Depends(get_db)
) -> SalesReturn:
    return _owned(db, id, _org_id(user))


@router.patch("/{id}", response_model=SalesReturnOut)
def update_sales_return(
    id: str,
    payload: SalesReturnUpdate,
    user: User = Depends(_edit),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> SalesReturn:
    """Correct a request while it is still open. A decided return cannot be edited —
    its stock and credit have already been acted on."""
    org_id = _org_id(user)
    sales_return = _owned(db, id, org_id)
    if sales_return.return_status not in workflow.OPEN_RETURN_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"A '{sales_return.return_status}' return can no longer be edited",
        )
    data = payload.model_dump(exclude_unset=True)

    if data.get("warehouse_id"):
        warehouse = stock_service.owned_warehouse(db, data["warehouse_id"], org_id)
        if warehouse is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="warehouse_id is not a warehouse in your firm",
            )

    if payload.items is not None:
        invoice = (
            db.get(Invoice, sales_return.invoice_reference_id)
            if sales_return.invoice_reference_id else None
        )
        try:
            sales_return.items = return_service.build_items(
                db, org_id, payload.items, invoice, return_id=sales_return.id
            )
        except ReturnError as exc:
            raise _bad_request(exc) from exc

    for field in ("return_reason", "return_date", "warehouse_id", "notes"):
        if field in data:
            setattr(sales_return, field, data[field])
    db.commit()
    db.refresh(sales_return)
    return sales_return


@router.patch("/{id}/receive", response_model=SalesReturnOut)
def receive_sales_return(
    id: str,
    payload: ReturnReceiveBody,
    user: User = Depends(_edit),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> SalesReturn:
    """The goods are physically back. Still no stock movement and no credit.

    Send what actually arrived per line — often less than the customer asked to return
    — and the condition it arrived in. A firm that checks goods at the door can skip
    this and go straight to approve, which takes the same figures.
    """
    org_id = _org_id(user)
    sales_return = _owned(db, id, org_id)
    try:
        return_service.receive(sales_return, payload.items)
    except ReturnError as exc:
        raise _bad_request(exc) from exc
    if payload.notes is not None:
        sales_return.notes = payload.notes
    db.commit()
    db.refresh(sales_return)
    return sales_return


@router.patch("/{id}/approve", response_model=SalesReturnOut)
def approve_sales_return(
    id: str,
    payload: ReturnApproveBody,
    user: User = Depends(_approve),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> SalesReturn:
    """Accept the return: restock what is saleable, then credit the customer.

    Per line, `received_quantity` is what came back, `condition` is what it is in, and
    `restock` is whether it goes back on the shelf. Only goods in **saleable**
    condition with `restock` true re-enter stock — asking to restock damaged or expired
    goods is refused, so a damaged return can never quietly become sellable again.

    The customer is credited for everything that came back, whatever its condition:
    whether to refund damaged goods is a commercial decision, not a stock one. That
    credit is a credit note of its own, raised against the original invoice, and the
    customer's outstanding balance falls by it. Send `credit_note: false` to accept the
    goods without crediting anything.
    """
    org_id = _org_id(user)
    sales_return = _owned(db, id, org_id)
    try:
        return_service.approve(
            db, org_id, sales_return, payload.items, user.id,
            warehouse_id=payload.warehouse_id, credit=payload.credit_note,
        )
    except ReturnError as exc:
        db.rollback()
        raise _bad_request(exc) from exc
    if payload.notes is not None:
        sales_return.notes = payload.notes
    db.commit()
    db.refresh(sales_return)
    return sales_return


@router.patch("/{id}/reject", response_model=SalesReturnOut)
def reject_sales_return(
    id: str,
    payload: ReturnRejectBody,
    user: User = Depends(_approve),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> SalesReturn:
    """Refuse a return. Nothing is restocked and nothing is credited."""
    org_id = _org_id(user)
    sales_return = _owned(db, id, org_id)
    try:
        return_service.reject(sales_return, payload.reason)
    except ReturnError as exc:
        raise _bad_request(exc) from exc
    db.commit()
    db.refresh(sales_return)
    return sales_return


@router.delete("/{id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_sales_return(
    id: str,
    user: User = Depends(_delete),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> None:
    """Withdraw a return request. Refused once it has been approved — stock has moved
    and a credit note has gone out; reverse those instead."""
    org_id = _org_id(user)
    sales_return = _owned(db, id, org_id)
    if sales_return.return_status == "approved":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An approved return cannot be deleted — it has restocked goods and a "
                   "credit note behind it",
        )
    db.delete(sales_return)
    db.commit()
