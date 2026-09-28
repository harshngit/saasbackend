"""Backfill: move existing DB-backed StoredFile bytes into Cloudflare R2.

    python -m app.scripts.move_files_to_r2 --dry-run
    python -m app.scripts.move_files_to_r2 --limit 20
    python -m app.scripts.move_files_to_r2

Phase 2 tooling — implemented in this Phase 1 PR, but not run against
production by it. Requires R2 to be fully configured (settings.r2_configured);
`--dry-run` is the one exception (see below) since it makes no R2 calls at all.

Selects `stored_files` rows where `data IS NOT NULL AND storage_key IS NULL`,
processes them in batches of 50: upload -> head_object -> verify the remote
object's size matches the row's own `size` -> only then set `storage_key` and
clear `data`, committing once per batch.

Idempotent and resumable by construction, not by any separate checkpoint
mechanism: the selection filter (`storage_key IS NULL`) means a row that
already succeeded on a prior run is never selected again, so re-running after
a partial failure (or just re-running for more rows) is always safe. A row
that fails (upload error, or a verified size mismatch) is left completely
unmodified — not marked, not partially updated — so it's naturally retried on
the next run.

A failure on one row never affects another: only rows that pass verification
in a given batch are mutated before that batch's single commit, so one bad
row in a batch of 50 simply isn't part of what gets committed — the other 49
succeed normally.
"""

import argparse
import logging
import sys

logging.basicConfig(level=logging.INFO, format="%(levelname)-5.5s [%(name)s] %(message)s")
logger = logging.getLogger("crm.move_files_to_r2")

BATCH_SIZE = 50


def _iter_batches(db, *, limit: int | None, exclude_ids: set[str]):
    """Yield successive batches (lists of StoredFile rows) still needing migration.

    Re-queries fresh each time rather than paging by offset: since a
    successfully processed row is committed with storage_key set (so it drops
    out of the WHERE clause immediately), a plain repeated
    "give me the next 50 that still qualify" is naturally resumable and never
    re-fetches a row that offset-based paging could otherwise skip or repeat.

    `exclude_ids` additionally excludes every row already attempted earlier in
    THIS invocation, regardless of outcome. Without it, a row that fails (and
    is therefore deliberately left unmodified so a later, separate run can
    retry it) would still match the "still eligible" filter on the very next
    query within the same run, and a --dry-run (which never mutates anything)
    would match forever — both cases looping infinitely instead of finishing.
    Each caller of main() gets its own fresh set, so cross-invocation retry of
    failed rows is unaffected.
    """
    from app.models import StoredFile

    remaining = limit
    while remaining is None or remaining > 0:
        take = BATCH_SIZE if remaining is None else min(BATCH_SIZE, remaining)
        query = db.query(StoredFile).filter(
            StoredFile.data.isnot(None), StoredFile.storage_key.is_(None)
        )
        if exclude_ids:
            query = query.filter(StoredFile.id.notin_(exclude_ids))
        batch = query.order_by(StoredFile.created_at).limit(take).all()
        if not batch:
            return
        yield batch
        exclude_ids.update(stored.id for stored in batch)
        if remaining is not None:
            remaining -= len(batch)


def _process_batch(db, batch, *, dry_run: bool) -> tuple[int, int]:
    """Returns (succeeded, failed) for this batch. Commits once, only if not dry-run."""
    from app.core import r2

    succeeded = 0
    failed = 0
    for stored in batch:
        key = r2.object_key(stored.organization_id, stored.id)
        expected_size = len(stored.data) if stored.data is not None else stored.size

        if dry_run:
            logger.info("[dry-run] would upload id=%s key=%s size=%d", stored.id, key, expected_size)
            succeeded += 1
            continue

        try:
            r2.put_object(key, stored.data, stored.content_type)
        except Exception:  # noqa: BLE001
            logger.exception("Upload failed for id=%s — left unmodified, will retry next run", stored.id)
            failed += 1
            continue

        head = r2.head_object(key)
        remote_size = head.get("ContentLength") if head else None
        if head is None or remote_size != expected_size:
            logger.error(
                "Verification failed for id=%s key=%s (expected size=%d, got=%r) — "
                "DB row left unmodified, will retry next run",
                stored.id, key, expected_size, remote_size,
            )
            failed += 1
            continue

        stored.storage_key = key
        stored.data = None
        succeeded += 1
        logger.info("Migrated id=%s -> %s (%d bytes, verified)", stored.id, key, expected_size)

    if not dry_run:
        db.commit()
    return succeeded, failed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Report what would happen; make no R2 calls or DB changes.")
    parser.add_argument("--limit", type=int, default=None, help="Process at most N rows total (across however many batches of 50 that takes).")
    args = parser.parse_args(argv)

    from app.core.config import settings

    if not args.dry_run and not settings.r2_configured:
        print(
            "R2 is not configured (R2_ACCOUNT_ID / R2_ACCESS_KEY_ID / R2_SECRET_ACCESS_KEY / "
            "R2_BUCKET_NAME must all be set) — refusing to run for real. Use --dry-run to preview "
            "what would be migrated without R2 credentials."
        )
        return 2

    from app.core.database import SessionLocal

    db = SessionLocal()
    total_succeeded = 0
    total_failed = 0
    total_batches = 0
    seen_ids: set[str] = set()
    try:
        for batch in _iter_batches(db, limit=args.limit, exclude_ids=seen_ids):
            total_batches += 1
            succeeded, failed = _process_batch(db, batch, dry_run=args.dry_run)
            total_succeeded += succeeded
            total_failed += failed
            logger.info(
                "Batch %d: %d succeeded, %d failed (running totals: %d / %d)",
                total_batches, succeeded, failed, total_succeeded, total_failed,
            )
    finally:
        db.close()

    mode = "DRY RUN — " if args.dry_run else ""
    print(
        f"{mode}Done. {total_batches} batch(es), {total_succeeded} file(s) "
        f"{'would be ' if args.dry_run else ''}migrated, {total_failed} failed."
    )
    if total_failed:
        print("Failed rows were left unmodified and will be retried on the next run.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
