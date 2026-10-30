from decimal import Decimal
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import func, select, update as sa_update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.accounting import (
    ACCOUNT_CLASSIFICATIONS,
    ACCOUNT_TYPES,
    Account,
    JournalEntry,
    JournalLine,
)
from app.models.customer import CustomerPayment
from app.models.expense import Expense
from app.models.invoice import Invoice
from app.models.number_sequence import NumberSequence
from app.models.sales_return import SalesReturn


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _to_decimal(val: Any) -> Decimal:
    """Safely convert any numeric value to Decimal quantized to 2 decimal places."""
    if isinstance(val, Decimal):
        return val.quantize(Decimal("0.01"))
    try:
        return Decimal(str(round(float(val or 0.0), 2))).quantize(Decimal("0.01"))
    except Exception:
        return Decimal("0.00")


def _allocate_journal_entry_number(
    db: Session,
    org_id: str,
    entry_type: str = "manual",
    effective_date: datetime | None = None,
) -> str:
    """Concurrency-safe atomic allocation of entry numbers via NumberSequence."""
    series = "OB" if entry_type == "opening_balance" else "JE"
    eff_dt = effective_date or _now()
    year = eff_dt.year if hasattr(eff_dt, "year") else _now().year
    prefix = f"{series}-{year}-"

    _match = (
        NumberSequence.organization_id == org_id,
        NumberSequence.series == series,
        NumberSequence.year == year,
    )

    exists = db.query(NumberSequence).filter(*_match).first()
    if exists is None:
        highest = 0
        existing_numbers = (
            db.query(JournalEntry.entry_number)
            .filter(
                JournalEntry.organization_id == org_id,
                JournalEntry.entry_number.like(f"{prefix}%"),
            )
            .all()
        )
        for (num_str,) in existing_numbers:
            try:
                val = int(num_str.split("-")[-1])
                if val > highest:
                    highest = val
            except (ValueError, IndexError):
                pass
        try:
            with db.begin_nested():
                db.add(
                    NumberSequence(
                        organization_id=org_id,
                        series=series,
                        year=year,
                        last_number=highest,
                    )
                )
                db.flush()
        except (IntegrityError, Exception):
            pass

    result = db.execute(
        sa_update(NumberSequence)
        .where(*_match)
        .values(last_number=NumberSequence.last_number + 1)
        .returning(NumberSequence.last_number)
    )
    row = result.first()
    db.flush()
    if row is not None:
        return f"{prefix}{row[0]:05d}"

    # Fallback if DB does not support returning or row was missing
    count = db.query(func.count(JournalEntry.id)).filter(JournalEntry.organization_id == org_id).scalar() or 0
    return f"{prefix}{count + 1:05d}"


DEFAULT_CHART_OF_ACCOUNTS = [
    # 1. Assets
    {"account_code": "1010", "name": "Cash on Hand", "account_type": "asset", "classification": "cash_and_bank", "is_system": True},
    {"account_code": "1020", "name": "Operating Bank Account", "account_type": "asset", "classification": "cash_and_bank", "is_system": True},
    {"account_code": "1200", "name": "Accounts Receivable", "account_type": "asset", "classification": "accounts_receivable", "is_system": True},
    {"account_code": "1300", "name": "Inventory Asset", "account_type": "asset", "classification": "inventory", "is_system": True},
    {"account_code": "1500", "name": "Office Equipment & Assets", "account_type": "asset", "classification": "fixed_asset", "is_system": False},
    # 2. Liabilities
    {"account_code": "2010", "name": "Accounts Payable", "account_type": "liability", "classification": "accounts_payable", "is_system": True},
    {"account_code": "2100", "name": "GST Output Payable", "account_type": "liability", "classification": "tax_liability", "is_system": True},
    {"account_code": "2200", "name": "Bank Loan & Borrowings", "account_type": "liability", "classification": "loan_liability", "is_system": False},
    # 3. Equity
    {"account_code": "3010", "name": "Owner Capital / Equity", "account_type": "equity", "classification": "owner_equity", "is_system": True},
    {"account_code": "3020", "name": "Retained Earnings", "account_type": "equity", "classification": "retained_earnings", "is_system": True},
    # 4. Income Statement Accounts (Revenue & Expenses for transactional ledger)
    {"account_code": "4010", "name": "Sales Revenue", "account_type": "revenue", "classification": "operating_revenue", "is_system": True},
    {"account_code": "4020", "name": "Sales Returns & Allowances", "account_type": "revenue", "classification": "contra_revenue", "is_system": True},
    {"account_code": "5010", "name": "Cost of Goods Sold", "account_type": "expense", "classification": "cogs", "is_system": True},
    {"account_code": "6010", "name": "Operating Expenses", "account_type": "expense", "classification": "operating_expense", "is_system": True},
]


def ensure_default_accounts(db: Session, org_id: str) -> dict[str, Account]:
    """Ensure standard default Chart of Accounts exists for the organization."""
    existing_accounts = db.query(Account).filter(Account.organization_id == org_id).all()
    by_code = {a.account_code: a for a in existing_accounts}

    added = False
    for spec in DEFAULT_CHART_OF_ACCOUNTS:
        code = spec["account_code"]
        if code not in by_code:
            account = Account(
                id=_uuid(),
                organization_id=org_id,
                account_code=code,
                name=spec["name"],
                account_type=spec["account_type"],
                classification=spec.get("classification"),
                is_active=True,
                is_system=spec.get("is_system", False),
            )
            db.add(account)
            by_code[code] = account
            added = True

    if added:
        db.flush()

    return by_code


def create_journal_entry(
    db: Session,
    org_id: str,
    effective_date: datetime,
    lines: list[dict[str, Any]],
    entry_type: str = "manual",
    reference: str | None = None,
    source_type: str | None = None,
    source_id: str | None = None,
    description: str | None = None,
    user_id: str | None = None,
) -> JournalEntry:
    """Create and post a balanced double-entry journal entry.

    Strict validation:
    - At least 2 lines.
    - debits and credits must be non-negative Decimals.
    - Total debits must equal total credits exactly (2 decimals).
    - Prevents duplicate posting if source_type and source_id match an existing entry.
    - Concurrency safe via savepoints and atomic number allocation.
    """
    if source_type and source_id:
        existing = (
            db.query(JournalEntry)
            .filter(
                JournalEntry.organization_id == org_id,
                JournalEntry.source_type == source_type,
                JournalEntry.source_id == source_id,
            )
            .first()
        )
        if existing:
            return existing

    if not lines or len(lines) < 2:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="A journal entry must contain at least two lines.",
        )

    accounts_map = {a.id: a for a in db.query(Account).filter(Account.organization_id == org_id).all()}
    if not accounts_map:
        ensure_default_accounts(db, org_id)
        accounts_map = {a.id: a for a in db.query(Account).filter(Account.organization_id == org_id).all()}
    accounts_by_code = {a.account_code: a for a in accounts_map.values()}

    validated_lines: list[dict[str, Any]] = []
    total_debits = Decimal("0.00")
    total_credits = Decimal("0.00")
    zero_dec = Decimal("0.00")

    for idx, raw in enumerate(lines, start=1):
        # Resolve account by id or code
        acc_id = raw.get("account_id")
        acc_code = raw.get("account_code")
        account = None
        if acc_id and acc_id in accounts_map:
            account = accounts_map[acc_id]
        elif acc_code and acc_code in accounts_by_code:
            account = accounts_by_code[acc_code]

        if not account:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Line {idx}: Account '{acc_id or acc_code}' not found in organization Chart of Accounts.",
            )

        if not account.is_active:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Line {idx}: Account '{account.account_code} - {account.name}' is inactive.",
            )

        debit = _to_decimal(raw.get("debit", 0.0))
        credit = _to_decimal(raw.get("credit", 0.0))

        if debit < zero_dec or credit < zero_dec:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Line {idx}: Debit and credit amounts must be non-negative.",
            )

        if debit > zero_dec and credit > zero_dec:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Line {idx}: A single line cannot have both debit and credit amounts.",
            )

        if debit == zero_dec and credit == zero_dec:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Line {idx}: Line must have either a non-zero debit or credit amount.",
            )

        total_debits += debit
        total_credits += credit

        validated_lines.append({
            "account_id": account.id,
            "line_number": idx,
            "debit": debit,
            "credit": credit,
            "description": raw.get("description"),
        })

    if total_debits != total_credits:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unbalanced journal entry: Total debits ({total_debits:.2f}) must equal total credits ({total_credits:.2f}).",
        )

    # Concurrency-safe entry number generation
    entry_number = _allocate_journal_entry_number(db, org_id, entry_type, effective_date)

    entry = JournalEntry(
        id=_uuid(),
        organization_id=org_id,
        entry_number=entry_number,
        entry_type=entry_type,
        effective_date=effective_date,
        reference=reference,
        source_type=source_type,
        source_id=source_id,
        status="posted",
        description=description,
        created_by_user_id=user_id,
    )

    try:
        with db.begin_nested():
            db.add(entry)
            for vl in validated_lines:
                line = JournalLine(
                    id=_uuid(),
                    organization_id=org_id,
                    journal_entry_id=entry.id,
                    account_id=vl["account_id"],
                    line_number=vl["line_number"],
                    debit=vl["debit"],
                    credit=vl["credit"],
                    description=vl["description"],
                )
                db.add(line)
            db.flush()
    except IntegrityError:
        if source_type and source_id:
            existing = (
                db.query(JournalEntry)
                .filter(
                    JournalEntry.organization_id == org_id,
                    JournalEntry.source_type == source_type,
                    JournalEntry.source_id == source_id,
                )
                .first()
            )
            if existing:
                return existing
        raise

    return entry


def reverse_journal_entry(
    db: Session,
    org_id: str,
    entry_id: str,
    reversal_date: datetime | None = None,
    reason: str | None = None,
    user_id: str | None = None,
) -> JournalEntry:
    """Reverse a posted journal entry by posting an auditable mirror reversal entry."""
    entry = (
        db.query(JournalEntry)
        .filter(
            JournalEntry.id == entry_id,
            JournalEntry.organization_id == org_id,
        )
        .first()
    )
    if not entry:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Journal entry '{entry_id}' not found.",
        )

    if entry.is_reversal:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot reverse a reversal entry.",
        )

    if entry.status == "reversed":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Journal entry '{entry.entry_number}' has already been reversed.",
        )

    rev_date = reversal_date or entry.effective_date or datetime.now(timezone.utc)
    rev_lines = [
        {
            "account_id": l.account_id,
            "debit": l.credit,  # swap debits and credits
            "credit": l.debit,
            "description": f"Reversal of {entry.entry_number}: {l.description or ''}".strip(),
        }
        for l in entry.lines
    ]

    rev_desc = f"Reversal of {entry.entry_number}" + (f": {reason}" if reason else "")
    rev_entry = create_journal_entry(
        db=db,
        org_id=org_id,
        effective_date=rev_date,
        lines=rev_lines,
        entry_type="reversal",
        reference=f"REV-{entry.entry_number}",
        source_type="journal_entry_reversal",
        source_id=entry.id,
        description=rev_desc,
        user_id=user_id,
    )

    rev_entry.is_reversal = True
    rev_entry.reversed_entry_id = entry.id
    entry.status = "reversed"

    db.flush()
    return rev_entry


def post_opening_balances(
    db: Session,
    org_id: str,
    effective_date: datetime,
    balances: list[dict[str, Any]],
    description: str | None = None,
    user_id: str | None = None,
) -> JournalEntry:
    """Establish balanced opening balances for an organization as of an effective date."""
    ensure_default_accounts(db, org_id)

    # Check for existing opening balance entry
    existing = (
        db.query(JournalEntry)
        .filter(
            JournalEntry.organization_id == org_id,
            JournalEntry.entry_type == "opening_balance",
            JournalEntry.status == "posted",
        )
        .first()
    )
    if existing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Opening balances have already been posted for this organization (Entry: {existing.entry_number}). Use manual journal entries or reversal to adjust balances.",
        )

    lines = []
    for b in balances:
        lines.append({
            "account_id": b.get("account_id"),
            "account_code": b.get("account_code"),
            "debit": b.get("debit", 0.0),
            "credit": b.get("credit", 0.0),
            "description": b.get("description", "Opening balance"),
        })

    return create_journal_entry(
        db=db,
        org_id=org_id,
        effective_date=effective_date,
        lines=lines,
        entry_type="opening_balance",
        reference=f"OB-{effective_date.date().isoformat()}",
        source_type="opening_balance",
        source_id=f"OB-{org_id}-{effective_date.date().isoformat()}",
        description=description or "Initial Opening Balances",
        user_id=user_id,
    )


def post_operational_document(
    db: Session,
    org_id: str,
    document_type: str,
    document_id: str,
) -> JournalEntry | None:
    """Post an operational business document to the general ledger with stable idempotency."""
    accounts = ensure_default_accounts(db, org_id)

    if document_type == "invoice":
        inv = db.get(Invoice, document_id)
        if not inv or inv.organization_id != org_id or inv.is_credit_note or inv.status in ("void", "cancelled"):
            return None

        total = round(inv.total, 2)
        subtotal = round(inv.subtotal, 2)
        tax = round(inv.tax or 0.0, 2)
        if total <= 0:
            return None

        # Dr Accounts Receivable (1200) for total
        # Cr Sales Revenue (4010) for subtotal
        # Cr GST Output Payable (2100) for tax
        lines = [
            {"account_code": "1200", "debit": total, "credit": 0.0, "description": f"Invoice {inv.invoice_number}"},
            {"account_code": "4010", "debit": 0.0, "credit": subtotal, "description": f"Invoice {inv.invoice_number} Revenue"},
        ]
        if tax > 0:
            lines.append({"account_code": "2100", "debit": 0.0, "credit": tax, "description": f"Invoice {inv.invoice_number} Output GST"})

        return create_journal_entry(
            db=db,
            org_id=org_id,
            effective_date=inv.invoice_date,
            lines=lines,
            entry_type="invoice",
            reference=inv.invoice_number,
            source_type="invoice",
            source_id=inv.id,
            description=f"Sales Invoice {inv.invoice_number}",
        )

    elif document_type == "customer_payment":
        pmt = db.get(CustomerPayment, document_id)
        if not pmt or pmt.organization_id != org_id or pmt.amount <= 0:
            return None

        amt = round(pmt.amount, 2)
        # Payment mode mapping: cash -> 1010, bank/online/cheque -> 1020
        bank_code = "1010" if (pmt.payment_mode or "").lower() == "cash" else "1020"

        # Dr Cash / Bank (1010 / 1020)
        # Cr Accounts Receivable (1200)
        lines = [
            {"account_code": bank_code, "debit": amt, "credit": 0.0, "description": f"Receipt {pmt.reference}"},
            {"account_code": "1200", "debit": 0.0, "credit": amt, "description": f"Customer Receipt {pmt.reference}"},
        ]

        pmt_date = pmt.payment_date if hasattr(pmt, "payment_date") and pmt.payment_date else datetime.now(timezone.utc)
        return create_journal_entry(
            db=db,
            org_id=org_id,
            effective_date=pmt_date,
            lines=lines,
            entry_type="customer_payment",
            reference=pmt.reference,
            source_type="customer_payment",
            source_id=pmt.id,
            description=f"Customer Payment {pmt.reference}",
        )

    elif document_type == "sales_return":
        sr = db.get(SalesReturn, document_id)
        if not sr or sr.organization_id != org_id or sr.return_status not in ("approved", "completed", "received"):
            return None

        amt = round(sr.credit_amount or 0.0, 2)
        if amt <= 0:
            return None

        # Dr Sales Returns & Allowances (4020)
        # Cr Accounts Receivable (1200)
        lines = [
            {"account_code": "4020", "debit": amt, "credit": 0.0, "description": f"Sales Return {sr.return_number}"},
            {"account_code": "1200", "debit": 0.0, "credit": amt, "description": f"Credit Note {sr.return_number}"},
        ]

        return create_journal_entry(
            db=db,
            org_id=org_id,
            effective_date=sr.return_date,
            lines=lines,
            entry_type="sales_return",
            reference=sr.return_number,
            source_type="sales_return",
            source_id=sr.id,
            description=f"Sales Return {sr.return_number}",
        )

    elif document_type == "expense":
        exp = db.get(Expense, document_id)
        if not exp or exp.organization_id != org_id or exp.status != "approved" or exp.amount <= 0:
            return None

        amt = round(exp.amount, 2)
        # Approved but not yet reimbursed: the money is owed, not paid -> Cr Accounts
        # Payable (2010); POST /expenses/{id}/reimburse later settles it
        # ("expense_reimbursement" below). Already reimbursed when approved (an Admin
        # recording an expense that was paid up front): Cr Cash/Bank directly.
        if exp.reimbursed_at is not None:
            offset_code = _cash_or_bank_code(exp.reimbursement_method or exp.payment_mode)
        else:
            offset_code = "2010"

        # Dr Operating Expenses (6010)
        # Cr Cash / Bank / AP
        lines = [
            {"account_code": "6010", "debit": amt, "credit": 0.0, "description": f"Expense: {exp.category}"},
            {"account_code": offset_code, "debit": 0.0, "credit": amt, "description": f"Expense Payment: {exp.category}"},
        ]

        return create_journal_entry(
            db=db,
            org_id=org_id,
            effective_date=exp.expense_date,
            lines=lines,
            entry_type="expense",
            reference=exp.expense_number or exp.category,
            source_type="expense",
            source_id=exp.id,
            description=f"Expense {exp.category}",
        )

    elif document_type == "expense_reimbursement":
        exp = db.get(Expense, document_id)
        if not exp or exp.organization_id != org_id or exp.status != "approved" or exp.reimbursed_at is None:
            return None
        # Settle only what approval accrued to Payable. An expense approved before
        # accrual existed was posted straight to Cash/Bank, so paying it again here
        # would take the cash out twice.
        approval = (
            db.query(JournalEntry)
            .filter(
                JournalEntry.organization_id == org_id,
                JournalEntry.source_type == "expense",
                JournalEntry.source_id == exp.id,
            )
            .first()
        )
        payable = accounts.get("2010")
        if approval is None or payable is None or not any(
            line.account_id == payable.id and (line.credit or 0) > 0 for line in approval.lines
        ):
            return None
        amt = round(exp.amount, 2)
        return create_journal_entry(
            db=db,
            org_id=org_id,
            effective_date=exp.reimbursed_at,
            lines=[
                {"account_code": "2010", "debit": amt, "credit": 0.0, "description": f"Expense reimbursed: {exp.category}"},
                {"account_code": _cash_or_bank_code(exp.reimbursement_method), "debit": 0.0, "credit": amt,
                 "description": f"Expense reimbursement paid ({exp.reimbursement_method})"},
            ],
            entry_type="expense",
            reference=exp.expense_number or exp.category,
            source_type="expense_reimbursement",
            source_id=exp.id,
            description=f"Expense {exp.expense_number or exp.category} reimbursed",
        )

    return None


def _cash_or_bank_code(method: str | None) -> str:
    """Cash on Hand (1010) for cash, the Operating Bank Account (1020) otherwise."""
    return "1010" if (method or "").strip().lower() == "cash" else "1020"
