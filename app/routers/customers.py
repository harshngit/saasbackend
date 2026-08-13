from datetime import datetime, timezone

from fastapi import (
    APIRouter, Depends, File, Form, HTTPException, Query, Request, Response, UploadFile, status,
)
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core import scoping
from app.core.deps import require_permission, require_unlocked_org
from app.core.pdf_docs import payment_receipt_pdf
from app.core.files import save_upload
from app.models import Customer, CustomerDocument, CustomerPayment, Invoice, StoredFile, User
from app.services import (
    customer_profile_service,
    ledger_service,
    lookup_service,
    numbering_service,
    payment_service,
)
from app.services.customer_profile_service import DOCUMENT_TYPES, OTHER_DOCUMENT_TYPE
from app.schemas.customer_profile import CustomerProfileIn, CustomerProfileOut
from app.schemas.ledger import CustomerLedger
from app.schemas.customer import (
    CustomerCreate,
    CustomerDocumentOut,
    CustomerOut,
    CustomerPaymentCreate,
    CustomerPaymentOut,
    CustomerUpdate,
)

router = APIRouter(prefix="/customers", tags=["customers"])

# Permission gates (admin/super_admin bypass; staff checked against their role matrix).
_view = require_permission("customers", "view")
_create = require_permission("customers", "create")
_edit = require_permission("customers", "edit")
_delete = require_permission("customers", "delete")
_pay_view = require_permission("payments", "view")
_pay_create = require_permission("payments", "create")
_pay_delete = require_permission("payments", "delete")


def _org_id(user: User) -> str:
    if not user.organization_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No organization on this account")
    return user.organization_id


def _owned_customer(db: Session, customer_id: str, user: User) -> Customer:
    """The customer, if this user may see it. Accepts the UUID or the customer code.

    Out of scope reads as "not found" rather than "forbidden": a field role should
    not be able to probe which customer ids exist in the firm."""
    record = lookup_service.by_id_or_code(
        db, Customer, customer_id, _org_id(user), Customer.customer_id
    )
    if record is None or not scoping.owns_record(db, user, record, "assigned_sales_officer_id"):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Customer not found")
    return record


def _owned_invoice(
    db: Session, invoice_id: str | None, customer: Customer, org_id: str
) -> Invoice | None:
    """The invoice a payment settles — it must belong to this firm and this customer."""
    if not invoice_id:
        return None
    invoice = db.get(Invoice, invoice_id)
    if invoice is None or invoice.organization_id != org_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Invoice not found")
    if invoice.customer_id != customer.id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="That invoice belongs to a different customer",
        )
    return invoice




def _attach_documents(db: Session, customer: Customer, section) -> None:
    """Attach uploaded files named in the payload's `documents` block."""
    if section is None:
        return
    try:
        customer_profile_service.attach_documents(db, customer, section)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


def _validate_assignee(db: Session, org_id: str, sales_officer_id: str | None) -> None:
    if sales_officer_id is None:
        return
    officer = db.get(User, sales_officer_id)
    if officer is None or officer.organization_id != org_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="assigned_sales_officer_id is not a user in your firm",
        )


@router.post("", response_model=CustomerProfileOut, status_code=status.HTTP_201_CREATED)
def create_customer(
    payload: CustomerProfileIn,
    user: User = Depends(_create),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> CustomerProfileOut:
    """Create a customer from the sectioned profile body.

    A flat body is still accepted — any top-level field that belongs to a
    section is folded into it, so callers written against the older shape keep
    working. `customer_id` is an Auto Number and is issued here, not sent in;
    the `documents` section is filled by uploading, not by writing ids.
    """
    org_id = _org_id(user)
    data = payload.to_columns()
    _validate_assignee(db, org_id, data.get("assigned_sales_officer_id"))
    if not data.get("name"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="basic_information.customer_name is required",
        )
    data["customer_id"] = numbering_service.next_number(
        db, org_id, Customer.customer_id, "CUST"
    )
    # A field role only sees its own customers, so default the sales rep to whoever
    # is creating it — otherwise they could not see the customer they just added.
    if not data.get("assigned_sales_officer_id") and scoping.scope_to_own(db, user):
        data["assigned_sales_officer_id"] = user.id
    customer = Customer(organization_id=org_id, **data)
    customer.recompute_outstanding()  # opening_balance -> outstanding
    db.add(customer)
    db.flush()
    _attach_documents(db, customer, payload.documents)
    db.commit()
    db.refresh(customer)
    return customer_profile_service.build_profile(db, customer)


@router.get("", response_model=list[CustomerOut])
def list_customers(
    user: User = Depends(_view),
    search: str | None = Query(default=None, description="matches customer code / name / business / phone / email"),
    category: str | None = Query(default=None),
    is_active: bool | None = Query(default=None),
    assigned_sales_officer_id: str | None = Query(default=None),
    db: Session = Depends(get_db),
) -> list[Customer]:
    org_id = _org_id(user)
    query = db.query(Customer).filter(Customer.organization_id == org_id)
    if search:
        like = f"%{search}%"
        query = query.filter(
            or_(
                Customer.name.ilike(like),
                Customer.business_name.ilike(like),
                Customer.phone.ilike(like),
                Customer.email.ilike(like),
                Customer.customer_id.ilike(like),
            )
        )
    if category is not None:
        query = query.filter(Customer.category == category)
    if is_active is not None:
        query = query.filter(Customer.is_active == is_active)
    if assigned_sales_officer_id is not None:
        query = query.filter(Customer.assigned_sales_officer_id == assigned_sales_officer_id)
    query = scoping.owned_by(query, db, user, Customer.assigned_sales_officer_id)
    return query.order_by(Customer.created_at.desc()).all()


@router.get("/{customer_id}", response_model=CustomerProfileOut)
def get_customer(
    customer_id: str, user: User = Depends(_view), db: Session = Depends(get_db)
) -> CustomerProfileOut:
    """The full 360° customer profile: every section, the uploaded documents, and
    the financial and sales summaries. Accepts the UUID or the customer code."""
    customer = _owned_customer(db, customer_id, user)
    return customer_profile_service.build_profile(db, customer)


@router.patch("/{customer_id}", response_model=CustomerProfileOut)
def update_customer(
    customer_id: str,
    payload: CustomerProfileIn,
    user: User = Depends(_edit),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> CustomerProfileOut:
    """Partial update. Only the sections you send are touched, and within a
    section only the fields you send. A flat body works too."""
    org_id = _org_id(user)
    customer = _owned_customer(db, customer_id, user)
    data = payload.to_columns()
    if "assigned_sales_officer_id" in data:
        _validate_assignee(db, org_id, data["assigned_sales_officer_id"])
    for field, value in data.items():
        setattr(customer, field, value)
    if "opening_balance" in data:
        customer.recompute_outstanding()
    _attach_documents(db, customer, payload.documents)
    db.commit()
    db.refresh(customer)
    return customer_profile_service.build_profile(db, customer)


@router.post("/{customer_id}/payments", response_model=CustomerOut, status_code=status.HTTP_201_CREATED)
def record_customer_payment(
    customer_id: str,
    payload: CustomerPaymentCreate,
    user: User = Depends(_pay_create),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> Customer:
    """Record a payment received from a customer. Returns the customer with updated balances.

    Pass `invoice_id` to settle a specific invoice — that invoice's `amount_paid`
    and `status` move with it, and more than the invoice still owes is refused. Omit it
    and the payment is an advance that only reduces the customer's outstanding balance.

    Every payment is receipted: the row carries its own RCPT number, and the same
    payment appears under GET /payment-receipts and in the customer's ledger."""
    org_id = _org_id(user)
    customer = _owned_customer(db, customer_id, user)
    invoice = _owned_invoice(db, payload.invoice_id, customer, org_id)
    try:
        payment_service.record(
            db, org_id,
            customer=customer,
            invoice=invoice,
            amount=payload.amount,
            payment_mode=payload.payment_mode,
            reference=payload.reference,
            note=payload.note,
            received_on=payload.received_on,
            order_id=payload.order_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    db.commit()
    db.refresh(customer)
    return customer


@router.get("/{customer_id}/ledger", response_model=CustomerLedger)
def customer_ledger(
    customer_id: str, user: User = Depends(_pay_view), db: Session = Depends(get_db)
) -> CustomerLedger:
    """The customer's account: what they owe, how old it is, and every entry behind it.

    `summary` is the standing position, including the credit still available against
    their limit. `ageing` buckets the unpaid invoices by how long they have been
    outstanding — from each invoice's due date where it has one, from its date where it
    does not. `transactions` is the account itself, oldest first, with a running
    balance: an invoice debits, a payment or a credit note credits.

    The receivable starts at the invoice, never at the order — an order is a promise, an
    invoice is a bill — so an unbilled order appears nowhere here.
    """
    customer = _owned_customer(db, customer_id, user)
    return ledger_service.build(db, customer)


@router.get("/{customer_id}/payments", response_model=list[CustomerPaymentOut])
def list_customer_payments(
    customer_id: str, user: User = Depends(_pay_view), db: Session = Depends(get_db)
) -> list[CustomerPayment]:
    _owned_customer(db, customer_id, user)
    return (
        db.query(CustomerPayment)
        .filter(CustomerPayment.customer_id == customer_id)
        .order_by(CustomerPayment.received_on.desc(), CustomerPayment.id.desc())
        .all()
    )


@router.get("/{customer_id}/payments/receipt/{payment_id}")
def payment_receipt(
    customer_id: str,
    payment_id: str,
    user: User = Depends(_pay_view),
    db: Session = Depends(get_db),
) -> Response:
    """Download a PDF receipt for a customer payment."""
    org_id = _org_id(user)
    customer = _owned_customer(db, customer_id, user)
    payment = db.get(CustomerPayment, payment_id)
    if payment is None or payment.customer_id != customer_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Payment not found")
    pdf = payment_receipt_pdf(user.organization, customer, payment)
    return Response(
        content=pdf, media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="receipt-{payment_id[:8]}.pdf"'},
    )


@router.delete("/{customer_id}/payments/{payment_id}", response_model=CustomerOut)
def void_customer_payment(
    customer_id: str,
    payment_id: str,
    user: User = Depends(_pay_delete),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> Customer:
    org_id = _org_id(user)
    customer = _owned_customer(db, customer_id, user)
    payment = db.get(CustomerPayment, payment_id)
    if payment is None or payment.customer_id != customer_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Payment not found")
    # Undo whatever this payment did: the invoice it settled, or the advance balance.
    payment_service.void(db, payment)
    db.commit()
    db.refresh(customer)
    return customer


@router.delete("/{customer_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_customer(
    customer_id: str,
    user: User = Depends(_delete),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> None:
    customer = _owned_customer(db, customer_id, user)
    db.delete(customer)
    db.commit()


# ------------------------------ Customer documents ------------------------------
# A row per file, so the named slots (GST certificate, PAN card, …) and the
# many-file "other" slot behave identically. No base64 anywhere: the bytes go to
# stored_files and the record keeps a URL.

@router.post(
    "/{customer_id}/documents",
    response_model=CustomerDocumentOut,
    status_code=status.HTTP_201_CREATED,
)
def upload_customer_document(
    customer_id: str,
    request: Request,
    document_type: str = Form(..., description=" | ".join(DOCUMENT_TYPES)),
    file: UploadFile = File(...),
    user: User = Depends(_edit),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> CustomerDocument:
    """Upload one document against a customer.

    A named type replaces whatever was in that slot; `other` appends, so the
    customer can carry many miscellaneous files."""
    org_id = _org_id(user)
    customer = _owned_customer(db, customer_id, user)
    kind = document_type.strip().lower().replace(" ", "_").replace("-", "_")
    if kind not in DOCUMENT_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"document_type must be one of: {', '.join(DOCUMENT_TYPES)}",
        )

    url, size = save_upload(db, org_id, file, request, allow_any=True)
    if kind != OTHER_DOCUMENT_TYPE:
        # Named slots hold one file — drop the previous one rather than leaving
        # two rows claiming to be "the" GST certificate.
        for existing in [d for d in customer.documents if d.document_type == kind]:
            db.delete(existing)

    document = CustomerDocument(
        # One id everywhere: the stored file's id is the document's id.
        id=url.rsplit("/", 1)[-1],
        customer_id=customer.id,
        organization_id=org_id,
        document_type=kind,
        name=file.filename or "document",
        content_type=file.content_type or "application/octet-stream",
        size=size,
        url=url,
        file_id=url.rsplit("/", 1)[-1],
    )
    db.add(document)
    db.commit()
    db.refresh(document)
    return document


@router.post(
    "/{customer_id}/documents/other",
    response_model=list[CustomerDocumentOut],
    status_code=status.HTTP_201_CREATED,
)
def upload_other_customer_documents(
    customer_id: str,
    request: Request,
    files: list[UploadFile] = File(..., description="One or more files"),
    user: User = Depends(_edit),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> list[CustomerDocument]:
    """Upload several "other" documents in one call. Appends to what is already
    on file and returns every other-document the customer now has."""
    org_id = _org_id(user)
    customer = _owned_customer(db, customer_id, user)
    for upload in files:
        url, size = save_upload(db, org_id, upload, request, allow_any=True)
        db.add(CustomerDocument(
            id=url.rsplit("/", 1)[-1],
            customer_id=customer.id,
            organization_id=org_id,
            document_type=OTHER_DOCUMENT_TYPE,
            name=upload.filename or "document",
            content_type=upload.content_type or "application/octet-stream",
            size=size,
            url=url,
            file_id=url.rsplit("/", 1)[-1],
        ))
    db.commit()
    db.refresh(customer)
    return [d for d in customer.documents if d.document_type == OTHER_DOCUMENT_TYPE]


@router.get("/{customer_id}/documents", response_model=list[CustomerDocumentOut])
def list_customer_documents(
    customer_id: str,
    document_type: str | None = Query(default=None, description="Filter to one type"),
    user: User = Depends(_view),
    db: Session = Depends(get_db),
) -> list[CustomerDocument]:
    """Every document on file for this customer."""
    customer = _owned_customer(db, customer_id, user)
    documents = sorted(customer.documents or [], key=lambda d: d.uploaded_at)
    if document_type:
        documents = [d for d in documents if d.document_type == document_type]
    return documents


def _owned_document(db: Session, customer, document_id: str) -> CustomerDocument:
    for document in customer.documents or []:
        if document.id == document_id:
            return document
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")


@router.get("/{customer_id}/documents/{document_id}/download")
def download_customer_document(
    customer_id: str,
    document_id: str,
    user: User = Depends(_view),
    db: Session = Depends(get_db),
) -> Response:
    """Download the file itself, with its original filename."""
    customer = _owned_customer(db, customer_id, user)
    document = _owned_document(db, customer, document_id)
    stored = db.get(StoredFile, document.file_id) if document.file_id else None
    if stored is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File no longer stored")
    return Response(
        content=stored.data,
        media_type=document.content_type,
        headers={"Content-Disposition": f'attachment; filename="{document.name}"'},
    )


@router.delete("/{customer_id}/documents/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_customer_document(
    customer_id: str,
    document_id: str,
    user: User = Depends(_edit),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> None:
    customer = _owned_customer(db, customer_id, user)
    db.delete(_owned_document(db, customer, document_id))
    db.commit()
