"""Centralized Organization Storage Accounting & Quota Enforcement Service.

Single source of truth for:
- Evaluating organization storage usage across files, reports, and application data.
- Recalculating snapshot storage metrics.
- Validating storage quotas before file persistence.
- Safely tracking uploads, deletions, and replacements.
"""

from datetime import datetime, timezone
import logging
from typing import Any, Dict, List, Optional, Tuple

from fastapi import HTTPException, status
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import (
    ActivityLog,
    Attendance,
    Customer,
    Delivery,
    Expense,
    FollowUp,
    Invoice,
    Lead,
    Leave,
    Organization,
    OrganizationStorageUsage,
    Product,
    PurchaseInvoice,
    PurchaseReturn,
    Quotation,
    SalesOrder,
    SalesReturn,
    StockMovement,
    StoredFile,
    Supplier,
    User,
    WarehouseTransfer,
    Visit,
)
from app.services.entitlement_service import get_effective_limit_with_source

logger = logging.getLogger("crm.storage_service")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
BYTES_PER_KB = 1024
BYTES_PER_MB = 1024 * 1024
BYTES_PER_GB = 1024 * 1024 * 1024

# ---------------------------------------------------------------------------
# Centralized Application Data Model Registry
# ---------------------------------------------------------------------------
# Explicitly registered organization-owned root models with their defensive
# logical size allotment per record.
#
# Assumptions:
# - Complex business documents (Invoice, Order, Quotation, PurchaseInvoice, Delivery)
#   allot 4,096 bytes (4 KB) per document to safely subsume child line-item overhead
#   without requiring high-overhead multi-table joins or risking double-counting.
# - Primary master entities (Customer, Product, Supplier, Lead, Transfers, Returns)
#   allot 2,048 bytes (2 KB) per record.
# - Operational logs and transactions (Expense, StockMovement, Attendance, Leave,
#   Visit, FollowUp, User, ActivityLog) allot 512 - 1,024 bytes per record.
# - Global/system models (Plan, Role, SystemSetting) and shared lookups are EXCLUDED.
# - All queries are strictly scoped to the tenant: `model.organization_id == org_id`.
APPLICATION_DATA_REGISTRY: List[Tuple[Any, int, str]] = [
    (Customer, 2048, "Customer entity records"),
    (Product, 2048, "Product catalog records"),
    (Supplier, 2048, "Supplier entity records"),
    (Lead, 2048, "Lead pipeline records"),
    (Invoice, 4096, "Invoices including line-item allotment"),
    (SalesOrder, 4096, "Sales orders including line-item allotment"),
    (Quotation, 4096, "Quotations including line-item allotment"),
    (PurchaseInvoice, 4096, "Purchase invoices including line-item allotment"),
    (Delivery, 4096, "Fulfillment deliveries including line items"),
    (SalesReturn, 2048, "Sales return documents"),
    (PurchaseReturn, 2048, "Purchase return documents"),
    (WarehouseTransfer, 2048, "Warehouse transfer records"),
    (Expense, 1024, "Expense ledger records"),
    (StockMovement, 1024, "Stock movement ledger records"),
    (Attendance, 512, "Staff attendance logs"),
    (Leave, 512, "Leave request records"),
    (Visit, 1024, "Field visit logs"),
    (FollowUp, 512, "CRM follow-up logs"),
    (User, 1024, "Organization user accounts"),
    (ActivityLog, 512, "System audit and activity logs"),
]


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Storage Recalculation & Retrieval
# ---------------------------------------------------------------------------

def recalculate_organization_storage(
    organization_id: str,
    db: Session,
) -> OrganizationStorageUsage:
    """Rebuild authoritative storage metrics from persisted records.

    1. Files & Documents: SUM(StoredFile.size) for organization_id.
    2. Stored Reports: 0 bytes (existing reports/exports are ephemeral).
    3. Application Data: Sum of registered model records * defensive row allotments.
    """
    # 1. Authoritative file bytes
    file_bytes_raw = (
        db.query(func.coalesce(func.sum(StoredFile.size), 0))
        .filter(StoredFile.organization_id == organization_id)
        .scalar()
    )
    files_documents_bytes = int(file_bytes_raw or 0)

    # 2. Ephemeral reports contribute zero
    stored_reports_bytes = 0

    # 3. Application data logical estimation
    app_bytes = 0
    for model_cls, weight_bytes, _ in APPLICATION_DATA_REGISTRY:
        if hasattr(model_cls, "organization_id"):
            count = (
                db.query(func.count(model_cls.id))
                .filter(model_cls.organization_id == organization_id)
                .scalar()
            ) or 0
            app_bytes += int(count) * weight_bytes

    total_used_bytes = files_documents_bytes + stored_reports_bytes + app_bytes

    usage = (
        db.query(OrganizationStorageUsage)
        .filter(OrganizationStorageUsage.organization_id == organization_id)
        .first()
    )
    if not usage:
        try:
            with db.begin_nested():
                usage = OrganizationStorageUsage(organization_id=organization_id)
                db.add(usage)
                db.flush()
        except IntegrityError:
            usage = (
                db.query(OrganizationStorageUsage)
                .filter(OrganizationStorageUsage.organization_id == organization_id)
                .first()
            )

    usage.files_documents_bytes = files_documents_bytes
    usage.stored_reports_bytes = stored_reports_bytes
    usage.application_data_bytes = app_bytes
    usage.total_used_bytes = total_used_bytes
    usage.calculated_at = _now()
    usage.updated_at = _now()
    db.flush()

    return usage


def get_or_create_storage_usage(
    organization_id: str,
    db: Session,
    for_update: bool = False,
) -> OrganizationStorageUsage:
    """Safely fetch or initialize OrganizationStorageUsage snapshot.

    If missing, serializes cold-start initialization using the parent Organization row
    so PostgreSQL can reliably lock against concurrent first-time requests.
    When for_update=True, acquires a row-level lock on supported databases (PostgreSQL).
    """
    query = db.query(OrganizationStorageUsage).filter(
        OrganizationStorageUsage.organization_id == organization_id
    )
    if for_update:
        query = query.with_for_update()
    usage = query.first()
    if not usage:
        if for_update:
            # PostgreSQL cannot lock a non-existent row. Serialize cold-start on parent Organization.
            db.query(Organization.id).filter(
                Organization.id == organization_id
            ).with_for_update().scalar()
            # Re-check under the lock
            usage = (
                db.query(OrganizationStorageUsage)
                .filter(OrganizationStorageUsage.organization_id == organization_id)
                .with_for_update()
                .first()
            )
        if not usage:
            recalculate_organization_storage(organization_id, db)
            query = db.query(OrganizationStorageUsage).filter(
                OrganizationStorageUsage.organization_id == organization_id
            )
            if for_update:
                query = query.with_for_update()
            usage = query.first()
    return usage


def get_storage_usage(
    organization_id: str,
    db: Session,
) -> Dict[str, Any]:
    """Retrieve current storage usage snapshot and limit analysis.

    Returns stable machine keys and quota details.
    """
    usage = get_or_create_storage_usage(organization_id, db, for_update=False)

    eff_limit_gb, source = get_effective_limit_with_source(db, organization_id, "max_storage_gb")

    is_unlimited = (eff_limit_gb is None)
    if is_unlimited:
        eff_limit_bytes = None
        remaining_bytes = None
        percentage_used = None
        is_over_limit = False
    else:
        eff_limit_bytes = int(round(float(eff_limit_gb) * BYTES_PER_GB))
        remaining_bytes = max(0, eff_limit_bytes - usage.total_used_bytes)
        if eff_limit_bytes > 0:
            percentage_used = round(min(usage.total_used_bytes / eff_limit_bytes * 100, 100), 2)
        else:
            percentage_used = 100.0 if usage.total_used_bytes > 0 else 0.0
        is_over_limit = (usage.total_used_bytes > eff_limit_bytes)

    return {
        "organization_id": organization_id,
        "files_documents_bytes": usage.files_documents_bytes,
        "stored_reports_bytes": usage.stored_reports_bytes,
        "application_data_bytes": usage.application_data_bytes,
        "total_used_bytes": usage.total_used_bytes,
        "effective_limit_bytes": eff_limit_bytes,
        "effective_limit_gb": float(eff_limit_gb) if eff_limit_gb is not None else None,
        "remaining_bytes": remaining_bytes,
        "percentage_used": percentage_used,
        "is_unlimited": is_unlimited,
        "is_over_limit": is_over_limit,
        "limit_source": source,
        "calculated_at": usage.calculated_at,
    }


# ---------------------------------------------------------------------------
# Quota Enforcement & Mutation Tracking
# ---------------------------------------------------------------------------

def _raise_quota_exceeded(
    organization_id: str,
    eff_limit_gb: float,
    eff_limit_bytes: int,
    current_bytes: int,
    incoming_bytes: int,
) -> None:
    logger.warning(
        "Storage limit reached for org=%s: current=%s, incoming=%s, limit=%s",
        organization_id,
        current_bytes,
        incoming_bytes,
        eff_limit_bytes,
    )
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail={
            "code": "PLAN_STORAGE_LIMIT_REACHED",
            "limit_key": "max_storage_gb",
            "limit_gb": float(eff_limit_gb),
            "limit_bytes": eff_limit_bytes,
            "current_bytes": current_bytes,
            "incoming_bytes": incoming_bytes,
            "remaining_bytes": max(0, eff_limit_bytes - current_bytes),
            "message": (
                f"Storage limit reached. Your plan allows {eff_limit_gb} GB ({eff_limit_bytes:,} bytes). "
                f"Current usage is {current_bytes:,} bytes, incoming upload is {incoming_bytes:,} bytes."
            ),
        },
        headers={"X-Error-Code": "PLAN_STORAGE_LIMIT_REACHED"},
    )


def check_storage_quota(
    organization_id: str,
    incoming_bytes: int,
    db: Session,
) -> None:
    """Validate whether organization can accept incoming_bytes without exceeding quota.

    Fast lock-free pre-flight check before initiating object upload.
    Raises HTTPException 403 (PLAN_STORAGE_LIMIT_REACHED) if quota is exceeded.
    """
    eff_limit_gb, _ = get_effective_limit_with_source(db, organization_id, "max_storage_gb")
    if eff_limit_gb is None:
        # Unlimited plan or override
        return

    eff_limit_bytes = int(round(float(eff_limit_gb) * BYTES_PER_GB))
    usage = get_or_create_storage_usage(organization_id, db, for_update=False)

    projected_total = usage.total_used_bytes + incoming_bytes
    if projected_total > eff_limit_bytes:
        _raise_quota_exceeded(organization_id, eff_limit_gb, eff_limit_bytes, usage.total_used_bytes, incoming_bytes)


def record_file_upload_serialized(
    organization_id: Optional[str],
    file_size: int,
    db: Session,
) -> None:
    """Atomically reserve quota and increment snapshot usage under row lock.

    Guarantees that concurrent uploads cannot race past effective quota limit.
    Raises HTTPException 403 (PLAN_STORAGE_LIMIT_REACHED) if another concurrent
    upload completed first and filled the remaining quota.
    """
    if not organization_id or file_size <= 0:
        return

    usage = get_or_create_storage_usage(organization_id, db, for_update=True)
    if not usage:
        return

    eff_limit_gb, _ = get_effective_limit_with_source(db, organization_id, "max_storage_gb")
    if eff_limit_gb is not None:
        eff_limit_bytes = int(round(float(eff_limit_gb) * BYTES_PER_GB))
        if usage.total_used_bytes + file_size > eff_limit_bytes:
            _raise_quota_exceeded(organization_id, eff_limit_gb, eff_limit_bytes, usage.total_used_bytes, file_size)

    usage.files_documents_bytes += file_size
    usage.total_used_bytes += file_size
    usage.updated_at = _now()
    db.flush()


def record_file_upload(
    organization_id: Optional[str],
    file_size: int,
    db: Session,
) -> None:
    """Increment snapshot storage usage after successful file persistence."""
    record_file_upload_serialized(organization_id, file_size, db)


def record_file_deletion(
    organization_id: Optional[str],
    file_size: int,
    db: Session,
) -> None:
    """Decrement snapshot storage usage after file deletion under row lock."""
    if not organization_id or file_size <= 0:
        return

    usage = get_or_create_storage_usage(organization_id, db, for_update=True)
    if not usage:
        return

    usage.files_documents_bytes = max(0, usage.files_documents_bytes - file_size)
    usage.total_used_bytes = max(0, usage.total_used_bytes - file_size)
    usage.updated_at = _now()
    db.flush()


# ---------------------------------------------------------------------------
# Legacy Reconciliation Utility
# ---------------------------------------------------------------------------

def reconcile_legacy_files(
    organization_id: Optional[str],
    db: Session,
) -> Dict[str, Any]:
    """Inspect and safely reconcile legacy StoredFile rows where size is unknown or 0.

    Recovers sizes from:
    1. Direct DB stored.data if present.
    2. R2 HeadObject ContentLength if storage_key is present.
    Never fabricates sizes.
    """
    query = db.query(StoredFile).filter(
        (StoredFile.size.is_(None)) | (StoredFile.size <= 0)
    )
    if organization_id:
        query = query.filter(StoredFile.organization_id == organization_id)

    files = query.all()
    reconciled_count = 0
    unresolved_count = 0
    bytes_recovered = 0

    for f in files:
        if f.data is not None and len(f.data) > 0:
            recovered_size = len(f.data)
            f.size = recovered_size
            reconciled_count += 1
            bytes_recovered += recovered_size
        elif f.storage_key:
            try:
                from app.core import r2
                client = r2.s3_client()
                if client and r2.BUCKET_NAME:
                    resp = client.head_object(Bucket=r2.BUCKET_NAME, Key=f.storage_key)
                    recovered_size = resp.get("ContentLength", 0)
                    if recovered_size > 0:
                        f.size = recovered_size
                        reconciled_count += 1
                        bytes_recovered += recovered_size
                    else:
                        unresolved_count += 1
                else:
                    unresolved_count += 1
            except Exception as e:
                logger.warning("Could not reconcile file %s via R2: %s", f.id, e)
                unresolved_count += 1
        else:
            unresolved_count += 1

    db.flush()

    # If organization specified, refresh its snapshot
    if organization_id:
        recalculate_organization_storage(organization_id, db)

    return {
        "scanned_count": len(files),
        "reconciled_count": reconciled_count,
        "unresolved_count": unresolved_count,
        "bytes_recovered": bytes_recovered,
    }
