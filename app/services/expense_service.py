import calendar
from datetime import datetime, timedelta, timezone
from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.models.expense import Expense, ExpenseItem
from app.models.supplier import Supplier
from app.schemas.expense import ExpenseItemIn
from app.services import numbering_service


def _add_months(sourcedate: datetime, months: int) -> datetime:
    month = sourcedate.month - 1 + months
    year = sourcedate.year + month // 12
    month = month % 12 + 1
    day = min(sourcedate.day, calendar.monthrange(year, month)[1])
    return sourcedate.replace(year=year, month=month, day=day)


def _add_years(sourcedate: datetime, years: int) -> datetime:
    try:
        return sourcedate.replace(year=sourcedate.year + years)
    except ValueError:
        # Handle leap year Feb 29
        return sourcedate.replace(year=sourcedate.year + years, day=28)


def calculate_totals(
    items_in: list[ExpenseItemIn] | None,
    header_tax_rate: float | None = None,
    header_tax_amount: float | None = None,
    header_amount: float | None = None,
) -> tuple[float, float, float, list[dict]]:
    """Calculate subtotal, tax_amount, total amount and processed line item records.

    Returns (subtotal, tax_amount, amount, calculated_items).
    """
    if items_in:
        calculated_items = []
        subtotal = 0.0
        tax_total = 0.0
        for item in items_in:
            if item.quantity <= 0:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Item quantity must be greater than 0",
                )
            if item.unit_price < 0:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Item unit price cannot be negative",
                )
            if item.tax_rate < 0 or item.tax_rate > 100:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Item tax rate must be between 0 and 100",
                )
            line_subtotal = round(item.quantity * item.unit_price, 2)
            item_tax = round(line_subtotal * (item.tax_rate or 0.0) / 100, 2)
            line_total = round(line_subtotal + item_tax, 2)

            subtotal += line_subtotal
            tax_total += item_tax
            calculated_items.append({
                "description": item.description,
                "quantity": item.quantity,
                "unit_price": item.unit_price,
                "tax_rate": item.tax_rate,
                "tax_amount": item_tax,
                "line_total": line_total,
            })

        subtotal = round(subtotal, 2)
        tax_amount = round(tax_total, 2)
        total_amount = round(subtotal + tax_amount, 2)
        return subtotal, tax_amount, total_amount, calculated_items

    # Single-amount header mode (backward compatibility)
    amt = round(header_amount or 0.0, 2)
    if amt <= 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Expense amount must be greater than 0",
        )
    if header_tax_amount is not None and header_tax_amount > 0:
        tax_amount = round(header_tax_amount, 2)
        subtotal = round(max(amt - tax_amount, 0.0), 2)
    elif header_tax_rate is not None and header_tax_rate > 0:
        subtotal = round(amt / (1 + header_tax_rate / 100), 2)
        tax_amount = round(amt - subtotal, 2)
    else:
        subtotal = amt
        tax_amount = 0.0

    return subtotal, tax_amount, amt, []


def validate_vendor(db: Session, org_id: str, vendor_id: str | None) -> Supplier | None:
    """Validate vendor exists and belongs strictly to the caller's organization."""
    if not vendor_id:
        return None
    vendor = db.get(Supplier, vendor_id)
    if vendor is None or vendor.organization_id != org_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Vendor does not exist in your organization",
        )
    return vendor


def compute_next_due_date(base_date: datetime, frequency: str | None) -> datetime | None:
    """Compute subsequent due date based on frequency."""
    if not frequency:
        return None
    freq = frequency.lower()
    if freq == "daily":
        return base_date + timedelta(days=1)
    elif freq == "weekly":
        return base_date + timedelta(weeks=1)
    elif freq == "monthly":
        return _add_months(base_date, 1)
    elif freq == "yearly":
        return _add_years(base_date, 1)
    return None


def process_due_recurring_expenses(db: Session, org_id: str | None = None) -> list[Expense]:
    """Generate the next expense instance for any recurring expense whose next_due_date has arrived."""
    now = datetime.now(timezone.utc)
    q = db.query(Expense).filter(
        Expense.is_recurring.is_(True),
        Expense.next_due_date.isnot(None),
        Expense.next_due_date <= now,
    )
    if org_id:
        q = q.filter(Expense.organization_id == org_id)

    due_templates = q.all()
    created_expenses = []

    for template in due_templates:
        org = template.organization_id
        # Create cloned child expense
        new_exp = Expense(
            organization_id=org,
            category=template.category,
            amount=template.amount,
            subtotal=template.subtotal,
            tax_rate=template.tax_rate,
            tax_amount=template.tax_amount,
            currency=template.currency,
            description=f"Recurring: {template.description or template.category}",
            expense_date=template.next_due_date or now,
            payment_mode=template.payment_mode,
            status="pending",
            submitted_by=template.submitted_by,
            expense_id=numbering_service.next_number(db, org, Expense.expense_id, "EXPID"),
            expense_number=numbering_service.next_number(db, org, Expense.expense_number, "EXP"),
            expense_type=template.expense_type,
            expense_status="Submitted",
            financial_year=template.financial_year,
            branch_id=template.branch_id,
            department_id=template.department_id,
            vendor_id=template.vendor_id,
            payee_name=template.payee_name,
            contact_person=template.contact_person,
            mobile_number=template.mobile_number,
            email_address=template.email_address,
            payee_gstin=template.payee_gstin,
            paid_from_account_id=template.paid_from_account_id,
            payment_reference=template.payment_reference,
            payment_status="Pending",
            approval_status="Pending",
            expense_account_id=template.expense_account_id,
            cost_center_id=template.cost_center_id,
            project_id=template.project_id,
            tax_category=template.tax_category,
            tds_applicable=template.tds_applicable,
            tds_amount=template.tds_amount,
            tags=list(template.tags or []),
            is_recurring=False,  # child instance is not a generator
        )
        # Copy item lines if template had items
        for item in template.items:
            new_exp.items.append(
                ExpenseItem(
                    description=item.description,
                    quantity=item.quantity,
                    unit_price=item.unit_price,
                    tax_rate=item.tax_rate,
                    tax_amount=item.tax_amount,
                    line_total=item.line_total,
                )
            )

        db.add(new_exp)
        created_expenses.append(new_exp)

        # Advance template's next_due_date
        template.next_due_date = compute_next_due_date(
            template.next_due_date or now, template.recurrence_frequency
        )

    db.commit()
    for exp in created_expenses:
        db.refresh(exp)
    return created_expenses
