from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import require_permission, require_unlocked_org
from app.models import EXPENSE_CATEGORIES, Expense, User
from app.schemas.expense import ExpenseCreate, ExpenseOut, ExpenseUpdate, RejectBody

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
    expense = Expense(
        organization_id=_org_id(user),
        submitted_by=user.id,
        expense_date=payload.expense_date or datetime.now(timezone.utc),
        **payload.model_dump(exclude={"expense_date"}),
    )
    db.add(expense)
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
    if expense.status != "pending":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Only pending expenses can be edited")
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(expense, field, value)
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
    if expense.status != "pending":
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
