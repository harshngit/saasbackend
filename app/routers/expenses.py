from datetime import datetime, timezone

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import require_permission, require_unlocked_org
from app.core.files import store_upload
from app.models import EXPENSE_CATEGORIES, Expense, User
from app.schemas.expense import ExpenseCreate, ExpenseOut, ExpenseUpdate, RejectBody
from app.services import notification_service, numbering_service

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


def _owned(db: Session, expense_id: str, org_id: str) -> Expense:
    e = db.get(Expense, expense_id)
    if e is None or e.organization_id != org_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Expense not found")
    return e


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
    data = payload.model_dump(exclude={"expense_date"})
    expense = Expense(
        organization_id=org_id,
        submitted_by=user.id,
        expense_date=payload.expense_date or datetime.now(timezone.utc),
        # Sheet: both are Auto Numbers.
        expense_id=numbering_service.next_number(db, org_id, Expense.expense_id, "EXPID"),
        expense_number=numbering_service.next_number(db, org_id, Expense.expense_number, "EXP"),
        **{
            **data,
            "expense_status": data.get("expense_status") or "Submitted",
            "payment_status": data.get("payment_status") or "Pending",
            "approval_status": data.get("approval_status") or "Pending",
        },
    )
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
    expense_id: str,
    file: UploadFile = File(...),
    user: User = Depends(_edit),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> Expense:
    """Attach a bill/receipt (image or PDF, max 10 MB) to an expense."""
    expense = _owned(db, expense_id, _org_id(user))
    expense.receipt_url = store_upload(file)
    db.commit()
    db.refresh(expense)
    return expense


@router.get("", response_model=list[ExpenseOut])
def list_expenses(
    user: User = Depends(_view),
    category: str | None = Query(default=None),
    status_filter: str | None = Query(default=None, alias="status"),
    submitted_by: str | None = Query(default=None),
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
    return q.order_by(Expense.expense_date.desc()).all()


@router.get("/{expense_id}", response_model=ExpenseOut)
def get_expense(expense_id: str, user: User = Depends(_view), db: Session = Depends(get_db)) -> Expense:
    return _owned(db, expense_id, _org_id(user))


@router.patch("/{expense_id}", response_model=ExpenseOut)
def update_expense(
    expense_id: str,
    payload: ExpenseUpdate,
    user: User = Depends(_edit),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> Expense:
    expense = _owned(db, expense_id, _org_id(user))
    if expense.status not in ("pending", "clarification_requested"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Only pending expenses can be edited")
    for field, value in payload.model_dump(exclude_unset=True).items():
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
    expense = _owned(db, expense_id, _org_id(user))
    if expense.status not in ("pending", "clarification_requested"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Only pending expenses can be approved")
    expense.status = "approved"
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
    expense = _owned(db, expense_id, _org_id(user))
    if expense.status != "pending":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Only pending expenses can be rejected")
    expense.status = "rejected"
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
    expense = _owned(db, expense_id, _org_id(user))
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


@router.delete("/{expense_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_expense(
    expense_id: str,
    user: User = Depends(_delete),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> None:
    expense = _owned(db, expense_id, _org_id(user))
    db.delete(expense)
    db.commit()
