from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import require_permission, require_unlocked_org
from app.models import Customer, User
from app.schemas.customer import CustomerCreate, CustomerOut, CustomerUpdate

router = APIRouter(prefix="/customers", tags=["customers"])

# Permission gates (admin/super_admin bypass; staff checked against their role matrix).
_view = require_permission("customers", "view")
_create = require_permission("customers", "create")
_edit = require_permission("customers", "edit")
_delete = require_permission("customers", "delete")


def _org_id(user: User) -> str:
    if not user.organization_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No organization on this account")
    return user.organization_id


def _owned_customer(db: Session, customer_id: str, org_id: str) -> Customer:
    customer = db.get(Customer, customer_id)
    if customer is None or customer.organization_id != org_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Customer not found")
    return customer


def _validate_assignee(db: Session, org_id: str, sales_officer_id: str | None) -> None:
    if sales_officer_id is None:
        return
    officer = db.get(User, sales_officer_id)
    if officer is None or officer.organization_id != org_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="assigned_sales_officer_id is not a user in your firm",
        )


@router.post("", response_model=CustomerOut, status_code=status.HTTP_201_CREATED)
def create_customer(
    payload: CustomerCreate,
    user: User = Depends(_create),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> Customer:
    org_id = _org_id(user)
    _validate_assignee(db, org_id, payload.assigned_sales_officer_id)
    customer = Customer(organization_id=org_id, **payload.model_dump())
    db.add(customer)
    db.commit()
    db.refresh(customer)
    return customer


@router.get("", response_model=list[CustomerOut])
def list_customers(
    user: User = Depends(_view),
    search: str | None = Query(default=None, description="matches name / business / phone / email"),
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
            )
        )
    if category is not None:
        query = query.filter(Customer.category == category)
    if is_active is not None:
        query = query.filter(Customer.is_active == is_active)
    if assigned_sales_officer_id is not None:
        query = query.filter(Customer.assigned_sales_officer_id == assigned_sales_officer_id)
    return query.order_by(Customer.created_at.desc()).all()


@router.get("/{customer_id}", response_model=CustomerOut)
def get_customer(customer_id: str, user: User = Depends(_view), db: Session = Depends(get_db)) -> Customer:
    return _owned_customer(db, customer_id, _org_id(user))


@router.patch("/{customer_id}", response_model=CustomerOut)
def update_customer(
    customer_id: str,
    payload: CustomerUpdate,
    user: User = Depends(_edit),
    _unlocked: User = Depends(require_unlocked_org),
    db: Session = Depends(get_db),
) -> Customer:
    org_id = _org_id(user)
    customer = _owned_customer(db, customer_id, org_id)
    data = payload.model_dump(exclude_unset=True)
    if "assigned_sales_officer_id" in data:
        _validate_assignee(db, org_id, data["assigned_sales_officer_id"])
    for field, value in data.items():
        setattr(customer, field, value)
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
    customer = _owned_customer(db, customer_id, _org_id(user))
    db.delete(customer)
    db.commit()
