from datetime import datetime, timezone
from fastapi import HTTPException, status
from sqlalchemy import or_
from sqlalchemy.orm import Session

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


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def recalculate_supplier_invoice(db: Session, invoice: SupplierInvoice) -> None:
    """Recalculate SupplierInvoice.amount_paid and payment_status from active allocations."""
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

    if total_paid <= 0:
        invoice.payment_status = "unpaid"
    elif total_paid + 0.01 >= invoice.grand_total:
        invoice.payment_status = "paid"
    else:
        invoice.payment_status = "partially_paid"


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
    q = q.order_by(SupplierPayment.paid_on.desc(), SupplierPayment.created_at.desc())

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
                reference=p.reference,
                allocated_amount=alloc.amount,
                created_at=alloc.created_at,
            )
        )
    return out
