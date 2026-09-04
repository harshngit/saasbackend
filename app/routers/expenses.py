from datetime import datetime, timezone

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile, status
from sqlalchemy.orm import Session

from app.core import scoping
from app.core.database import get_db
from app.core.deps import require_permission, require_unlocked_org
from app.core.files import save_upload
from app.models import EXPENSE_CATEGORIES, Expense, ExpenseItem, User
from app.schemas.expense import ExpenseCreate, ExpenseOut, ExpenseUpdate, RejectBody
from app.services import expense_service, lookup_service, notification_service, numbering_service

router = APIRouter(prefix="/expenses", tags=["expenses"])

_view = require_permission("expenses", "view")
_create = require_permission("expenses", "create")
_edit = require_permission("expenses", "edit")
_approve = require_permission("expenses", "approve")
_delete = require_permission("expenses", "delete")


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
    user: User = Depends(_create),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> Expense:
    org_id = _org_id(user)

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
        expense_status=payload.expense_status or "Submitted",
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
        payment_status=payload.payment_status or "Pending",
        approval_status=payload.approval_status or "Pending",
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
    )

    for item_data in calculated_items:
        expense.items.append(ExpenseItem(**item_data))

    db.add(expense)
    db.flush()
    notification_service.notify_org_admins(
        db, expense.organization_id, "New expense submitted",
        f"{expense.category}: Rs {expense.amount:,.2f}", type="expense", link=expense.id)
    db.commit()
    db.refresh(expense)
    return expense


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
    expense.receipt_url, _ = save_upload(db, expense.organization_id, file, request)
    db.commit()
    db.refresh(expense)
    return expense


@router.get("", response_model=list[ExpenseOut])
def list_expenses(
    user: User = Depends(_view),
    category: str | None = Query(default=None),
    status_filter: str | None = Query(default=None, alias="status"),
    submitted_by: str | None = Query(default=None),
    vendor_id: str | None = Query(default=None),
    branch_id: str | None = Query(default=None),
    department_id: str | None = Query(default=None),
    cost_center_id: str | None = Query(default=None),
    project_id: str | None = Query(default=None),
    payment_status: str | None = Query(default=None),
    is_recurring: bool | None = Query(default=None),
    tag: str | None = Query(default=None),
    db: Session = Depends(get_db),
) -> list[Expense]:
    org_id = _org_id(user)
    q = db.query(Expense).filter(Expense.organization_id == org_id)
    if category:
        q = q.filter(Expense.category == category)
    if status_filter:
        q = q.filter(Expense.status == status_filter)
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

    # An "own"-scope user only sees expenses they submitted — same dynamic
    # list-scoping helper as everywhere else. "all"-scope (Admin, Accountant,
    # or any custom org-wide role) is unaffected. team_columns=(): Expenses
    # are explicitly excluded from Team Scope.
    q = scoping.owned_by(q, db, user, Expense.submitted_by, team_columns=())
    return q.order_by(Expense.expense_date.desc()).all()


@router.get("/{expense_id}", response_model=ExpenseOut)
def get_expense(expense_id: str, user: User = Depends(_view), db: Session = Depends(get_db)) -> Expense:
    return _owned(db, expense_id, _org_id(user), user)


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

    # Validate vendor if vendor_id is updated
    if "vendor_id" in update_data:
        expense_service.validate_vendor(db, org_id, update_data["vendor_id"])

    # If items are provided in update, recalculate line items and totals
    if "items" in update_data:
        items_in = payload.items
        subtotal, tax_amount, total_amount, calculated_items = expense_service.calculate_totals(
            items_in=items_in,
            header_tax_rate=update_data.get("tax_rate", expense.tax_rate),
            header_tax_amount=update_data.get("tax_amount", expense.tax_amount),
            header_amount=update_data.get("amount", expense.amount),
        )
        expense.subtotal = subtotal
        expense.tax_amount = tax_amount
        expense.amount = total_amount
        expense.items.clear()
        for item_data in calculated_items:
            expense.items.append(ExpenseItem(**item_data))
        del update_data["items"]
    elif "amount" in update_data or "tax_rate" in update_data or "tax_amount" in update_data:
        subtotal, tax_amount, total_amount, _ = expense_service.calculate_totals(
            items_in=None,
            header_tax_rate=update_data.get("tax_rate", expense.tax_rate),
            header_tax_amount=update_data.get("tax_amount", expense.tax_amount),
            header_amount=update_data.get("amount", expense.amount),
        )
        expense.subtotal = subtotal
        expense.tax_amount = tax_amount
        expense.amount = total_amount

    # Validate TDS
    tds_app = update_data.get("tds_applicable", expense.tds_applicable)
    tds_amt = update_data.get("tds_amount", expense.tds_amount)
    if tds_app and tds_amt > expense.amount:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="TDS amount cannot exceed the total expense amount",
        )

    for field, value in update_data.items():
        if field not in ("amount", "subtotal", "tax_amount"):
            setattr(expense, field, value)

    if expense.status == "clarification_requested":
        expense.status = "pending"  # resubmitted for approval

    db.commit()
    db.refresh(expense)
    return expense


@router.patch("/{expense_id}/approve", response_model=ExpenseOut)
def approve_expense(
    expense_id: str,
    user: User = Depends(_approve),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> Expense:
    # Reviewer action, gated by the separate expenses:approve permission —
    # deliberately org-wide regardless of the reviewer's own data scope.
    expense = _owned(db, expense_id, _org_id(user), user, enforce_scope=False)
    if expense.status not in ("pending", "clarification_requested"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Only pending expenses can be approved")
    expense.status = "approved"
    expense.approval_status = "Approved"
    expense.approved_by = user.id
    db.commit()
    db.refresh(expense)
    return expense


@router.patch("/{expense_id}/reject", response_model=ExpenseOut)
def reject_expense(
    expense_id: str,
    payload: RejectBody,
    user: User = Depends(_approve),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> Expense:
    # Reviewer action, gated by the separate expenses:approve permission —
    # deliberately org-wide regardless of the reviewer's own data scope.
    expense = _owned(db, expense_id, _org_id(user), user, enforce_scope=False)
    if expense.status != "pending":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Only pending expenses can be rejected")
    expense.status = "rejected"
    expense.approval_status = "Rejected"
    expense.approved_by = user.id
    expense.reject_reason = payload.reason
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
    if expense.submitted_by:
        notification_service.notify(
            db, expense.submitted_by, "Clarification requested on your expense",
            payload.reason, type="expense", link=expense.id, organization_id=expense.organization_id)
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


@router.delete("/{expense_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_expense(
    expense_id: str,
    user: User = Depends(_delete),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> None:
    expense = _owned(db, expense_id, _org_id(user), user)
    db.delete(expense)
    db.commit()
