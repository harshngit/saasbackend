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


# ------------------------------- lifecycle -------------------------------
#
#   created -> pending_approval -> approved -> reimbursed
#                     |
#                     +-> rejected
#
# Stored `status` keeps its original values (see EXPENSE_LIFECYCLE_STATUSES in
# app/models/expense.py); `Expense.lifecycle_status` is the client-facing view.

from app.core.files import extract_file_id_from_url, normalize_file_url  # noqa: E402
from app.models import ActivityLog, StoredFile, User  # noqa: E402
from app.models.expense import EXPENSE_REIMBURSEMENT_METHODS  # noqa: E402
from app.services import activity_service, role_service  # noqa: E402

PENDING_STATUSES = ("pending", "clarification_requested")
# Fields that make up the submitted amount. Once an expense is created it is in the
# approval workflow, so these never change: a wrong amount is rejected and raised
# again, never silently edited under an approver.
AMOUNT_FIELDS = ("amount", "items", "tax_rate", "tax_amount", "currency", "tds_applicable", "tds_amount")
# Lifecycle fields only the approve / reject / reimburse actions may move.
LIFECYCLE_FIELDS = ("approval_status", "payment_status", "expense_status")

_METHOD_ALIASES = {
    "cash": "cash",
    "bank_transfer": "bank_transfer", "bank transfer": "bank_transfer", "bank": "bank_transfer",
    "neft": "bank_transfer", "rtgs": "bank_transfer", "imps": "bank_transfer",
    "upi": "upi", "gpay": "upi", "phonepe": "upi", "paytm": "upi",
    "cheque": "cheque", "check": "cheque",
}

_TITLES = {
    "created": "Expense created",
    "approved": "Expense approved",
    "rejected": "Expense rejected",
    "reimbursed": "Expense reimbursed",
    "clarification_requested": "Clarification requested",
    "resubmitted": "Expense resubmitted",
}


def normalize_reimbursement_method(value: str | None) -> str:
    key = _METHOD_ALIASES.get((value or "").strip().lower())
    if key is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"payment_method must be one of {list(EXPENSE_REIMBURSEMENT_METHODS)}",
        )
    return key


def is_admin(user: User) -> bool:
    """Admin / Super Admin: the existing full-access concept, not a role name."""
    return role_service.is_full_access(user)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def record_history(db: Session, expense: Expense, actor: User | None, action: str, note: str | None = None) -> None:
    activity_service.record(
        db, expense.organization_id, actor, "expense",
        f"{_TITLES.get(action, action)}: {expense.expense_number or expense.category}",
        note,
        entity_type="expense", entity_id=expense.id, action=action,
    )


def _name(db: Session, user_id: str | None) -> str | None:
    if not user_id:
        return None
    u = db.get(User, user_id)
    return u.name if u is not None else None


def history(db: Session, expense: Expense) -> list[dict]:
    """created / approved / rejected / reimbursed trail, oldest first. Expenses that
    pre-date the audit trail get their entries rebuilt from the lifecycle columns."""
    rows: list[ActivityLog] = activity_service.history(db, expense.organization_id, "expense", expense.id)
    entries = [
        {"action": r.action, "title": _TITLES.get(r.action or "", r.title), "actor_id": r.user_id,
         "actor_name": r.actor_name, "at": r.created_at, "note": r.description}
        for r in rows
    ]
    if not any(e["action"] == "created" for e in entries):
        legacy = [{"action": "created", "title": _TITLES["created"], "actor_id": expense.submitted_by,
                   "actor_name": _name(db, expense.submitted_by), "at": expense.created_at, "note": None}]
        if expense.status == "approved" and not any(e["action"] == "approved" for e in entries):
            legacy.append({"action": "approved", "title": _TITLES["approved"], "actor_id": expense.approved_by,
                           "actor_name": _name(db, expense.approved_by),
                           "at": expense.approved_at or expense.updated_at, "note": expense.approval_note})
        if expense.status == "rejected" and not any(e["action"] == "rejected" for e in entries):
            reviewer = expense.rejected_by or expense.approved_by
            legacy.append({"action": "rejected", "title": _TITLES["rejected"], "actor_id": reviewer,
                           "actor_name": _name(db, reviewer),
                           "at": expense.rejected_at or expense.updated_at, "note": expense.reject_reason})
        entries = legacy + entries
    return entries


def _require_not_own(user: User, expense: Expense, verb: str) -> None:
    """A reviewer may not decide on their own expense. Admins are excepted: their own
    expenses are approved on creation."""
    if expense.submitted_by == user.id and not is_admin(user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=f"You cannot {verb} your own expense")


def _post_gl(db: Session, expense: Expense, document_type: str) -> None:
    from app.services import accounting_service
    try:
        accounting_service.post_operational_document(db, expense.organization_id, document_type, expense.id)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 - surfaced as a clean 500; the caller never commits
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to post expense to general ledger: {exc}",
        ) from exc


def mark_approved(db: Session, expense: Expense, user: User, note: str | None = None) -> None:
    expense.status = "approved"
    expense.approval_status = "Approved"
    expense.expense_status = "Approved"
    expense.approved_by = user.id
    expense.approved_at = _utcnow()
    expense.approval_note = note
    db.flush()
    record_history(db, expense, user, "approved", note)


def mark_reimbursed(
    db: Session, expense: Expense, user: User, method: str,
    reference: str | None, notes: str | None, paid_at: datetime | None,
) -> None:
    expense.reimbursed_by = user.id
    expense.reimbursed_at = paid_at or _utcnow()
    expense.reimbursement_method = method
    expense.reimbursement_reference = reference
    expense.reimbursement_notes = notes
    expense.payment_status = "Paid"
    expense.expense_status = "Paid"
    db.flush()
    record_history(db, expense, user, "reimbursed", notes)


def approve(db: Session, expense: Expense, user: User, note: str | None = None) -> Expense:
    if expense.status not in PENDING_STATUSES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Only pending expenses can be approved")
    _require_not_own(user, expense, "approve")
    mark_approved(db, expense, user, note)
    _post_gl(db, expense, "expense")
    return expense


def reject(db: Session, expense: Expense, user: User, reason: str | None) -> Expense:
    if expense.status not in PENDING_STATUSES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Only pending expenses can be rejected")
    if not (reason or "").strip():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="A rejection reason is required")
    _require_not_own(user, expense, "reject")
    expense.status = "rejected"
    expense.approval_status = "Rejected"
    expense.expense_status = "Rejected"
    # approved_by is still written for clients that read the reviewer from it.
    expense.approved_by = user.id
    expense.rejected_by = user.id
    expense.rejected_at = _utcnow()
    expense.reject_reason = reason.strip()
    db.flush()
    record_history(db, expense, user, "rejected", expense.reject_reason)
    return expense


def reimburse(
    db: Session, expense: Expense, user: User, *, payment_method: str, amount: float | None = None,
    payment_reference: str | None = None, notes: str | None = None, paid_at: datetime | None = None,
) -> Expense:
    """Full reimbursement only: the amount, if sent, must be the expense's net payable."""
    if expense.reimbursed_at is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="This expense has already been reimbursed")
    if expense.status != "approved":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Only approved expenses can be reimbursed")
    _require_not_own(user, expense, "reimburse")
    method = normalize_reimbursement_method(payment_method)
    if amount is not None and abs(round(amount, 2) - expense.net_payable) > 0.005:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Partial reimbursement is not supported: amount must be {expense.net_payable:.2f}",
        )
    mark_reimbursed(db, expense, user, method, payment_reference, notes, paid_at)
    _post_gl(db, expense, "expense_reimbursement")
    return expense


def lock_violations(expense: Expense, update_data: dict) -> list[str]:
    """Amount / lifecycle fields a PATCH tries to change. Re-sending the current
    value (a full-form save) is not a change."""
    changed = []
    for field in AMOUNT_FIELDS + LIFECYCLE_FIELDS:
        if field not in update_data:
            continue
        value = update_data[field]
        if field == "items":
            if value is None:
                continue
            current = [(i.description, float(i.quantity), float(i.unit_price), float(i.tax_rate)) for i in expense.items]
            sent = [(i["description"], float(i.get("quantity", 1.0)), float(i.get("unit_price", 0.0)),
                     float(i.get("tax_rate", 0.0))) for i in value]
            if sent != current:
                changed.append(field)
        elif field in ("amount", "tax_amount", "tds_amount", "tax_rate"):
            cur = getattr(expense, field)
            if value is None and field != "amount":
                if cur in (None, 0, 0.0):
                    continue
            if value is None:
                continue
            if cur is None or abs(float(value) - float(cur)) > 0.005:
                changed.append(field)
        elif value is not None and value != getattr(expense, field):
            changed.append(field)
    return changed


def validate_attachment_refs(db: Session, org_id: str, *refs: str | None) -> None:
    """A /files/{id} reference attached to an expense must be this organization's own
    upload. External URLs and legacy data: URIs pass through unchanged."""
    for ref in refs:
        file_id = extract_file_id_from_url(ref) if isinstance(ref, str) else None
        if not file_id:
            continue
        stored = db.get(StoredFile, file_id)
        if stored is None or (stored.organization_id and stored.organization_id != org_id):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="An attached file was not found in your organization",
            )


def supporting_refs(docs: list | None) -> list[str]:
    out = []
    for doc in docs or []:
        if isinstance(doc, str):
            out.append(doc)
        elif isinstance(doc, dict):
            ref = doc.get("url") or doc.get("file_url") or doc.get("file_id") or doc.get("id")
            if isinstance(ref, str):
                out.append(ref)
    return out


def attachments(db: Session, expense: Expense) -> list[dict]:
    """Proof attached to an expense (receipt, vendor invoice, supporting documents)
    with the stored file's metadata. Only files of the expense's own organization
    are described; any other reference comes back without metadata."""
    found: list[tuple[str, str | None, dict | None]] = []
    if expense.receipt_url:
        found.append(("receipt", expense.receipt_url, None))
    if expense.vendor_invoice_url:
        found.append(("vendor_invoice", expense.vendor_invoice_url, None))
    for doc in expense.supporting_documents or []:
        ref = supporting_refs([doc])
        found.append(("supporting_document", ref[0] if ref else None, doc if isinstance(doc, dict) else None))

    out = []
    for kind, ref, meta in found:
        if not ref:
            continue
        meta = meta or {}
        file_id = extract_file_id_from_url(ref)
        stored = db.get(StoredFile, file_id) if file_id else None
        if stored is not None and stored.organization_id and stored.organization_id != expense.organization_id:
            stored = None
        mime = stored.content_type if stored is not None else (meta.get("mime_type") or meta.get("content_type"))
        out.append({
            "id": stored.id if stored is not None else file_id,
            "kind": kind,
            "url": normalize_file_url(ref),
            "filename": stored.filename if stored is not None else (meta.get("filename") or meta.get("name")),
            "mime_type": mime,
            "size": stored.size if stored is not None else None,
            "uploaded_at": stored.created_at if stored is not None else None,
            "is_image": bool(mime and mime.startswith("image/")),
        })
    return out
