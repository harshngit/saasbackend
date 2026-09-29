"""Backend host file-URL migration: relative-path audit and migration tool.

Absolute backend file URLs are converted to /files/<id>.
Only validated stored_files IDs are rewritten.
Dry-run is the default.
--apply is required for database writes.

Finds database columns and JSON document fields containing an absolute
backend file URL — from the old Render hosting (any *.onrender.com), the
EC2-era host (api.asynk.in), or the current Cloudflare Worker host
(crm-saas-backend.bsmart.workers.dev) — and rewrites each one to the
canonical, host-independent form:

    /files/<file_id>

This script deliberately has no "target host" concept. The whole point of
the migration is that a stored file reference stops embedding a host at
all, so it keeps working no matter which host ends up serving the API next
(this backend has already moved host three times: Render -> EC2 -> Cloudflare
Workers). Frontend/mobile clients resolve a relative /files/<id> reference
against whatever API base URL they're already configured with.

Default mode is DRY-RUN (safe, read-only, zero database writes).
Pass `--apply` to commit changes.

Usage:
    python -m app.scripts.migrate_render_urls
    python -m app.scripts.migrate_render_urls --dry-run
    python -m app.scripts.migrate_render_urls --apply
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

# Every backend host a stored file URL may have been generated under.
# `onrender.com` stays subdomain-flexible (any *.onrender.com Render
# service); the other two are this app's own two fixed, known hosts
# (the stopped EC2-era host, and the current Cloudflare Worker host).
_RENDER_HOST_RE = r"[a-zA-Z0-9.\-_]*onrender\.com"
_ASYNK_HOST_RE = r"api\.asynk\.in"
_WORKERS_HOST_RE = r"crm-saas-backend\.bsmart\.workers\.dev"

ABSOLUTE_FILE_URL_PATTERN = re.compile(
    rf"https?://(?P<host>{_RENDER_HOST_RE}|{_ASYNK_HOST_RE}|{_WORKERS_HOST_RE})"
    r"/files/(?P<fid>[0-9a-fA-F\-]{36}|[a-zA-Z0-9\-_]+)",
    re.IGNORECASE,
)

# A genuinely bare `/files/<id>` reference. The negative lookbehind requires
# the character right before `/files/` to NOT be a hostname character
# (letter/digit/dot/hyphen) — which is always true for the `/files/<id>`
# suffix of an absolute URL (always preceded by ".com", ".in", etc.), so this
# never double-counts an absolute match, and never mistakes an unrelated
# site's own `/files/...` path (e.g. https://evil.example.com/files/abc) for
# one of ours.
RELATIVE_FILE_URL_PATTERN = re.compile(
    r"(?<![A-Za-z0-9.\-])/files/(?P<fid>[0-9a-fA-F\-]{36}|[a-zA-Z0-9\-_]+)"
)

_HOST_SUFFIX_TO_CATEGORY = (
    ("onrender.com", "render"),
    ("api.asynk.in", "asynk"),
    ("crm-saas-backend.bsmart.workers.dev", "workers"),
)


def _classify_host(host: str) -> str:
    """Map a matched host to a reporting category. Defensive fallback of
    "other" is unreachable given ABSOLUTE_FILE_URL_PATTERN's own alternation
    only ever captures one of the three known hosts, but keeps this function
    safe if that pattern is ever extended without updating this table."""
    host_lower = host.lower()
    for suffix, category in _HOST_SUFFIX_TO_CATEGORY:
        if host_lower == suffix or host_lower.endswith("." + suffix):
            return category
    return "other"


# Explicit target registry: (ModelName, [single_url_columns], [json_columns])
# Exhaustively covers every genuine file-URL-capable column in the schema —
# verified against every String/Text/JSON column in app/models/ (see the
# preceding audit). Deliberately excludes columns that are always raw IDs
# (e.g. CustomerDocument.file_id, StoredFile itself) and columns that are
# genuinely external, non-file URLs (e.g. facebook_url/instagram_url/
# linkedin_url/twitter_url/youtube_url on both Organization and Customer).
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


def extract_file_references(value: Any) -> list[dict]:
    """Recursively extract every file reference from any string in `value`.

    Each item is {"fid": str, "host": "render"|"asynk"|"workers"|"relative"}.
    Absolute matches and already-relative matches are both returned (and
    never double-counted — see RELATIVE_FILE_URL_PATTERN's docstring).
    """
    refs: list[dict] = []
    if isinstance(value, str):
        for match in ABSOLUTE_FILE_URL_PATTERN.finditer(value):
            refs.append({"fid": match.group("fid"), "host": _classify_host(match.group("host"))})
        for match in RELATIVE_FILE_URL_PATTERN.finditer(value):
            refs.append({"fid": match.group("fid"), "host": "relative"})
    elif isinstance(value, dict):
        for v in value.values():
            refs.extend(extract_file_references(v))
    elif isinstance(value, list):
        for item in value:
            refs.extend(extract_file_references(item))
    return refs


def transform_file_urls(value: Any, valid_file_ids: set[str]) -> tuple[Any, int, int]:
    """Recursively rewrite absolute backend file URLs to relative /files/<id>.

    Only rewrites a match whose file id is in valid_file_ids — a missing or
    invalid id is left completely untouched, never turned into a broken
    relative link. Already-relative references need no rewrite and are left
    as-is (this function only matches ABSOLUTE_FILE_URL_PATTERN).

    Returns: (new_value, transformed_count, skipped_count)
    """
    if isinstance(value, str):
        transformed = 0
        skipped = 0

        def _repl(match: re.Match) -> str:
            nonlocal transformed, skipped
            fid = match.group("fid")
            if fid in valid_file_ids:
                transformed += 1
                return f"/files/{fid}"
            skipped += 1
            return match.group(0)

        new_str = ABSOLUTE_FILE_URL_PATTERN.sub(_repl, value)
        return new_str, transformed, skipped

    elif isinstance(value, dict):
        new_dict = {}
        total_transformed = 0
        total_skipped = 0
        for k, v in value.items():
            new_v, tr, sk = transform_file_urls(v, valid_file_ids)
            new_dict[k] = new_v
            total_transformed += tr
            total_skipped += sk
        return new_dict, total_transformed, total_skipped

    elif isinstance(value, list):
        new_list = []
        total_transformed = 0
        total_skipped = 0
        for item in value:
            new_item, tr, sk = transform_file_urls(item, valid_file_ids)
            new_list.append(new_item)
            total_transformed += tr
            total_skipped += sk
        return new_list, total_transformed, total_skipped

    return value, 0, 0


class AuditResult(NamedTuple):
    total_references: int
    render_references: int
    asynk_references: int
    workers_references: int
    other_references: int
    relative_references: int
    unique_file_ids: set[str]
    valid_file_ids: set[str]
    missing_file_ids: set[str]
    eligible_rewrites: int
    skipped_rewrites: int
    rows_modified: int
    # {(model, column, host_category): {"count": int, "ids": set[str], "valid_ids": set[str], "missing_ids": set[str]}}
    column_breakdown: dict[tuple[str, str, str], dict]


def validate_stored_files(db: Session, file_ids: set[str]) -> tuple[set[str], set[str]]:
    """Validate extracted file IDs against stored_files.

    A StoredFile reference is valid only if the row exists AND either
    storage_key is set (R2) or data is set (legacy DB) — a row with neither
    (corrupt/orphaned) counts as invalid, same as a row that doesn't exist.
    """
    if not file_ids:
        return set(), set()

    from app.models.stored_file import StoredFile

    valid_ids: set[str] = set()
    id_list = list(file_ids)
    for i in range(0, len(id_list), 500):
        chunk = id_list[i : i + 500]
        rows = (
            db.query(StoredFile.id, StoredFile.storage_key, StoredFile.data)
            .filter(StoredFile.id.in_(chunk))
            .all()
        )
        for row in rows:
            if row.storage_key or (row.data is not None):
                valid_ids.add(row.id)

    missing_ids = file_ids - valid_ids
    return valid_ids, missing_ids


def audit_and_migrate(db: Session, *, apply: bool = False) -> AuditResult:
    """Scan all target models/columns, audit file URL references, and
    optionally rewrite the eligible (valid, absolute-host) ones to /files/<id>."""
    from app import models

    unique_ids: set[str] = set()
    column_breakdown: dict[tuple[str, str, str], dict] = {}
    total = render = asynk = workers = other = relative = 0

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
            for col in scalar_cols + json_cols:
                val = getattr(row, col, None)
                if not val:
                    continue
                refs = extract_file_references(val)
                if not refs:
                    continue
                for ref in refs:
                    fid, host = ref["fid"], ref["host"]
                    unique_ids.add(fid)
                    total += 1
                    if host == "render":
                        render += 1
                    elif host == "asynk":
                        asynk += 1
                    elif host == "workers":
                        workers += 1
                    elif host == "relative":
                        relative += 1
                    else:
                        other += 1
                    key = (model_name, col, host)
                    entry = column_breakdown.setdefault(key, {"count": 0, "ids": set()})
                    entry["count"] += 1
                    entry["ids"].add(fid)

    # Phase 2: Validate against stored_files
    valid_file_ids, missing_file_ids = validate_stored_files(db, unique_ids)

    for entry in column_breakdown.values():
        entry["valid_ids"] = entry["ids"] & valid_file_ids
        entry["missing_ids"] = entry["ids"] - valid_file_ids

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
                    new_val, tr, sk = transform_file_urls(val, valid_file_ids)
                    eligible_rewrites += tr
                    skipped_rewrites += sk
                    if tr > 0:
                        row_changed = True
                        if apply:
                            setattr(row, col, new_val)

            for col in json_cols:
                val = getattr(row, col, None)
                if val:
                    new_val, tr, sk = transform_file_urls(copy.deepcopy(val), valid_file_ids)
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
        total_references=total,
        render_references=render,
        asynk_references=asynk,
        workers_references=workers,
        other_references=other,
        relative_references=relative,
        unique_file_ids=unique_ids,
        valid_file_ids=valid_file_ids,
        missing_file_ids=missing_file_ids,
        eligible_rewrites=eligible_rewrites,
        skipped_rewrites=skipped_rewrites,
        rows_modified=rows_modified,
        column_breakdown=column_breakdown,
    )


def print_report(result: AuditResult, *, apply: bool) -> None:
    mode_header = "APPLY MODE" if apply else "DRY RUN AUDIT (Default - No changes written)"
    print("\n" + "=" * 72)
    print(f"  BACKEND FILE URL MIGRATION REPORT — {mode_header}")
    print("  Absolute backend file URLs -> relative /files/<id>")
    print("=" * 72)
    print(f"Total file URL references:            {result.total_references}")
    print(f"  Render (*.onrender.com):             {result.render_references}")
    print(f"  api.asynk.in:                        {result.asynk_references}")
    print(f"  Cloudflare Worker:                   {result.workers_references}")
    print(f"  Other supported absolute hosts:      {result.other_references}")
    print(f"  Already relative (/files/<id>):      {result.relative_references}")
    print(f"Unique file IDs found:                 {len(result.unique_file_ids)}")
    print(f"Valid StoredFile references:           {len(result.valid_file_ids)}")
    print(f"Missing/invalid StoredFile references: {len(result.missing_file_ids)}")
    print(f"References eligible for rewrite:       {result.eligible_rewrites}")
    print(f"References skipped (missing/invalid):  {result.skipped_rewrites}")
    print(f"Rows {'modified' if apply else 'that would be modified'}: {result.rows_modified}")

    if result.column_breakdown:
        print("\nBreakdown by Model / Column / Host:")
        print("-" * 72)
        print(f"  {'Model':<18}{'Column':<26}{'Host':<10}{'Count':>7}{'Valid':>8}{'Missing':>9}")
        for (model, col, host), entry in sorted(result.column_breakdown.items()):
            print(
                f"  {model:<18}{col:<26}{host:<10}"
                f"{entry['count']:>7}{len(entry['valid_ids']):>8}{len(entry['missing_ids']):>9}"
            )

    if result.missing_file_ids:
        print("\nWARNING: the following referenced file IDs were NOT found (or not valid) in stored_files:")
        print("-" * 72)
        for mid in sorted(result.missing_file_ids):
            print(f"  - {mid}")
        print("These references will NOT be rewritten, to avoid creating broken links.")

    print("=" * 72)
    if not apply:
        print("Note: this was a DRY RUN — no database writes occurred.")
        print("Run with --apply to rewrite eligible references to /files/<id>.\n")
    else:
        print("Migration completed and committed successfully.\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Rewrite absolute backend file URLs (any *.onrender.com, api.asynk.in, or "
            "crm-saas-backend.bsmart.workers.dev) to relative /files/<id> references.\n\n"
            "Absolute backend file URLs are converted to /files/<id>.\n"
            "Only validated stored_files IDs are rewritten.\n"
            "Dry-run is the default.\n"
            "--apply is required for database writes."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually apply changes to the database. (Default is dry-run mode).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run in read-only dry-run mode (default; explicit form).",
    )
    args = parser.parse_args(argv)

    apply_mode = bool(args.apply and not args.dry_run)

    from app.core.database import SessionLocal

    db = SessionLocal()
    try:
        result = audit_and_migrate(db, apply=apply_mode)
        print_report(result, apply=apply_mode)
    except Exception:
        db.rollback()
        logger.exception("Error during backend file URL audit/migration")
        return 1
    finally:
        db.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
