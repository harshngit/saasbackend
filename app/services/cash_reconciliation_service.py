from datetime import date, datetime, time, timezone
from decimal import Decimal
import logging
from zoneinfo import ZoneInfo

from fastapi import HTTPException, status
from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import (
    CustomerPayment,
    Expense,
    Organization,
    PaymentSplit,
    SupplierPayment,
    User,
)
from app.models.cash_reconciliation import CashReconciliation, CashReconciliationDetail
from app.schemas.cash_reconciliation import (
    CashReconciliationCloseIn,
    CashReconciliationCreate,
    CashReconciliationUpdate,
)
from app.services import activity_service
from app.services.numbering_service import DEFAULT_BUSINESS_TIMEZONE
from app.services.report_service import get_org_timezone

logger = logging.getLogger("crm.cash_reconciliation")

CANONICAL_METHODS = ["cash", "upi", "card", "bank_transfer", "cheque", "cod"]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def normalize_payment_method(mode: str | None) -> str:
    """Normalize a payment method string to a consistent lowercase key."""
    if not mode:
        return "cash"
    s = mode.strip().lower()
    if "razorpay" in s:
        return "razorpay"
    if s in ("bank transfer", "bank_transfer", "neft", "rtgs", "imps", "wire"):
        return "bank_transfer"
    if s in ("credit card", "debit card", "card"):
        return "card"
    if s in ("upi", "gpay", "phonepe", "paytm"):
        return "upi"
    if s in ("cash on delivery", "cod"):
        return "cod"
    if s in ("cheque", "check"):
        return "cheque"
    if s == "cash":
        return "cash"
    return s.replace(" ", "_")


def get_business_day_utc_range(db: Session, org_id: str, target_date: date) -> tuple[datetime, datetime]:
    """Convert a business date (local to org timezone) into UTC start and end bounds."""
    tz = get_org_timezone(db, org_id) if (db and org_id) else ZoneInfo(DEFAULT_BUSINESS_TIMEZONE)
    start_dt = datetime.combine(target_date, time.min).replace(tzinfo=tz).astimezone(timezone.utc)
    end_dt = datetime.combine(target_date, time.max).replace(tzinfo=tz).astimezone(timezone.utc)
    return start_dt, end_dt


def calculate_expected_figures(db: Session, org_id: str, target_date: date) -> dict:
    """Calculate expected collections and cash movements for a business date."""
    start_dt, end_dt = get_business_day_utc_range(db, org_id, target_date)

    # 1. Customer Payments (Collections)
    payments = (
        db.query(CustomerPayment)
        .filter(
            CustomerPayment.organization_id == org_id,
            CustomerPayment.received_on >= start_dt,
            CustomerPayment.received_on <= end_dt,
        )
        .all()
    )

    method_totals: dict[str, Decimal] = {m: Decimal("0.00") for m in CANONICAL_METHODS}
    cash_sales = Decimal("0.00")
    cash_received = Decimal("0.00")

    for p in payments:
        # Check splits first
        if p.splits and len(p.splits) > 0:
            for s in p.splits:
                m = normalize_payment_method(s.payment_mode)
                amt = Decimal(str(round(s.amount, 2)))
                method_totals[m] = method_totals.get(m, Decimal("0.00")) + amt
                if m == "cash":
                    if p.customer_id is None:
                        cash_sales += amt
                    else:
                        cash_received += amt
        else:
            m = normalize_payment_method(p.payment_mode)
            amt = Decimal(str(round(p.amount, 2)))
            method_totals[m] = method_totals.get(m, Decimal("0.00")) + amt
            if m == "cash":
                if p.customer_id is None:
                    cash_sales += amt
                else:
                    cash_received += amt

    total_expected_collections = sum(method_totals.values())

    # 2. Supplier Cash Payments (Outflow)
    supplier_payments = (
        db.query(SupplierPayment)
        .filter(
            SupplierPayment.organization_id == org_id,
            SupplierPayment.paid_on >= start_dt,
            SupplierPayment.paid_on <= end_dt,
            SupplierPayment.status != "voided",
        )
        .all()
    )
    cash_paid = Decimal("0.00")
    for sp in supplier_payments:
        mode = normalize_payment_method(sp.payment_mode or sp.payment_method)
        if mode == "cash":
            cash_paid += Decimal(str(round(sp.amount, 2)))

    # 3. Approved Cash Expenses (Outflow)
    expenses = (
        db.query(Expense)
        .filter(
            Expense.organization_id == org_id,
            Expense.expense_date >= start_dt,
            Expense.expense_date <= end_dt,
            Expense.status == "approved",
        )
        .all()
    )
    expenses_paid = Decimal("0.00")
    for exp in expenses:
        mode = normalize_payment_method(exp.payment_mode)
        if mode == "cash":
            expenses_paid += Decimal(str(round(exp.amount, 2)))

    return {
        "method_totals": method_totals,
        "total_expected_collections": total_expected_collections,
        "cash_sales": cash_sales,
        "cash_received": cash_received,
        "cash_paid": cash_paid,
        "expenses_paid": expenses_paid,
    }


def create_draft(
    db: Session,
    org_id: str,
    user: User,
    payload: CashReconciliationCreate,
) -> CashReconciliation:
    """Create a new draft Cash Reconciliation session."""
    target_date = payload.reconciliation_date
    if not target_date:
        tz = get_org_timezone(db, org_id)
        target_date = datetime.now(tz).date()

    # Verify no existing reconciliation exists for this date
    existing = (
        db.query(CashReconciliation)
        .filter(
            CashReconciliation.organization_id == org_id,
            CashReconciliation.reconciliation_date == target_date,
        )
        .first()
    )
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"A cash reconciliation already exists for {target_date.isoformat()}",
        )

    expected = calculate_expected_figures(db, org_id, target_date)
    method_totals = expected["method_totals"]

    opening_bal = Decimal(str(round(payload.opening_balance, 2)))
    actual_cash = Decimal(str(round(payload.actual_cash_on_hand, 2)))

    rec = CashReconciliation(
        organization_id=org_id,
        reconciliation_date=target_date,
        status="draft",
        opening_balance=opening_bal,
        cash_sales=expected["cash_sales"],
        cash_received=expected["cash_received"],
        cash_paid=expected["cash_paid"],
        expenses_paid=expected["expenses_paid"],
        expected_closing_cash=opening_bal + expected["cash_sales"] + expected["cash_received"] - expected["cash_paid"] - expected["expenses_paid"],
        actual_cash_on_hand=actual_cash,
        cash_balance_variance=actual_cash - (opening_bal + expected["cash_sales"] + expected["cash_received"] - expected["cash_paid"] - expected["expenses_paid"]),
        variance_reason=payload.variance_reason,
        notes=payload.notes,
        created_by_id=user.id,
    )
    db.add(rec)
    db.flush()

    # Map actual amounts provided in payload
    actual_map: dict[str, tuple[Decimal, str | None]] = {}
    if payload.details:
        for d in payload.details:
            m = normalize_payment_method(d.payment_method)
            actual_map[m] = (Decimal(str(round(d.actual_amount, 2))), d.notes)

    # If actual_cash_on_hand was supplied but no cash detail was in payload, set cash detail
    if "cash" not in actual_map and actual_cash > Decimal("0.00"):
        actual_map["cash"] = (actual_cash, None)

    # Build method details
    total_actual = Decimal("0.00")
    for method, exp_amt in method_totals.items():
        act_amt, d_notes = actual_map.get(method, (Decimal("0.00"), None))
        total_actual += act_amt
        detail = CashReconciliationDetail(
            reconciliation_id=rec.id,
            organization_id=org_id,
            payment_method=method,
            expected_amount=exp_amt,
            actual_amount=act_amt,
            variance_amount=act_amt - exp_amt,
            notes=d_notes,
        )
        db.add(detail)

    # Any extra methods supplied in payload not in method_totals
    for m, (act_amt, d_notes) in actual_map.items():
        if m not in method_totals:
            total_actual += act_amt
            detail = CashReconciliationDetail(
                reconciliation_id=rec.id,
                organization_id=org_id,
                payment_method=m,
                expected_amount=Decimal("0.00"),
                actual_amount=act_amt,
                variance_amount=act_amt,
                notes=d_notes,
            )
            db.add(detail)

    rec.total_expected_collections = expected["total_expected_collections"]
    rec.total_actual_collections = total_actual
    rec.collections_variance = total_actual - rec.total_expected_collections

    activity_service.record(
        db,
        org_id,
        user,
        "finance",
        f"Cash reconciliation draft created for {target_date.isoformat()}",
    )
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        # Verify if conflict was due to duplicate date for this org
        existing_conflict = (
            db.query(CashReconciliation.id)
            .filter(
                CashReconciliation.organization_id == org_id,
                CashReconciliation.reconciliation_date == target_date,
            )
            .first()
        )
        if existing_conflict or "uq_cash_reconciliation_org_date" in str(exc).lower() or "unique constraint" in str(exc).lower():
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"A cash reconciliation already exists for {target_date.isoformat()}",
            )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Database integrity conflict during reconciliation draft creation",
        )

    db.refresh(rec)
    return rec


def refresh_draft_if_needed(db: Session, reconciliation: CashReconciliation) -> None:
    """Recalculate expected figures for a draft reconciliation against live payment data."""
    if reconciliation.status != "draft":
        return

    expected = calculate_expected_figures(
        db, reconciliation.organization_id, reconciliation.reconciliation_date
    )
    method_totals = expected["method_totals"]

    reconciliation.cash_sales = expected["cash_sales"]
    reconciliation.cash_received = expected["cash_received"]
    reconciliation.cash_paid = expected["cash_paid"]
    reconciliation.expenses_paid = expected["expenses_paid"]
    reconciliation.expected_closing_cash = (
        reconciliation.opening_balance
        + expected["cash_sales"]
        + expected["cash_received"]
        - expected["cash_paid"]
        - expected["expenses_paid"]
    )
    reconciliation.cash_balance_variance = (
        reconciliation.actual_cash_on_hand - reconciliation.expected_closing_cash
    )

    detail_by_method = {d.payment_method: d for d in reconciliation.details}
    total_actual = Decimal("0.00")

    for method, exp_amt in method_totals.items():
        if method in detail_by_method:
            d = detail_by_method[method]
            d.expected_amount = exp_amt
            d.variance_amount = d.actual_amount - exp_amt
            total_actual += d.actual_amount
        else:
            d = CashReconciliationDetail(
                reconciliation_id=reconciliation.id,
                organization_id=reconciliation.organization_id,
                payment_method=method,
                expected_amount=exp_amt,
                actual_amount=Decimal("0.00"),
                variance_amount=-exp_amt,
            )
            db.add(d)

    for m, d in detail_by_method.items():
        if m not in method_totals:
            d.expected_amount = Decimal("0.00")
            d.variance_amount = d.actual_amount
            total_actual += d.actual_amount

    reconciliation.total_expected_collections = expected["total_expected_collections"]
    reconciliation.total_actual_collections = total_actual
    reconciliation.collections_variance = total_actual - reconciliation.total_expected_collections
    db.commit()
    db.refresh(reconciliation)


def update_draft(
    db: Session,
    reconciliation: CashReconciliation,
    payload: CashReconciliationUpdate,
    user: User,
) -> CashReconciliation:
    """Update editable fields of a draft reconciliation."""
    # Ensure row is locked and verify status under transaction lock
    locked_rec = (
        db.query(CashReconciliation)
        .filter(
            CashReconciliation.id == reconciliation.id,
            CashReconciliation.organization_id == reconciliation.organization_id,
        )
        .with_for_update()
        .first()
    )
    if locked_rec is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Cash reconciliation not found",
        )

    if locked_rec.status != "draft":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot modify a closed reconciliation",
        )

    reconciliation = locked_rec

    # 1. Update basic fields if provided
    if payload.opening_balance is not None:
        reconciliation.opening_balance = Decimal(str(round(payload.opening_balance, 2)))
    if payload.actual_cash_on_hand is not None:
        reconciliation.actual_cash_on_hand = Decimal(str(round(payload.actual_cash_on_hand, 2)))
    if payload.variance_reason is not None:
        reconciliation.variance_reason = payload.variance_reason
    if payload.notes is not None:
        reconciliation.notes = payload.notes

    # 2. Update details if provided
    if payload.details is not None:
        detail_by_method = {d.payment_method: d for d in reconciliation.details}
        for item in payload.details:
            m = normalize_payment_method(item.payment_method)
            act_amt = Decimal(str(round(item.actual_amount, 2)))
            if m in detail_by_method:
                d = detail_by_method[m]
                d.actual_amount = act_amt
                d.variance_amount = act_amt - d.expected_amount
                if item.notes is not None:
                    d.notes = item.notes
            else:
                d = CashReconciliationDetail(
                    reconciliation_id=reconciliation.id,
                    organization_id=reconciliation.organization_id,
                    payment_method=m,
                    expected_amount=Decimal("0.00"),
                    actual_amount=act_amt,
                    variance_amount=act_amt,
                    notes=item.notes,
                )
                db.add(d)

    # Recalculate totals
    refresh_draft_if_needed(db, reconciliation)
    activity_service.record(
        db,
        reconciliation.organization_id,
        user,
        "finance",
        f"Cash reconciliation draft updated for {reconciliation.reconciliation_date.isoformat()}",
    )
    db.commit()
    db.refresh(reconciliation)
    return reconciliation


def close_reconciliation(
    db: Session,
    reconciliation_id: str,
    org_id: str,
    user: User,
    payload: CashReconciliationCloseIn | None = None,
) -> CashReconciliation:
    """Atomically snapshot, lock, and close a reconciliation draft."""
    # Acquire lock on row
    reconciliation = (
        db.query(CashReconciliation)
        .filter(
            CashReconciliation.id == reconciliation_id,
            CashReconciliation.organization_id == org_id,
        )
        .with_for_update()
        .first()
    )
    if reconciliation is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Cash reconciliation not found",
        )

    if reconciliation.status != "draft":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Reconciliation is already closed",
        )

    # Apply any closing updates
    if payload:
        if payload.opening_balance is not None:
            reconciliation.opening_balance = Decimal(str(round(payload.opening_balance, 2)))
        if payload.actual_cash_on_hand is not None:
            reconciliation.actual_cash_on_hand = Decimal(str(round(payload.actual_cash_on_hand, 2)))
        if payload.variance_reason is not None:
            reconciliation.variance_reason = payload.variance_reason
        if payload.notes is not None:
            reconciliation.notes = payload.notes
        if payload.details is not None:
            detail_by_method = {d.payment_method: d for d in reconciliation.details}
            for item in payload.details:
                m = normalize_payment_method(item.payment_method)
                act_amt = Decimal(str(round(item.actual_amount, 2)))
                if m in detail_by_method:
                    d = detail_by_method[m]
                    d.actual_amount = act_amt
                    if item.notes is not None:
                        d.notes = item.notes
                else:
                    d = CashReconciliationDetail(
                        reconciliation_id=reconciliation.id,
                        organization_id=reconciliation.organization_id,
                        payment_method=m,
                        expected_amount=Decimal("0.00"),
                        actual_amount=act_amt,
                        variance_amount=act_amt,
                        notes=item.notes,
                    )
                    db.add(d)

    # Recalculate live expected values for final snapshot
    expected = calculate_expected_figures(
        db, reconciliation.organization_id, reconciliation.reconciliation_date
    )
    method_totals = expected["method_totals"]

    reconciliation.cash_sales = expected["cash_sales"]
    reconciliation.cash_received = expected["cash_received"]
    reconciliation.cash_paid = expected["cash_paid"]
    reconciliation.expenses_paid = expected["expenses_paid"]
    reconciliation.expected_closing_cash = (
        reconciliation.opening_balance
        + expected["cash_sales"]
        + expected["cash_received"]
        - expected["cash_paid"]
        - expected["expenses_paid"]
    )
    reconciliation.cash_balance_variance = (
        reconciliation.actual_cash_on_hand - reconciliation.expected_closing_cash
    )

    detail_by_method = {d.payment_method: d for d in reconciliation.details}
    total_actual = Decimal("0.00")

    for method, exp_amt in method_totals.items():
        if method in detail_by_method:
            d = detail_by_method[method]
            d.expected_amount = exp_amt
            d.variance_amount = d.actual_amount - exp_amt
            total_actual += d.actual_amount
        else:
            d = CashReconciliationDetail(
                reconciliation_id=reconciliation.id,
                organization_id=reconciliation.organization_id,
                payment_method=method,
                expected_amount=exp_amt,
                actual_amount=Decimal("0.00"),
                variance_amount=-exp_amt,
            )
            db.add(d)

    for m, d in detail_by_method.items():
        if m not in method_totals:
            d.expected_amount = Decimal("0.00")
            d.variance_amount = d.actual_amount
            total_actual += d.actual_amount

    reconciliation.total_expected_collections = expected["total_expected_collections"]
    reconciliation.total_actual_collections = total_actual
    reconciliation.collections_variance = total_actual - reconciliation.total_expected_collections

    # Enforce variance reason when any variance is non-zero
    has_variance = (
        reconciliation.collections_variance != Decimal("0.00")
        or reconciliation.cash_balance_variance != Decimal("0.00")
    )
    if has_variance:
        reason = (reconciliation.variance_reason or "").strip()
        notes = (reconciliation.notes or "").strip()
        if not reason and not notes:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="A variance reason is required when variance is non-zero",
            )

    # Transition to closed
    reconciliation.status = "closed"
    reconciliation.closed_by_id = user.id
    reconciliation.closed_at = _now()

    activity_service.record(
        db,
        org_id,
        user,
        "finance",
        f"Cash reconciliation closed for {reconciliation.reconciliation_date.isoformat()}",
        f"Collections variance: {reconciliation.collections_variance}, Cash balance variance: {reconciliation.cash_balance_variance}",
    )
    db.commit()
    db.refresh(reconciliation)
    return reconciliation


def delete_draft(db: Session, reconciliation: CashReconciliation, user: User) -> None:
    """Delete a reconciliation session while in draft state."""
    if reconciliation.status != "draft":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot delete a closed reconciliation",
        )
    date_str = reconciliation.reconciliation_date.isoformat()
    org_id = reconciliation.organization_id
    db.delete(reconciliation)
    activity_service.record(
        db,
        org_id,
        user,
        "finance",
        f"Cash reconciliation draft deleted for {date_str}",
    )
    db.commit()
