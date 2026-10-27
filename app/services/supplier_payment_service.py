from datetime import datetime, timezone
from fastapi import HTTPException, status
from sqlalchemy import func, or_
from sqlalchemy.orm import Session, lazyload

from app.core.sorting import apply_sorting
from app.models.purchase_invoice import PurchaseInvoice
from app.models.supplier import Supplier, SupplierPayment
from app.models.supplier_invoice import SupplierInvoice
from app.models.supplier_payment_allocation import SupplierPaymentAllocation
from app.schemas.supplier_payment import (
    InvoicePaymentAllocationOut,
    PaymentAllocationOut,
    SupplierPaymentCreate,
    SupplierPaymentListResponse,
    SupplierPaymentOut,
)
from app.services import numbering_service

SUPPLIER_PAYMENT_SORTABLE_FIELDS = {
    "payment_number": SupplierPayment.payment_number,
    "reference": SupplierPayment.reference,
    "supplier": Supplier.name,
    "supplier_name": Supplier.name,
    "paid_on": SupplierPayment.paid_on,
    "payment_date": SupplierPayment.paid_on,
    "date": SupplierPayment.paid_on,
    "amount": SupplierPayment.amount,
    "unallocated_amount": SupplierPayment.unallocated_amount,
    "outstanding_amount": SupplierPayment.unallocated_amount,
    "outstanding": SupplierPayment.unallocated_amount,
    "payment_method": SupplierPayment.payment_method,
    "payment_mode": SupplierPayment.payment_mode,
    "status": SupplierPayment.status,
    "created_at": SupplierPayment.created_at,
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def recalculate_supplier_invoice(db: Session, invoice: SupplierInvoice) -> None:
    """Recalculate SupplierInvoice.amount_paid and payment_status from active allocations and returns."""
    allocations = (
        db.query(SupplierPaymentAllocation)
        .join(SupplierPayment, SupplierPayment.id == SupplierPaymentAllocation.supplier_payment_id)
        .filter(
            SupplierPaymentAllocation.supplier_invoice_id == invoice.id,
            SupplierPayment.status == "recorded",
        )
        .all()
    )
    total_paid = round(sum(alloc.amount for alloc in allocations), 2)
    invoice.amount_paid = total_paid

    invoice.payment_status = _payment_status(total_paid, invoice.return_amount, invoice.grand_total)

    if invoice.purchase_id:
        db.flush()  # the purchase sum below must see this invoice's new figure
        purchase = db.get(PurchaseInvoice, invoice.purchase_id)
        if purchase:
            recalculate_purchase_payment(db, purchase)


def _payment_status(paid: float, returned: float | None, total: float | None) -> str:
    settled = round((paid or 0.0) + (returned or 0.0), 2)
    if settled <= 0:
        return "unpaid"
    if settled + 0.01 >= (total or 0.0) and (total or 0.0) > 0:
        return "paid"
    return "partially_paid"


def purchase_paid_from_invoices(db: Session, purchase: PurchaseInvoice) -> float:
    """The canonical amount paid on a purchase: the sum of what its non-cancelled supplier
    invoices have been paid. Each invoice's figure is in turn the sum of its allocations from
    recorded (non-voided) supplier payments, so a payment counts exactly once, however many
    invoices the purchase has."""
    paid = (
        db.query(func.coalesce(func.sum(SupplierInvoice.amount_paid), 0.0))
        .filter(
            SupplierInvoice.organization_id == purchase.organization_id,
            SupplierInvoice.purchase_id == purchase.id,
            SupplierInvoice.status != "cancelled",
        )
        .scalar()
    )
    return round(float(paid or 0.0), 2)


def recalculate_purchase_payment(db: Session, purchase: PurchaseInvoice) -> None:
    """Refresh the purchase's stored payment summary (amount_paid / payment_status) from its
    supplier invoices -- the only place those two columns are written. They are a cache of
    the supplier payment allocations, never an independent record of payment."""
    purchase.amount_paid = purchase_paid_from_invoices(db, purchase)
    purchase.payment_status = _payment_status(purchase.amount_paid, purchase.return_amount, purchase.total)


def recalculate_supplier_total_paid(db: Session, supplier: Supplier) -> None:
    """Recalculate Supplier.total_paid from active SupplierPayment records."""
    payments = (
        db.query(SupplierPayment)
        .filter(
            SupplierPayment.supplier_id == supplier.id,
            SupplierPayment.organization_id == supplier.organization_id,
            SupplierPayment.status == "recorded",
        )
        .all()
    )
    supplier.total_paid = round(sum(p.amount for p in payments), 2)


def build_payment_out(payment: SupplierPayment) -> SupplierPaymentOut:
    """Map SupplierPayment ORM object to Pydantic SupplierPaymentOut schema."""
    allocations_out: list[PaymentAllocationOut] = []
    for alloc in payment.allocations:
        inv = alloc.supplier_invoice
        allocations_out.append(
            PaymentAllocationOut(
                id=alloc.id,
                organization_id=alloc.organization_id,
                supplier_payment_id=alloc.supplier_payment_id,
                supplier_invoice_id=alloc.supplier_invoice_id,
                supplier_invoice_number=inv.supplier_invoice_number if inv else None,
                invoice_grand_total=inv.grand_total if inv else None,
                amount=alloc.amount,
                created_at=alloc.created_at,
            )
        )

    supplier_name = payment.supplier.name if payment.supplier else "Unknown Supplier"
    payment_num = payment.payment_number or f"SPAY-{payment.id[:8]}"

    return SupplierPaymentOut(
        id=payment.id,
        organization_id=payment.organization_id,
        payment_number=payment_num,
        supplier_id=payment.supplier_id,
        supplier_name=supplier_name,
        payment_date=payment.paid_on,
        amount=payment.amount,
        payment_method=payment.payment_method or payment.payment_mode or "cash",
        payment_mode=payment.payment_mode or payment.payment_method or "cash",
        reference=payment.reference,
        notes=payment.note,
        status=payment.status,
        allocated_amount=payment.allocated_amount,
        unallocated_amount=payment.unallocated_amount,
        created_by=payment.created_by,
        voided_by=payment.voided_by,
        voided_at=payment.voided_at,
        void_reason=payment.void_reason,
        created_at=payment.created_at,
        allocations=allocations_out,
    )


def record_supplier_payment(
    db: Session,
    org_id: str,
    payload: SupplierPaymentCreate,
    user_id: str | None = None,
) -> SupplierPaymentOut:
    """Record a new Supplier Payment with optional invoice allocations in ONE transaction."""
    try:
        if payload.amount <= 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Payment amount must be greater than zero",
            )

        supplier = (
            db.query(Supplier)
            .filter(Supplier.id == payload.supplier_id, Supplier.organization_id == org_id)
            .with_for_update()
            .first()
        )
        if supplier is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Supplier not found in your organization",
            )

        alloc_sum = round(sum(a.amount for a in payload.allocations), 2)
        if alloc_sum > round(payload.amount, 2):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Total allocated amount ({alloc_sum}) cannot exceed payment amount ({payload.amount})",
            )

        # Validate allocations and lock target SupplierInvoice rows
        inv_map: dict[str, SupplierInvoice] = {}
        if payload.allocations:
            target_inv_ids = sorted(list({a.supplier_invoice_id for a in payload.allocations}))
            
            # Concurrency safety: pessimistic row locking ordered by primary key
            invoices = (
                db.query(SupplierInvoice)
                .filter(
                    SupplierInvoice.organization_id == org_id,
                    SupplierInvoice.id.in_(target_inv_ids),
                )
                .options(lazyload("*"))  # no eager OUTER JOINs in FOR UPDATE (Postgres rejects them)
                .with_for_update()
                .all()
            )
            inv_map = {inv.id: inv for inv in invoices}

            # Check for missing invoices
            for inv_id in target_inv_ids:
                if inv_id not in inv_map:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=f"Supplier invoice {inv_id} not found",
                    )

            # Validate allocation constraints per invoice
            allocations_per_inv: dict[str, float] = {}
            for alloc_in in payload.allocations:
                if alloc_in.amount <= 0:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail="Allocation amount must be greater than zero",
                    )

                inv = inv_map[alloc_in.supplier_invoice_id]

                if inv.supplier_id != payload.supplier_id:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=f"Invoice {inv.supplier_invoice_number} does not belong to supplier {supplier.name}",
                    )

                if inv.status != "recorded":
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=f"Cannot allocate payment to invoice {inv.supplier_invoice_number} with status '{inv.status}'. Invoice must be 'recorded'.",
                    )

                allocations_per_inv[inv.id] = round(allocations_per_inv.get(inv.id, 0.0) + alloc_in.amount, 2)

            for inv_id, req_alloc in allocations_per_inv.items():
                inv = inv_map[inv_id]
                recalculate_supplier_invoice(db, inv)  # Fresh accurate state inside lock
                current_outstanding = inv.outstanding_amount

                if req_alloc > current_outstanding + 0.01:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=f"Allocation amount ({req_alloc}) exceeds outstanding balance ({current_outstanding}) for invoice {inv.supplier_invoice_number}",
                    )

        # Generate sequential payment number SPAY-YYYY-1001
        payment_num = numbering_service.next_number(db, org_id, SupplierPayment.payment_number, "SPAY")
        method = payload.payment_method or "cash"

        payment = SupplierPayment(
            organization_id=org_id,
            supplier_id=supplier.id,
            payment_number=payment_num,
            amount=round(payload.amount, 2),
            payment_method=method,
            payment_mode=method,
            reference=payload.reference,
            note=payload.notes,
            paid_on=payload.payment_date or _now(),
            status="recorded",
            allocated_amount=alloc_sum,
            unallocated_amount=round(payload.amount - alloc_sum, 2),
            created_by=user_id,
        )
        db.add(payment)
        db.flush()

        # Create allocations
        for alloc_in in payload.allocations:
            alloc_row = SupplierPaymentAllocation(
                organization_id=org_id,
                supplier_payment_id=payment.id,
                supplier_invoice_id=alloc_in.supplier_invoice_id,
                amount=round(alloc_in.amount, 2),
            )
            db.add(alloc_row)
        db.flush()

        # Recalculate balances for all affected invoices
        for inv in inv_map.values():
            recalculate_supplier_invoice(db, inv)

        # Recalculate Supplier total_paid
        recalculate_supplier_total_paid(db, supplier)

        db.commit()
        db.refresh(payment)

        return build_payment_out(payment)
    except Exception:
        db.rollback()
        raise


def void_supplier_payment(
    db: Session,
    org_id: str,
    payment_id: str,
    reason: str,
    user_id: str | None = None,
) -> SupplierPaymentOut:
    """Void an existing Supplier Payment and reverse invoice allocations atomically."""
    try:
        payment = (
            db.query(SupplierPayment)
            .filter(SupplierPayment.id == payment_id, SupplierPayment.organization_id == org_id)
            .options(lazyload("*"))  # no eager OUTER JOINs in FOR UPDATE (Postgres rejects them)
            .with_for_update()
            .first()
        )
        if payment is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Supplier payment not found",
            )

        if payment.status == "voided":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Supplier payment is already voided",
            )

        supplier = (
            db.query(Supplier)
            .filter(Supplier.id == payment.supplier_id, Supplier.organization_id == org_id)
            .with_for_update()
            .first()
        )

        # Collect linked invoice IDs
        linked_inv_ids = sorted(list({alloc.supplier_invoice_id for alloc in payment.allocations}))
        invoices: list[SupplierInvoice] = []
        if linked_inv_ids:
            invoices = (
                db.query(SupplierInvoice)
                .filter(
                    SupplierInvoice.organization_id == org_id,
                    SupplierInvoice.id.in_(linked_inv_ids),
                )
                .options(lazyload("*"))  # no eager OUTER JOINs in FOR UPDATE (Postgres rejects them)
                .with_for_update()
                .all()
            )

        payment.status = "voided"
        payment.voided_at = _now()
        payment.voided_by = user_id
        payment.void_reason = reason
        db.flush()

        # Recalculate affected invoices
        for inv in invoices:
            recalculate_supplier_invoice(db, inv)

        # Recalculate Supplier total_paid
        if supplier:
            recalculate_supplier_total_paid(db, supplier)

        db.commit()
        db.refresh(payment)

        return build_payment_out(payment)
    except Exception:
        db.rollback()
        raise


def get_supplier_payment_detail(
    db: Session,
    org_id: str,
    payment_id: str,
) -> SupplierPaymentOut:
    """Get single supplier payment by ID with allocations."""
    payment = db.get(SupplierPayment, payment_id)
    if payment is None or payment.organization_id != org_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Supplier payment not found",
        )

    return build_payment_out(payment)


def list_supplier_payments(
    db: Session,
    org_id: str,
    supplier_id: str | None = None,
    payment_status: str | None = None,
    payment_method: str | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    search: str | None = None,
    page: int | None = None,
    page_size: int | None = None,
    sort_by: str | None = None,
    sort_order: str = "asc",
) -> SupplierPaymentListResponse:
    """List tenant supplier payments with filtering and pagination."""
    q = (
        db.query(SupplierPayment)
        .join(Supplier, Supplier.id == SupplierPayment.supplier_id)
        .filter(SupplierPayment.organization_id == org_id)
    )

    if supplier_id:
        q = q.filter(SupplierPayment.supplier_id == supplier_id)
    if payment_status:
        q = q.filter(SupplierPayment.status == payment_status.lower())
    if payment_method:
        q = q.filter(
            or_(
                SupplierPayment.payment_method == payment_method.lower(),
                SupplierPayment.payment_mode == payment_method.lower(),
            )
        )
    if date_from:
        q = q.filter(SupplierPayment.paid_on >= _aware(date_from))
    if date_to:
        q = q.filter(SupplierPayment.paid_on <= _aware(date_to))
    if search:
        s = f"%{search}%"
        q = q.filter(
            or_(
                SupplierPayment.payment_number.ilike(s),
                SupplierPayment.reference.ilike(s),
                Supplier.name.ilike(s),
            )
        )

    total_count = q.count()
    q = apply_sorting(
        q,
        sort_by=sort_by,
        sort_order=sort_order,
        sortable_fields=SUPPLIER_PAYMENT_SORTABLE_FIELDS,
        default_order=[SupplierPayment.paid_on.desc(), SupplierPayment.created_at.desc(), SupplierPayment.id.desc()],
        secondary_key=SupplierPayment.id.desc(),
    )

    if page is not None and page_size is not None:
        offset = (page - 1) * page_size
        payments = q.offset(offset).limit(page_size).all()
    else:
        payments = q.all()

    items = [build_payment_out(p) for p in payments]
    return SupplierPaymentListResponse(items=items, total=total_count, page=page, page_size=page_size)


def get_invoice_payment_history(
    db: Session,
    org_id: str,
    supplier_invoice_id: str,
) -> list[InvoicePaymentAllocationOut]:
    """Get payment allocations linked to a specific SupplierInvoice."""
    inv = db.get(SupplierInvoice, supplier_invoice_id)
    if inv is None or inv.organization_id != org_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Supplier invoice not found",
        )

    allocations = (
        db.query(SupplierPaymentAllocation)
        .join(SupplierPayment, SupplierPayment.id == SupplierPaymentAllocation.supplier_payment_id)
        .filter(
            SupplierPaymentAllocation.supplier_invoice_id == supplier_invoice_id,
            SupplierPaymentAllocation.organization_id == org_id,
        )
        .order_by(SupplierPaymentAllocation.created_at.desc())
        .all()
    )

    out: list[InvoicePaymentAllocationOut] = []
    for alloc in allocations:
        p = alloc.supplier_payment
        out.append(
            InvoicePaymentAllocationOut(
                allocation_id=alloc.id,
                supplier_payment_id=p.id,
                payment_number=p.payment_number,
                payment_date=p.paid_on,
                payment_method=p.payment_method or p.payment_mode or "cash",
                payment_status=p.status,
                status=p.status,
                voided_at=p.voided_at,
                void_reason=p.void_reason,
                reference=p.reference,
                allocated_amount=alloc.amount,
                created_at=alloc.created_at,
            )
        )
    return out


# --------------------------------------------------------------------------------------
# Paying a purchase, and reconciling legacy purchase payment summaries
# --------------------------------------------------------------------------------------


def pay_purchase(
    db: Session,
    org_id: str,
    purchase: PurchaseInvoice,
    amount: float,
    payment_method: str | None = None,
    reference: str | None = None,
    user_id: str | None = None,
) -> SupplierPaymentOut:
    """Record a real Supplier Payment of `amount` for a purchase, allocated to its recorded
    supplier invoices oldest first. Goes through record_supplier_payment, so every rule of
    the canonical payment flow (locks, outstanding checks, totals) applies unchanged."""
    if not purchase.supplier_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This purchase has no supplier, so no supplier payment can be recorded against it",
        )
    invoices = (
        db.query(SupplierInvoice)
        .filter(
            SupplierInvoice.organization_id == org_id,
            SupplierInvoice.purchase_id == purchase.id,
            SupplierInvoice.status == "recorded",
        )
        .order_by(SupplierInvoice.supplier_invoice_date.asc(), SupplierInvoice.created_at.asc())
        .all()
    )
    if not invoices:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This purchase has no recorded supplier invoice yet. Confirm the purchase "
                   "(or record its supplier invoice) before recording a payment.",
        )
    remaining = round(amount, 2)
    allocations = []
    for inv in invoices:
        recalculate_supplier_invoice(db, inv)
        open_amount = round(inv.outstanding_amount, 2)
        if open_amount <= 0 or remaining <= 0:
            continue
        portion = min(open_amount, remaining)
        allocations.append({"supplier_invoice_id": inv.id, "amount": portion})
        remaining = round(remaining - portion, 2)
    if remaining > 0.01:
        outstanding = round(sum(a["amount"] for a in allocations), 2)
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Payment ({round(amount, 2)}) exceeds the purchase's outstanding balance ({outstanding})",
        )
    payload = SupplierPaymentCreate(
        supplier_id=purchase.supplier_id,
        amount=round(amount, 2),
        payment_method=payment_method or "cash",
        reference=reference,
        notes=f"Recorded from purchase {purchase.purchase_number or purchase.invoice_number}",
        allocations=allocations,
    )
    return record_supplier_payment(db, org_id, payload, user_id=user_id)


def reconcile_purchase_payments(db: Session, org_id: str | None = None, apply: bool = False) -> list[dict]:
    """Compare stored payment summaries with what the supplier payment allocations say.

    Reports every supplier invoice whose stored amount_paid differs from its allocations,
    and every purchase whose stored amount_paid / payment_status differs from its supplier
    invoices. With `apply=True` the stored figures are recalculated from allocations and
    each change is written to the activity log. It never creates or deletes a payment:
    an amount recorded only on the old purchase-level fields, with no supplier payment
    behind it, is reported so the team can record the real payment if one was made.
    """
    from app.services import activity_service

    findings: list[dict] = []
    inv_q = db.query(SupplierInvoice).filter(SupplierInvoice.status != "cancelled")
    pur_q = db.query(PurchaseInvoice)
    if org_id:
        inv_q = inv_q.filter(SupplierInvoice.organization_id == org_id)
        pur_q = pur_q.filter(PurchaseInvoice.organization_id == org_id)

    # 1. Canonical figures straight from the ledger, before anything is written, so a dry run
    #    and an apply report exactly the same findings.
    allocated_by_invoice = dict(
        db.query(SupplierPaymentAllocation.supplier_invoice_id, func.coalesce(func.sum(SupplierPaymentAllocation.amount), 0.0))
        .join(SupplierPayment, SupplierPayment.id == SupplierPaymentAllocation.supplier_payment_id)
        .filter(SupplierPayment.status == "recorded")
        .group_by(SupplierPaymentAllocation.supplier_invoice_id)
        .all()
    )
    invoices = inv_q.all()
    canonical_invoice = {inv.id: round(float(allocated_by_invoice.get(inv.id, 0.0) or 0.0), 2) for inv in invoices}
    paid_by_purchase: dict[str, float] = {}
    for inv in invoices:
        paid_by_purchase[inv.purchase_id] = round(paid_by_purchase.get(inv.purchase_id, 0.0) + canonical_invoice[inv.id], 2)
    purchases_with_invoice = set(paid_by_purchase)

    stale_invoices = [inv for inv in invoices if abs(round(inv.amount_paid or 0.0, 2) - canonical_invoice[inv.id]) > 0.005]
    for inv in stale_invoices:
        findings.append({
            "kind": "supplier_invoice", "organization_id": inv.organization_id, "id": inv.id,
            "number": inv.supplier_invoice_number, "stored_amount_paid": round(inv.amount_paid or 0.0, 2),
            "canonical_amount_paid": canonical_invoice[inv.id],
        })
    stale_purchases = []
    for purchase in pur_q.all():
        canonical_paid = paid_by_purchase.get(purchase.id, 0.0)
        canonical_status = _payment_status(canonical_paid, purchase.return_amount, purchase.total)
        stored_paid = round(purchase.amount_paid or 0.0, 2)
        if abs(stored_paid - canonical_paid) > 0.005 or purchase.payment_status != canonical_status:
            stale_purchases.append(purchase)
            findings.append({
                "kind": "purchase", "organization_id": purchase.organization_id, "id": purchase.id,
                "number": purchase.purchase_number or purchase.invoice_number,
                "stored_amount_paid": stored_paid, "stored_payment_status": purchase.payment_status,
                "canonical_amount_paid": canonical_paid, "canonical_payment_status": canonical_status,
                "has_supplier_invoice": purchase.id in purchases_with_invoice,
            })

    # 2. Apply: recalculate from the ledger (never create or remove a payment) and log each change.
    if apply:
        for inv in stale_invoices:
            old = inv.amount_paid
            recalculate_supplier_invoice(db, inv)
            activity_service.record(
                db, inv.organization_id, None, "supplier_invoice", "Payment summary reconciled",
                f"Supplier invoice {inv.supplier_invoice_number}: stored amount paid {old} -> {inv.amount_paid} "
                "(from recorded supplier payments)",
            )
        db.flush()
        for purchase in stale_purchases:
            old = next((f["stored_amount_paid"], f["stored_payment_status"]) for f in findings
                       if f["kind"] == "purchase" and f["id"] == purchase.id)
            recalculate_purchase_payment(db, purchase)
            activity_service.record(
                db, purchase.organization_id, None, "purchase", "Payment summary reconciled",
                f"Purchase {purchase.purchase_number or purchase.invoice_number}: stored {old[0]} ({old[1]}) -> "
                f"{purchase.amount_paid} ({purchase.payment_status}) from supplier payments",
            )
        db.commit()
    return findings
