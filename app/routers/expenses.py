from datetime import datetime, timedelta, timezone

from fastapi import (
    APIRouter, BackgroundTasks, Depends, File, Header, HTTPException, Query, Request, Response, UploadFile, status,
)
from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core import scoping
from app.core.database import get_db
from app.core.deps import require_entitlement, require_permission, require_unlocked_org
from app.core.files import delete_replaced_file, save_upload
from app.core.sorting import apply_sorting
from app.models import EXPENSE_CATEGORIES, Expense, ExpenseItem, User
from app.core.pagination import paginate
from app.schemas.pagination import PaginatedResponse
from app.schemas.expense import (
    ApproveBody,
    BulkDelete,
    BulkDeleteResult,
    ExpenseCreate,
    ExpenseDetailOut,
    ExpenseHistoryEntry,
    ExpenseOut,
    ExpenseUpdate,
    RejectBody,
    ReimburseBody,
)
from app.core import notification_types as nt
from app.services import expense_service, lookup_service, notification_service, numbering_service

router = APIRouter(
    prefix="/expenses",
    tags=["expenses"],
    dependencies=[Depends(require_entitlement("finance.expenses"))],
)

_view = require_permission("expenses", "view")
_create = require_permission("expenses", "create")
_edit = require_permission("expenses", "edit")
_approve = require_permission("expenses", "approve")
_delete = require_permission("expenses", "delete")

EXPENSE_SORTABLE_FIELDS = {
    "expense_date": Expense.expense_date,
    "date": Expense.expense_date,
    "category": Expense.category,
    "vendor": Expense.payee_name,
    "payee": Expense.payee_name,
    "payee_name": Expense.payee_name,
    "amount": Expense.amount,
    "payment_method": Expense.payment_mode,
    "payment_mode": Expense.payment_mode,
    "status": Expense.status,
    "expense_status": Expense.expense_status,
    "approval_status": Expense.approval_status,
    "payment_status": Expense.payment_status,
    "expense_number": Expense.expense_number,
    "expense_id": Expense.expense_id,
    "created_at": Expense.created_at,
}


def _org_id(user: User) -> str:
    if not user.organization_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No organization on this account")
    return user.organization_id


def _owned(db: Session, expense_id: str, org_id: str, user: User, *, enforce_scope: bool = True) -> Expense:
    """Accepts the UUID or the human-facing code (expense_number, expense_id).

    An "own"-scope user (a custom role with data_scope=="own" submitting
    their own expenses) may only reach an Expense they submitted — the same
    dynamic scope check every other module uses. `enforce_scope=False` is
    used only by the reviewer actions (approve/reject/request-clarification),
    which are gated by the separate `expenses:approve` permission and must
    keep working across the whole organization's expenses regardless of the
    reviewer's own data scope — an own-scoped check there would make
    approving anyone else's expense impossible, which is not what this fixes.
    """
    record = lookup_service.by_id_or_code(
        db, Expense, expense_id, org_id, Expense.expense_number, Expense.expense_id
    )
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Expense not found")
    # team_attributes=(): Expenses are explicitly excluded from Team Scope.
    if enforce_scope and not scoping.owns_record(db, user, record, "submitted_by", team_attributes=()):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Expense not found")
    return record


@router.get("/categories")
def expense_categories(_user: User = Depends(_view)) -> dict:
    """Suggested expense categories for the frontend dropdown."""
    return {"categories": EXPENSE_CATEGORIES}


@router.post("", response_model=ExpenseOut, status_code=status.HTTP_201_CREATED)
def create_expense(
    payload: ExpenseCreate,
    response: Response,
    background: BackgroundTasks,
    idempotency_key: str | None = Header(
        default=None, alias="Idempotency-Key", max_length=100,
        description="Optional. Send the same key when retrying a create (e.g. after a timeout): the "
                    "expense the first attempt made is returned instead of a duplicate.",
    ),
    user: User = Depends(_create),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> Expense:
    org_id = _org_id(user)
    key = (idempotency_key or "").strip() or None
    if key:
        earlier = _by_idempotency_key(db, org_id, user.id, key)
        if earlier is not None:
            return _replay(earlier, payload, response)
    expense_service.validate_attachment_refs(
        db, org_id, payload.receipt_url, payload.vendor_invoice_url,
        *expense_service.supporting_refs(payload.supporting_documents),
    )
    # Admin-created expenses are approved on creation (approved = accepted as valid).
    # They are reimbursed on creation only when the Admin explicitly says it was
    # already paid (payment_status "Paid") with a canonical payment method.
    # Everyone else's expense starts pending approval, whatever lifecycle values
    # the client sent.
    creator_is_admin = expense_service.is_admin(user)
    already_paid = creator_is_admin and (payload.payment_status or "").strip().lower() == "paid"
    paid_method = expense_service.normalize_reimbursement_method(payload.payment_mode) if already_paid else None

    # Validate vendor if provided and auto-populate payee info if not manually set
    vendor = expense_service.validate_vendor(db, org_id, payload.vendor_id)
    payee_name = payload.payee_name or (vendor.name if vendor else None)
    contact_person = payload.contact_person or (vendor.contact_person if vendor else None)
    mobile_number = payload.mobile_number or (vendor.phone if vendor else None)
    email_address = payload.email_address or (vendor.email if vendor else None)
    payee_gstin = payload.payee_gstin or (vendor.gst_number if vendor else None)

    # Server-side calculation of subtotals, taxes, and amounts
    subtotal, tax_amount, total_amount, calculated_items = expense_service.calculate_totals(
        items_in=payload.items,
        header_tax_rate=payload.tax_rate,
        header_tax_amount=payload.tax_amount,
        header_amount=payload.amount,
    )

    tds_amount = round(payload.tds_amount or 0.0, 2) if payload.tds_applicable else 0.0
    if tds_amount > total_amount:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="TDS amount cannot exceed the total expense amount",
        )

    # Compute initial next_due_date if recurring and next_due_date is not explicitly provided
    expense_date = payload.expense_date or datetime.now(timezone.utc)
    next_due_date = payload.next_due_date
    if payload.is_recurring and not next_due_date:
        next_due_date = expense_service.compute_next_due_date(expense_date, payload.recurrence_frequency)

    expense = Expense(
        organization_id=org_id,
        submitted_by=user.id,
        category=payload.category,
        amount=total_amount,
        subtotal=subtotal,
        tax_rate=payload.tax_rate,
        tax_amount=tax_amount,
        currency=payload.currency or "INR",
        description=payload.description,
        expense_date=expense_date,
        payment_mode=payload.payment_mode,
        receipt_url=payload.receipt_url,
        vendor_invoice_url=payload.vendor_invoice_url,
        supporting_documents=payload.supporting_documents or [],
        status="pending",
        # Auto-generated IDs
        expense_id=numbering_service.next_number(db, org_id, Expense.expense_id, "EXPID"),
        expense_number=numbering_service.next_number(db, org_id, Expense.expense_number, "EXP"),
        expense_type=payload.expense_type,
        expense_status="Submitted",
        financial_year=payload.financial_year,
        branch_id=payload.branch_id,
        department_id=payload.department_id,
        vendor_id=payload.vendor_id,
        payee_name=payee_name,
        contact_person=contact_person,
        mobile_number=mobile_number,
        email_address=email_address,
        payee_gstin=payee_gstin,
        payment_reference=payload.payment_reference,
        payment_status="Pending",
        approval_status="Pending",
        paid_from_account_id=payload.paid_from_account_id,
        expense_account_id=payload.expense_account_id,
        cost_center_id=payload.cost_center_id,
        project_id=payload.project_id,
        tax_category=payload.tax_category,
        tds_applicable=payload.tds_applicable,
        tds_amount=tds_amount,
        tags=payload.tags or [],
        is_recurring=payload.is_recurring,
        recurrence_frequency=payload.recurrence_frequency,
        next_due_date=next_due_date,
        idempotency_key=key,
        # Set here, not at the INSERT, so an auto-approval below is never "before" it.
        created_at=datetime.now(timezone.utc),
    )

    for item_data in calculated_items:
        expense.items.append(ExpenseItem(**item_data))

    # An Admin's expense is written already approved (and paid) in the one INSERT,
    # instead of an INSERT followed by an UPDATE per step.
    approval_note = "Created by Admin (auto-approved)"
    paid_note = "Recorded as already paid at creation"
    if creator_is_admin:
        expense_service.apply_approval(expense, user, approval_note)
        if already_paid:
            expense_service.apply_reimbursement(
                db, expense, user, paid_method, payload.payment_reference, paid_note, None)

    db.add(expense)
    if key:
        try:
            with db.begin_nested():
                db.flush()
        except IntegrityError:
            # The same key won a concurrent attempt a moment ago: answer with that one.
            db.rollback()
            earlier = _by_idempotency_key(db, org_id, user.id, key)
            if earlier is None:
                raise
            return _replay(earlier, payload, response)
    else:
        db.flush()
    # Recorded in the same instant, so each step is stamped a microsecond after the one
    # before it: the history then always reads created < approved < reimbursed.
    steps = [expense_service.record_history(db, expense, user, "created")]
    if creator_is_admin:
        steps.append(expense_service.record_history(db, expense, user, "approved", approval_note))
        if already_paid:
            steps.append(expense_service.record_history(db, expense, user, "reimbursed", paid_note))
    for n, entry in enumerate(steps):
        entry.created_at = expense.created_at + timedelta(microseconds=n)
    if creator_is_admin:
        expense_service._post_gl(db, expense, "expense")
    db.commit()
    db.refresh(expense)
    # The notifications (one row per recipient, with its duplicate check, realtime event
    # and push) are written after the response has been sent, in their own session:
    # they cost a database round trip or more per recipient and the caller does not
    # wait for them. Same rows and content as before.
    background.add_task(_notify_expense_created, expense.id, user.id, creator_is_admin)
    return expense


def _notify_expense_created(expense_id: str, actor_id: str, creator_is_admin: bool) -> None:
    from app.core.database import SessionLocal

    db = SessionLocal()
    try:
        expense = db.get(Expense, expense_id)
        if expense is None:
            return
        if creator_is_admin:
            notification_service.notify_expense_status_changed(db, expense, actor_id=actor_id)
        else:
            notification_service.notify_org_admins(
                db, expense.organization_id, "New expense submitted",
                f"{expense.category}: Rs {expense.amount:,.2f}", type="expense", link=expense.id,
                data=nt.build_data(nt.EXPENSE_CREATED, expense_id=expense.id))
        db.commit()
    except Exception:  # noqa: BLE001 -- the expense is saved; a notification failure must not surface
        db.rollback()
        import logging

        logging.getLogger("crm.expenses").exception("Expense %s: notifications failed", expense_id)
    finally:
        db.close()


def _by_idempotency_key(db: Session, org_id: str, user_id: str, key: str) -> Expense | None:
    return (
        db.query(Expense)
        .filter(Expense.organization_id == org_id, Expense.submitted_by == user_id, Expense.idempotency_key == key)
        .first()
    )


def _replay(earlier: Expense, payload: ExpenseCreate, response: Response) -> Expense:
    """A retried create: the expense the first attempt made, unchanged. A key reused for
    a different expense (another category or amount) is refused rather than guessed at."""
    _, _, total_amount, _ = expense_service.calculate_totals(
        items_in=payload.items, header_tax_rate=payload.tax_rate,
        header_tax_amount=payload.tax_amount, header_amount=payload.amount,
    )
    if earlier.category != payload.category or abs((earlier.amount or 0) - total_amount) > 0.005:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "IDEMPOTENCY_KEY_REUSED",
                "message": "This Idempotency-Key was already used for a different expense. "
                           "Use a new key for a new expense.",
                "expense_id": earlier.id,
            },
        )
    response.headers["Idempotent-Replayed"] = "true"
    return earlier


@router.post("/{expense_id}/receipt", response_model=ExpenseOut)
def upload_receipt(
    request: Request,
    expense_id: str,
    file: UploadFile = File(...),
    user: User = Depends(_edit),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> Expense:
    """Attach a bill/receipt (image or PDF, max 10 MB) to an expense."""
    expense = _owned(db, expense_id, _org_id(user), user)
    old_receipt = expense.receipt_url
    expense.receipt_url, _ = save_upload(db, expense.organization_id, file, request)
    new_receipt, org_id = expense.receipt_url, expense.organization_id
    db.commit()
    # Only after the commit, and only if it is this firm's file and nothing else uses it.
    if old_receipt and old_receipt != new_receipt:
        delete_replaced_file(db, old_receipt, org_id)
    db.refresh(expense)
    return expense


@router.get("", response_model=PaginatedResponse[ExpenseOut])
def list_expenses(
    user: User = Depends(_view),
    category: str | None = Query(default=None),
    status_filter: str | None = Query(default=None, alias="status"),
    lifecycle_status: str | None = Query(
        default=None, description="pending_approval | approved | rejected | reimbursed (approved = not yet reimbursed)"),
    submitted_by: str | None = Query(default=None),
    vendor_id: str | None = Query(default=None),
    branch_id: str | None = Query(default=None),
    department_id: str | None = Query(default=None),
    cost_center_id: str | None = Query(default=None),
    project_id: str | None = Query(default=None),
    payment_status: str | None = Query(default=None),
    is_recurring: bool | None = Query(default=None),
    tag: str | None = Query(default=None),
    search: str | None = Query(default=None, description="matches expense_number, expense_id, description, payee_name"),
    page: int = Query(default=1, ge=1, description="Page number (1-indexed)"),
    page_size: int = Query(default=10, ge=1, le=100, description="Items per page (default 10)"),
    sort_by: str | None = Query(default=None, description="Field to sort by"),
    sort_order: str = Query(default="asc", description="Sort order: asc or desc"),
    db: Session = Depends(get_db),
) -> PaginatedResponse[ExpenseOut]:
    org_id = _org_id(user)
    q = db.query(Expense).filter(Expense.organization_id == org_id)
    if category:
        q = q.filter(Expense.category == category)
    if status_filter == "pending_approval":
        q = q.filter(Expense.status.in_(expense_service.PENDING_STATUSES))
    elif status_filter == "reimbursed":
        q = q.filter(Expense.status == "approved", Expense.reimbursed_at.isnot(None))
    elif status_filter:
        q = q.filter(Expense.status == status_filter)
    if lifecycle_status:
        if lifecycle_status == "pending_approval":
            q = q.filter(Expense.status.in_(expense_service.PENDING_STATUSES))
        elif lifecycle_status == "approved":
            q = q.filter(Expense.status == "approved", Expense.reimbursed_at.is_(None))
        elif lifecycle_status == "reimbursed":
            q = q.filter(Expense.status == "approved", Expense.reimbursed_at.isnot(None))
        elif lifecycle_status == "rejected":
            q = q.filter(Expense.status == "rejected")
        else:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="lifecycle_status must be one of pending_approval, approved, rejected, reimbursed",
            )
    if submitted_by:
        q = q.filter(Expense.submitted_by == submitted_by)
    if vendor_id:
        q = q.filter(Expense.vendor_id == vendor_id)
    if branch_id:
        q = q.filter(Expense.branch_id == branch_id)
    if department_id:
        q = q.filter(Expense.department_id == department_id)
    if cost_center_id:
        q = q.filter(Expense.cost_center_id == cost_center_id)
    if project_id:
        q = q.filter(Expense.project_id == project_id)
    if payment_status:
        q = q.filter(Expense.payment_status == payment_status)
    if is_recurring is not None:
        q = q.filter(Expense.is_recurring.is_(is_recurring))
    if tag:
        # Check tag in JSON list
        q = q.filter(Expense.tags.contains([tag]))
    if search:
        s = f"%{search}%"
        q = q.filter(
            or_(
                Expense.expense_number.ilike(s),
                Expense.expense_id.ilike(s),
                Expense.description.ilike(s),
                Expense.payee_name.ilike(s),
            )
        )

    q = scoping.owned_by(q, db, user, Expense.submitted_by, team_columns=())
    q = apply_sorting(
        q,
        sort_by=sort_by,
        sort_order=sort_order,
        sortable_fields=EXPENSE_SORTABLE_FIELDS,
        default_order=[Expense.expense_date.desc(), Expense.id.desc()],
        secondary_key=Expense.id.desc(),
    )
    items, total, page, page_size, total_pages = paginate(q, page=page, page_size=page_size)
    expense_service.preload_user_names(db, items)  # submitted / approved / reimbursed by: one query
    return PaginatedResponse(
        items=[ExpenseOut.model_validate(e) for e in items],
        total=total,
        page=page,
        page_size=page_size,
        total_pages=total_pages,
    )


def _detail(db: Session, expense: Expense) -> ExpenseDetailOut:
    out = ExpenseDetailOut.model_validate(expense)
    out.attachments = expense_service.attachments(db, expense)
    out.history = [ExpenseHistoryEntry(**e) for e in expense_service.history(db, expense)]
    return out


@router.get("/{expense_id}", response_model=ExpenseDetailOut)
def get_expense(expense_id: str, user: User = Depends(_view), db: Session = Depends(get_db)) -> ExpenseDetailOut:
    """Expense detail, with its proof attachments (file metadata) and lifecycle history."""
    return _detail(db, _owned(db, expense_id, _org_id(user), user))


@router.get("/{expense_id}/history", response_model=list[ExpenseHistoryEntry])
def get_expense_history(expense_id: str, user: User = Depends(_view), db: Session = Depends(get_db)) -> list[dict]:
    """Created / Approved / Rejected / Reimbursed trail: actor, time, note."""
    return expense_service.history(db, _owned(db, expense_id, _org_id(user), user))


@router.patch("/{expense_id}", response_model=ExpenseOut)
def update_expense(
    expense_id: str,
    payload: ExpenseUpdate,
    user: User = Depends(_edit),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> Expense:
    org_id = _org_id(user)
    expense = _owned(db, expense_id, org_id, user)
    if expense.status not in ("pending", "clarification_requested"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Only pending expenses can be edited")

    update_data = payload.model_dump(exclude_unset=True)

    # The submitted amount (and the lifecycle columns) are fixed once the expense
    # is in the approval workflow, i.e. from creation. A wrong amount is rejected
    # and raised again; it is never edited under the approver.
    locked = expense_service.lock_violations(expense, update_data)
    if locked:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "EXPENSE_AMOUNT_LOCKED",
                "message": "The amount and approval state of a submitted expense cannot be edited. "
                           "Reject it and raise a new expense with the correct amount.",
                "fields": locked,
            },
        )
    for field in expense_service.AMOUNT_FIELDS + expense_service.LIFECYCLE_FIELDS:
        update_data.pop(field, None)
    expense_service.validate_attachment_refs(
        db, org_id, update_data.get("receipt_url"), update_data.get("vendor_invoice_url"),
        *expense_service.supporting_refs(update_data.get("supporting_documents")),
    )

    # Validate vendor if vendor_id is updated
    if "vendor_id" in update_data:
        expense_service.validate_vendor(db, org_id, update_data["vendor_id"])

    for field, value in update_data.items():
        if field not in ("amount", "subtotal", "tax_amount"):
            setattr(expense, field, value)

    if expense.status == "clarification_requested":
        expense.status = "pending"  # resubmitted for approval
        expense_service.record_history(db, expense, user, "resubmitted")

    db.commit()
    db.refresh(expense)
    return expense


@router.post("/{expense_id}/approve", response_model=ExpenseOut)
@router.patch("/{expense_id}/approve", response_model=ExpenseOut)
def approve_expense(
    expense_id: str,
    payload: ApproveBody | None = None,
    user: User = Depends(_approve),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> Expense:
    """pending_approval -> approved. Records approved_by / approved_at / note and posts
    the expense to the ledger (accrued to Payable until reimbursed). A reviewer cannot
    approve their own expense."""
    # Reviewer action, gated by the separate expenses:approve permission —
    # deliberately org-wide regardless of the reviewer's own data scope.
    expense = _owned(db, expense_id, _org_id(user), user, enforce_scope=False)
    expense_service.approve(db, expense, user, payload.note if payload else None)
    notification_service.notify_expense_status_changed(db, expense, actor_id=user.id)
    db.commit()
    db.refresh(expense)
    return expense


@router.post("/{expense_id}/reject", response_model=ExpenseOut)
@router.patch("/{expense_id}/reject", response_model=ExpenseOut)
def reject_expense(
    expense_id: str,
    payload: RejectBody,
    user: User = Depends(_approve),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> Expense:
    """pending_approval -> rejected. `reason` is required. A rejected expense can never
    be reimbursed."""
    # Reviewer action, gated by the separate expenses:approve permission —
    # deliberately org-wide regardless of the reviewer's own data scope.
    expense = _owned(db, expense_id, _org_id(user), user, enforce_scope=False)
    expense_service.reject(db, expense, user, payload.reason or payload.note)
    notification_service.notify_expense_status_changed(db, expense, actor_id=user.id)
    db.commit()
    db.refresh(expense)
    return expense


@router.post("/{expense_id}/reimburse", response_model=ExpenseOut)
@router.patch("/{expense_id}/reimburse", response_model=ExpenseOut)
def reimburse_expense(
    expense_id: str,
    payload: ReimburseBody,
    user: User = Depends(_approve),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> Expense:
    """approved -> reimbursed (full amount only). Records reimbursed_by / reimbursed_at /
    payment_method / payment_reference / notes and posts the payment to the ledger.
    A second call is refused with 409; rejected or pending expenses with 400."""
    expense = _owned(db, expense_id, _org_id(user), user, enforce_scope=False)
    expense_service.reimburse(
        db, expense, user,
        payment_method=payload.payment_method, amount=payload.amount,
        payment_reference=payload.payment_reference, notes=payload.notes, paid_at=payload.paid_at,
    )
    notification_service.notify_expense_status_changed(db, expense, actor_id=user.id)
    db.commit()
    db.refresh(expense)
    return expense


@router.patch("/{expense_id}/request-clarification", response_model=ExpenseOut)
def request_clarification(
    expense_id: str,
    payload: RejectBody,
    user: User = Depends(_approve),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> Expense:
    """Ask the submitter for more info (keeps it out of approved/rejected)."""
    # Reviewer action, gated by the separate expenses:approve permission —
    # deliberately org-wide regardless of the reviewer's own data scope.
    expense = _owned(db, expense_id, _org_id(user), user, enforce_scope=False)
    if expense.status not in ("pending", "clarification_requested"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Only pending expenses need clarification")
    expense.status = "clarification_requested"
    expense.reject_reason = payload.reason  # reused as the clarification note
    expense_service.record_history(db, expense, user, "clarification_requested", payload.reason)
    if expense.submitted_by:
        notification_service.notify(
            db, expense.submitted_by, "Clarification requested on your expense",
            payload.reason, type="expense", link=expense.id, organization_id=expense.organization_id,
            data=nt.build_data(nt.EXPENSE_CLARIFICATION_REQUESTED, expense_id=expense.id))
    db.commit()
    db.refresh(expense)
    return expense


@router.post("/recurring/process", response_model=list[ExpenseOut])
def process_recurring(
    user: User = Depends(_create),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> list[Expense]:
    """Process all due recurring expenses for the organization."""
    org_id = _org_id(user)
    return expense_service.process_due_recurring_expenses(db, org_id)


@router.post("/bulk-delete", response_model=BulkDeleteResult)
@router.delete("/bulk-delete", response_model=BulkDeleteResult)
def bulk_delete_expenses(

    payload: BulkDelete,
    user: User = Depends(_delete),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> BulkDeleteResult:
    """Permanently delete multiple expenses atomically. No status guard exists
    today (same as single delete — this task preserves that, not invents one).

    Registered *before* DELETE /{expense_id} below: both are now the same
    HTTP method, and route matching follows registration order, so this
    literal path must be tried first or it would never be reached.
    """
    org_id = _org_id(user)
    unique_ids = list(dict.fromkeys(payload.ids))

    expenses = [_owned(db, eid, org_id, user) for eid in unique_ids]
    for expense in expenses:
        _require_deletable(expense)
    for expense in expenses:
        db.delete(expense)
    db.commit()
    return BulkDeleteResult(deleted=len(expenses))


@router.delete("/{expense_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_expense(
    expense_id: str,
    user: User = Depends(_delete),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> None:
    expense = _owned(db, expense_id, _org_id(user), user)
    _require_deletable(expense)
    db.delete(expense)
    db.commit()


def _require_deletable(expense: Expense) -> None:
    """A reimbursed expense is a record of money paid out: terminal and read-only."""
    if expense.reimbursed_at is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="A reimbursed expense cannot be deleted",
        )
