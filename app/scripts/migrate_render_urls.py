"""One-off audit and migration tool for Render file URLs.

Finds database columns and JSON document fields containing legacy Render URLs:
    https://<subdomain>.onrender.com/files/<file_id>
and rewrites them to the canonical target URL:
    https://api.asynk.in/files/<file_id>

Default mode is DRY-RUN (safe, read-only, zero database writes).
Pass `--apply` to commit changes.

Usage:
    python -m app.scripts.migrate_render_urls
    python -m app.scripts.migrate_render_urls --dry-run
    python -m app.scripts.migrate_render_urls --apply
    python -m app.scripts.migrate_render_urls --apply --target-base-url "https://api.asynk.in"
"""

import argparse
import copy
import logging
import re
import sys
from typing import Any, NamedTuple

from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

logging.basicConfig(level=logging.INFO, format="%(levelname)-5.5s [%(name)s] %(message)s")
logger = logging.getLogger("crm.migrate_render_urls")

DEFAULT_TARGET_BASE_URL = "https://api.asynk.in"

# Regex matching Render file URLs (HTTPS and HTTP, any onrender subdomain)
RENDER_URL_PATTERN = re.compile(
    r"https?://[a-zA-Z0-9.\-_]*onrender\.com/files/([0-9a-fA-F\-]{36}|[a-zA-Z0-9\-_]+)",
    re.IGNORECASE,
)

# Explicit target registry: (ModelName, [single_url_columns], [json_columns])
TARGET_REGISTRY: list[tuple[str, list[str], list[str]]] = [
    (
        "Organization",
        [
            "logo_url",
            "company_logo",
            "signature_url",
            "authorized_signature",
            "stamp_url",
            "letterhead_url",
            "banner_url",
            "payment_qr_url",
            "google_pay_phonepe_paytm_qr_code",
            "doc_gst_url",
            "doc_pan_url",
            "doc_coi_url",
            "doc_trade_license_url",
            "doc_msme_url",
            "doc_fssai_url",
            "doc_other_url",
            "auth_person_photo_url",
            "auth_person_signature_url",
        ],
        ["doc_other_files"],
    ),
    (
        "User",
        [
            "profile_photo",
            "identity_proof_file",
            "identify_proofs",
            "resume_cv",
            "offer_letter",
            "appointment_letter",
        ],
        ["uploaded_documents", "experience_certificates", "educational_certificates"],
    ),
    (
        "Product",
        [
            "cover_image",
            "product_video",
            "product_catalog_brochure",
            "product_manual",
            "download_file",
            "compliance_certificate",
            "warranty_document",
            "product_datasheet",
        ],
        ["images", "other_attachments"],
    ),
    (
        "ProductVariant",
        ["image_url"],
        [],
    ),
    (
        "Category",
        ["image"],
        [],
    ),
    (
        "CustomerDocument",
        ["url"],
        [],
    ),
    (
        "Customer",
        ["profile_image_id"],
        [],
    ),
    (
        "Expense",
        ["receipt_url", "vendor_invoice_url"],
        ["supporting_documents"],
    ),
    (
        "PurchaseInvoice",
        [
            "attachment_url",
            "supplier_quotation_url",
            "purchase_order_url",
            "supplier_invoice_url",
            "delivery_challan_url",
        ],
        ["supporting_documents"],
    ),
    (
        "SupplierInvoice",
        ["attachment_url"],
        [],
    ),
    (
        "Delivery",
        ["pod_signature_file_id"],
        ["pod_photo_file_ids"],
    ),
]


def extract_render_file_ids(value: Any) -> list[str]:
    """Recursively extract all file IDs from any strings matching RENDER_URL_PATTERN."""
    ids: list[str] = []
    if isinstance(value, str):
        for match in RENDER_URL_PATTERN.finditer(value):
            ids.append(match.group(1))
    elif isinstance(value, dict):
        for v in value.values():
            ids.extend(extract_render_file_ids(v))
    elif isinstance(value, list):
        for item in value:
            ids.extend(extract_render_file_ids(item))
    return ids


def transform_render_urls(
    value: Any,
    valid_file_ids: set[str],
    target_base_url: str,
) -> tuple[Any, int, int]:
    """Recursively transform Render URLs to target base URL.

    Only transforms URLs whose file ID is in valid_file_ids.
    Returns: (new_value, transformed_count, skipped_count)
    """
    target_base = target_base_url.rstrip("/")

    if isinstance(value, str):
        transformed = 0
        skipped = 0

        def _repl(match: re.Match) -> str:
            nonlocal transformed, skipped
            file_id = match.group(1)
            if file_id in valid_file_ids:
                transformed += 1
                return f"{target_base}/files/{file_id}"
            else:
                skipped += 1
                return match.group(0)

        new_str = RENDER_URL_PATTERN.sub(_repl, value)
        return new_str, transformed, skipped

    elif isinstance(value, dict):
        new_dict = {}
        total_transformed = 0
        total_skipped = 0
        for k, v in value.items():
            new_v, tr, sk = transform_render_urls(v, valid_file_ids, target_base_url)
            new_dict[k] = new_v
            total_transformed += tr
            total_skipped += sk
        return new_dict, total_transformed, total_skipped

    elif isinstance(value, list):
        new_list = []
        total_transformed = 0
        total_skipped = 0
        for item in value:
            new_item, tr, sk = transform_render_urls(item, valid_file_ids, target_base_url)
            new_list.append(new_item)
            total_transformed += tr
            total_skipped += sk
        return new_list, total_transformed, total_skipped

    return value, 0, 0


class AuditResult(NamedTuple):
    total_references: int
    unique_file_ids: set[str]
    valid_file_ids: set[str]
    missing_file_ids: set[str]
    column_counts: dict[str, int]
    eligible_rewrites: int
    skipped_rewrites: int
    rows_modified: int


def validate_stored_files(db: Session, file_ids: set[str]) -> tuple[set[str], set[str]]:
    """Validate extracted file IDs against stored_files table.

    A StoredFile reference is valid if the row exists and either
    storage_key is set (R2) or data is set (legacy DB).
    """
    if not file_ids:
        return set(), set()

    from app.models.stored_file import StoredFile

    valid_ids: set[str] = set()
    # Batch query in chunks of 500
    id_list = list(file_ids)
    for i in range(0, len(id_list), 500):
        chunk = id_list[i : i + 500]
        rows = (
            db.query(StoredFile.id, StoredFile.storage_key, StoredFile.data)
            .filter(StoredFile.id.in_(chunk))
            .all()
        )
        for row in rows:
            # Valid if either storage_key or data is populated
            if row.storage_key or (row.data is not None):
                valid_ids.add(row.id)

    missing_ids = file_ids - valid_ids
    return valid_ids, missing_ids


def audit_and_migrate(
    db: Session,
    *,
    apply: bool = False,
    target_base_url: str = DEFAULT_TARGET_BASE_URL,
) -> AuditResult:
    """Scan all target models/columns, audit Render URLs, and optionally rewrite valid ones."""
    from app import models

    all_file_ids: set[str] = set()
    column_counts: dict[str, int] = {}
    found_occurrences = 0

    # Phase 1: Discovery & Collection
    model_instances: list[tuple[str, Any, list[str], list[str], list[Any]]] = []

    for model_name, scalar_cols, json_cols in TARGET_REGISTRY:
        model_cls = getattr(models, model_name, None)
        if model_cls is None:
            logger.warning("Model %s not found in app.models, skipping", model_name)
            continue

        try:
            rows = db.query(model_cls).all()
        except Exception:
            logger.exception("Failed to query rows for model %s", model_name)
            continue

        model_instances.append((model_name, model_cls, scalar_cols, json_cols, rows))

        for row in rows:
            for col in scalar_cols:
                val = getattr(row, col, None)
                if val:
                    ids = extract_render_file_ids(val)
                    if ids:
                        key = f"{model_name}.{col}"
                        column_counts[key] = column_counts.get(key, 0) + len(ids)
                        all_file_ids.update(ids)
                        found_occurrences += len(ids)

            for col in json_cols:
                val = getattr(row, col, None)
                if val:
                    ids = extract_render_file_ids(val)
                    if ids:
                        key = f"{model_name}.{col}"
                        column_counts[key] = column_counts.get(key, 0) + len(ids)
                        all_file_ids.update(ids)
                        found_occurrences += len(ids)

    # Phase 2: Validate against stored_files
    valid_file_ids, missing_file_ids = validate_stored_files(db, all_file_ids)

    # Phase 3: Migration / Dry-run calculation
    eligible_rewrites = 0
    skipped_rewrites = 0
    rows_modified = 0

    for model_name, model_cls, scalar_cols, json_cols, rows in model_instances:
        model_modified = False
        for row in rows:
            row_changed = False
            for col in scalar_cols:
                val = getattr(row, col, None)
                if val:
                    new_val, tr, sk = transform_render_urls(val, valid_file_ids, target_base_url)
                    eligible_rewrites += tr
                    skipped_rewrites += sk
                    if tr > 0:
                        row_changed = True
                        if apply:
                            setattr(row, col, new_val)

            for col in json_cols:
                val = getattr(row, col, None)
                if val:
                    # Deep copy before mutating
                    new_val, tr, sk = transform_render_urls(
                        copy.deepcopy(val), valid_file_ids, target_base_url
                    )
                    eligible_rewrites += tr
                    skipped_rewrites += sk
                    if tr > 0:
                        row_changed = True
                        if apply:
                            setattr(row, col, new_val)
                            flag_modified(row, col)

            if row_changed:
                rows_modified += 1
                model_modified = True

        if apply and model_modified:
            db.flush()

    if apply:
        db.commit()

    return AuditResult(
        total_references=found_occurrences,
        unique_file_ids=all_file_ids,
        valid_file_ids=valid_file_ids,
        missing_file_ids=missing_file_ids,
        column_counts=column_counts,
        eligible_rewrites=eligible_rewrites,
        skipped_rewrites=skipped_rewrites,
        rows_modified=rows_modified,
    )


def print_report(result: AuditResult, *, apply: bool, target_base_url: str) -> None:
    mode_header = "APPLY MODE" if apply else "DRY RUN AUDIT (Default - No changes written)"
    print("\n" + "=" * 60)
    print(f"  RENDER URL MIGRATION REPORT — {mode_header}")
    print("=" * 60)
    print(f"Target Base URL:               {target_base_url}")
    print(f"Total Render URL references:   {result.total_references}")
    print(f"Unique file IDs found:         {len(result.unique_file_ids)}")
    print(f"Valid StoredFile references:   {len(result.valid_file_ids)}")
    print(f"Missing StoredFile references: {len(result.missing_file_ids)}")
    print(f"URLs eligible for rewrite:     {result.eligible_rewrites}")
    print(f"URLs skipped (missing file):   {result.skipped_rewrites}")
    print(f"Rows {'modified' if apply else 'that would be modified'}: {result.rows_modified}")

    if result.column_counts:
        print("\nBreakdown by Model.Column:")
        print("-" * 60)
        for col, count in sorted(result.column_counts.items()):
            print(f"  {col:<45} : {count:>5} reference(s)")

    if result.missing_file_ids:
        print("\nWARNING: The following referenced file IDs were NOT found in stored_files:")
        print("-" * 60)
        for mid in sorted(result.missing_file_ids):
            print(f"  - {mid}")
        print("These URLs will NOT be rewritten to prevent creating broken links.")

    print("=" * 60)
    if not apply:
        print("Note: To execute the migration, run with the `--apply` flag.\n")
    else:
        print("Migration completed and committed successfully.\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Audit and migrate legacy Render URLs to api.asynk.in."
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually apply changes to the database. (Default is dry-run mode).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run in read-only dry-run mode (default).",
    )
    parser.add_argument(
        "--target-base-url",
        type=str,
        default=DEFAULT_TARGET_BASE_URL,
        help=f"Target base URL for files (default: {DEFAULT_TARGET_BASE_URL}).",
    )
    args = parser.parse_args(argv)

    apply_mode = bool(args.apply and not args.dry_run)

    from app.core.database import SessionLocal

    db = SessionLocal()
    try:
        result = audit_and_migrate(
            db,
            apply=apply_mode,
            target_base_url=args.target_base_url,
        )
        print_report(result, apply=apply_mode, target_base_url=args.target_base_url)
    except Exception:
        db.rollback()
        logger.exception("Error during Render URL audit/migration")
        return 1
    finally:
        db.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
