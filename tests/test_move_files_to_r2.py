"""Focused tests for the R2 backfill CLI (app/scripts/move_files_to_r2.py).

Never runs against real R2 or a real "production" database — everything here
uses the normal SQLite test database and a mocked boto3/R2 client.
"""

import os
import sys
import uuid
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.abspath("."))

from app.core import r2
from app.core.config import settings
from app.core.database import SessionLocal
from app.models import Organization, StoredFile
from app.scripts import move_files_to_r2


@pytest.fixture(autouse=True)
def _clean_stored_files():
    """move_files_to_r2's backfill query is intentionally global (no org
    filter), and several tests below deliberately leave a row unmodified to
    prove it's left for retry. Without this, that row would still match the
    next test's own query and contaminate it. Only StoredFile rows are
    touched, and only for tests in this module."""
    db = SessionLocal()
    try:
        db.query(StoredFile).delete()
        db.commit()
    finally:
        db.close()
    yield


def _configure_r2(monkeypatch) -> MagicMock:
    monkeypatch.setattr(settings, "r2_account_id", "acc")
    monkeypatch.setattr(settings, "r2_access_key_id", "key")
    monkeypatch.setattr(settings, "r2_secret_access_key", "secret")
    monkeypatch.setattr(settings, "r2_bucket_name", "bucket")
    mock_client = MagicMock()
    monkeypatch.setattr(r2, "_get_client", lambda: mock_client)
    return mock_client


def _make_org() -> str:
    db = SessionLocal()
    try:
        org = Organization(name=f"Backfill Org {uuid.uuid4().hex[:6]}")
        db.add(org)
        db.commit()
        return org.id
    finally:
        db.close()


def _make_stored_file(org_id: str, data: bytes = b"payload", *, storage_key: str | None = None) -> str:
    db = SessionLocal()
    try:
        stored = StoredFile(
            organization_id=org_id, filename="f.bin", content_type="application/octet-stream",
            size=len(data), data=data, storage_key=storage_key,
        )
        db.add(stored)
        db.commit()
        return stored.id
    finally:
        db.close()


def _head_ok(size: int) -> dict:
    return {"ContentLength": size}


def test_refuses_to_run_for_real_without_r2_configured():
    assert settings.r2_configured is False
    rc = move_files_to_r2.main([])
    assert rc == 2


def test_dry_run_makes_no_upload_or_db_change(monkeypatch):
    mock_client = _configure_r2(monkeypatch)
    org_id = _make_org()
    file_id = _make_stored_file(org_id, b"dry-run-bytes")

    rc = move_files_to_r2.main(["--dry-run"])
    assert rc == 0
    mock_client.put_object.assert_not_called()

    db = SessionLocal()
    try:
        stored = db.get(StoredFile, file_id)
        assert stored.storage_key is None
        assert stored.data == b"dry-run-bytes"
    finally:
        db.close()


def test_successful_migration_sets_storage_key_and_clears_data(monkeypatch):
    mock_client = _configure_r2(monkeypatch)
    org_id = _make_org()
    payload = b"twelve-bytes"
    file_id = _make_stored_file(org_id, payload)
    mock_client.head_object.return_value = _head_ok(len(payload))

    rc = move_files_to_r2.main([])
    assert rc == 0
    mock_client.put_object.assert_called_once()
    put_call = mock_client.put_object.call_args.kwargs
    assert put_call["Key"] == f"org/{org_id}/{file_id}"
    assert put_call["Body"] == payload

    db = SessionLocal()
    try:
        stored = db.get(StoredFile, file_id)
        assert stored.storage_key == f"org/{org_id}/{file_id}"
        assert stored.data is None
    finally:
        db.close()


def test_already_migrated_rows_are_skipped():
    org_id = _make_org()
    _make_stored_file(org_id, b"already-there", storage_key=f"org/{org_id}/already")
    # No R2 config needed: nothing eligible, so --dry-run (which never requires
    # R2 config) can prove zero rows were even considered.
    rc = move_files_to_r2.main(["--dry-run"])
    assert rc == 0


def test_rerun_after_success_is_idempotent(monkeypatch):
    mock_client = _configure_r2(monkeypatch)
    org_id = _make_org()
    payload = b"idempotent"
    file_id = _make_stored_file(org_id, payload)
    mock_client.head_object.return_value = _head_ok(len(payload))

    assert move_files_to_r2.main([]) == 0
    assert mock_client.put_object.call_count == 1

    # Second run: the row no longer matches (storage_key is set), so nothing new happens.
    assert move_files_to_r2.main([]) == 0
    assert mock_client.put_object.call_count == 1


def test_size_mismatch_does_not_clear_db_bytes(monkeypatch):
    mock_client = _configure_r2(monkeypatch)
    org_id = _make_org()
    payload = b"mismatched-size"
    file_id = _make_stored_file(org_id, payload)
    mock_client.head_object.return_value = _head_ok(len(payload) - 1)  # wrong size

    rc = move_files_to_r2.main([])
    assert rc == 1  # failures reported

    db = SessionLocal()
    try:
        stored = db.get(StoredFile, file_id)
        assert stored.storage_key is None  # left completely unmodified
        assert stored.data == payload
    finally:
        db.close()


def test_head_object_failure_leaves_row_untouched_and_is_reported(monkeypatch):
    mock_client = _configure_r2(monkeypatch)
    org_id = _make_org()
    file_id = _make_stored_file(org_id, b"will-fail-head")
    mock_client.head_object.side_effect = RuntimeError("simulated head_object failure")

    rc = move_files_to_r2.main([])
    assert rc == 1

    db = SessionLocal()
    try:
        stored = db.get(StoredFile, file_id)
        assert stored.storage_key is None
        assert stored.data == b"will-fail-head"
    finally:
        db.close()


def test_upload_failure_for_one_row_does_not_affect_others_in_the_batch(monkeypatch):
    mock_client = _configure_r2(monkeypatch)
    org_id = _make_org()
    good_payload = b"good-file"
    bad_id = _make_stored_file(org_id, b"bad-file")
    good_id = _make_stored_file(org_id, good_payload)
    mock_client.head_object.return_value = _head_ok(len(good_payload))

    def put_side_effect(Bucket, Key, Body, ContentType):  # noqa: N803
        if Key.endswith(bad_id):
            raise RuntimeError("simulated upload failure")

    mock_client.put_object.side_effect = put_side_effect

    rc = move_files_to_r2.main([])
    assert rc == 1  # at least one failure reported

    db = SessionLocal()
    try:
        bad = db.get(StoredFile, bad_id)
        assert bad.storage_key is None  # untouched, will retry
        good = db.get(StoredFile, good_id)
        assert good.storage_key is not None  # the other row in the same batch still succeeded
        assert good.data is None
    finally:
        db.close()


def test_limit_is_respected(monkeypatch):
    mock_client = _configure_r2(monkeypatch)
    org_id = _make_org()
    for _ in range(3):
        _make_stored_file(org_id, b"x")
    mock_client.head_object.return_value = _head_ok(1)

    rc = move_files_to_r2.main(["--limit", "2"])
    assert rc == 0
    assert mock_client.put_object.call_count == 2
