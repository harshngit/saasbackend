"""Focused tests for the R2-aware file storage paths added in Phase 1.

Every test that would otherwise need real R2 credentials mocks
`app.core.r2._get_client()` instead — no boto3 client ever actually connects
to Cloudflare, and no real access key/secret is used anywhere in this file.
"""

import io
import os
import sys
import uuid
from unittest.mock import MagicMock

sys.path.insert(0, os.path.abspath("."))

from fastapi import UploadFile
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from starlette.datastructures import Headers

from app.core import files as files_module
from app.core import r2
from app.core.config import settings
from app.core.database import SessionLocal
from app.main import app
from app.models import StoredFile, User

client = TestClient(app)


def _register_org(label: str) -> tuple[dict, str]:
    email = f"{label}_{uuid.uuid4().hex[:8]}@example.com"
    r = client.post("/auth/register", json={
        "organization_name": f"{label} {uuid.uuid4().hex[:6]}",
        "admin_name": f"Admin {label}",
        "email": email,
        "password": "Password123!",
        "role": "admin",
    })
    assert r.status_code == 201, r.text
    token = r.json()["tokens"]["access_token"]
    db = SessionLocal()
    try:
        org_id = db.query(User).filter(User.email == email).first().organization_id
    finally:
        db.close()
    return {"Authorization": f"Bearer {token}"}, org_id


def _r2_configured(monkeypatch) -> MagicMock:
    """Make settings.r2_configured True and return a mock client that
    app.core.r2._get_client() will hand back instead of a real boto3 client."""
    monkeypatch.setattr(settings, "r2_account_id", "test-account")
    monkeypatch.setattr(settings, "r2_access_key_id", "test-key")
    monkeypatch.setattr(settings, "r2_secret_access_key", "test-secret")
    monkeypatch.setattr(settings, "r2_bucket_name", "test-bucket")
    mock_client = MagicMock()
    monkeypatch.setattr(r2, "_get_client", lambda: mock_client)
    return mock_client


# ------------------------------- no-R2 fallback ----------------------------------


def test_save_upload_without_r2_configured_stores_db_bytes(monkeypatch):
    assert settings.r2_configured is False  # sanity: default test settings have no R2
    _, org_id = _register_org("no_r2_upload")
    db = SessionLocal()
    try:
        upload = UploadFile(filename="a.png", file=io.BytesIO(b"hello-bytes"))
        url, size = files_module.save_upload(db, org_id, upload, allow_any=True)
        db.commit()
        file_id = url.rsplit("/", 1)[-1]
        stored = db.get(StoredFile, file_id)
        assert stored.storage_key is None
        assert stored.data == b"hello-bytes"
        assert size == len(b"hello-bytes")
    finally:
        db.close()


def test_save_bytes_without_r2_configured_stores_db_bytes():
    _, org_id = _register_org("no_r2_bytes")
    db = SessionLocal()
    try:
        url = files_module.save_bytes(db, org_id, b"raw-content", "f.bin", "application/octet-stream")
        db.commit()
        stored = db.get(StoredFile, url.rsplit("/", 1)[-1])
        assert stored.storage_key is None
        assert stored.data == b"raw-content"
    finally:
        db.close()


# ------------------------- new uploads return relative URLs -----------------------
# public_url() no longer embeds a backend host — this app has moved host three
# times (Render -> EC2 -> Cloudflare Workers), and a stored/returned URL that
# baked one in would break at the next move. Every caller must get back
# exactly "/files/{id}", never an absolute URL under any of those hosts.

_FORBIDDEN_HOST_FRAGMENTS = ("onrender.com", "api.asynk.in", "bsmart.workers.dev", "testserver", "http://", "https://")


def test_public_url_is_always_relative_regardless_of_request():
    from app.core.files import public_url

    assert public_url(None, "abc123") == "/files/abc123"


def test_save_upload_returns_relative_url_with_real_request():
    """The normal, request-based upload path (POST /files/upload) — a real
    Request object is available, but the returned URL must still be relative."""
    headers, org_id = _register_org("relative_upload")
    r = client.post(
        "/files/upload",
        files={"file": ("a.png", io.BytesIO(b"relative-url-bytes"), "image/png")},
        headers=headers,
    )
    assert r.status_code == 201, r.text
    url = r.json()["url"]
    assert url.startswith("/files/")
    for fragment in _FORBIDDEN_HOST_FRAGMENTS:
        assert fragment not in url, f"{fragment!r} leaked into upload URL: {url}"


def test_save_upload_direct_call_returns_relative_url():
    """save_upload() called directly (as most routers do) with a real
    FastAPI Request — still relative."""
    _, org_id = _register_org("relative_upload_direct")
    db = SessionLocal()
    try:
        upload = UploadFile(filename="a.png", file=io.BytesIO(b"direct-call-bytes"))
        url, _size = files_module.save_upload(db, org_id, upload, allow_any=True)
        assert url.startswith("/files/")
        for fragment in _FORBIDDEN_HOST_FRAGMENTS:
            assert fragment not in url, f"{fragment!r} leaked into upload URL: {url}"
    finally:
        db.close()


def test_save_bytes_request_none_path_returns_relative_url():
    """save_bytes() is called with request=None from customer_profile_service.py
    (there's no HTTP request in scope when converting a legacy inline document) —
    this is the one path that previously could produce a bare relative URL only
    when PUBLIC_BASE_URL was unset; it must now always be relative regardless."""
    _, org_id = _register_org("relative_bytes_no_request")
    db = SessionLocal()
    try:
        url = files_module.save_bytes(
            db, org_id, b"no-request-bytes", "doc.pdf", "application/pdf", request=None,
        )
        assert url == f"/files/{url.rsplit('/', 1)[-1]}"
        for fragment in _FORBIDDEN_HOST_FRAGMENTS:
            assert fragment not in url, f"{fragment!r} leaked into upload URL: {url}"
    finally:
        db.close()


def test_relative_url_still_serves_the_file_correctly():
    """Confirms the relative-URL change didn't break actual file retrieval —
    GET /files/{id} still works using just the id from the relative URL."""
    headers, org_id = _register_org("relative_url_retrieval")
    r = client.post(
        "/files/upload",
        files={"file": ("a.png", io.BytesIO(b"still-retrievable"), "image/png")},
        headers=headers,
    )
    file_id = r.json()["file_id"]
    assert r.json()["url"] == f"/files/{file_id}"

    r2 = client.get(f"/files/{file_id}")
    assert r2.status_code == 200
    assert r2.content == b"still-retrievable"


def test_files_get_serves_db_bytes_unchanged_when_no_storage_key():
    headers, org_id = _register_org("no_r2_get")
    db = SessionLocal()
    try:
        stored = StoredFile(organization_id=org_id, filename="a.txt", content_type="text/plain", size=5, data=b"hello")
        db.add(stored)
        db.commit()
        file_id = stored.id
    finally:
        db.close()

    r = client.get(f"/files/{file_id}")
    assert r.status_code == 200
    assert r.content == b"hello"
    assert r.headers["cache-control"] == "public, max-age=31536000, immutable"


def test_files_delete_without_storage_key_behaves_as_before():
    headers, org_id = _register_org("no_r2_delete")
    db = SessionLocal()
    try:
        stored = StoredFile(organization_id=org_id, filename="a.txt", content_type="text/plain", size=5, data=b"hello")
        db.add(stored)
        db.commit()
        file_id = stored.id
    finally:
        db.close()

    r = client.delete(f"/files/{file_id}", headers=headers)
    assert r.status_code == 204
    db = SessionLocal()
    try:
        assert db.get(StoredFile, file_id) is None
    finally:
        db.close()


# ---------------------------------- R2 upload ------------------------------------


def test_save_upload_with_r2_configured_uploads_and_clears_data(monkeypatch):
    mock_client = _r2_configured(monkeypatch)
    _, org_id = _register_org("r2_upload")
    db = SessionLocal()
    try:
        upload = UploadFile(
            filename="a.png", file=io.BytesIO(b"r2-bytes"),
            headers=Headers(raw=[(b"content-type", b"image/png")]),
        )
        url, size = files_module.save_upload(db, org_id, upload, allow_any=True)
        db.commit()
        file_id = url.rsplit("/", 1)[-1]

        mock_client.put_object.assert_called_once()
        call = mock_client.put_object.call_args.kwargs
        assert call["Bucket"] == "test-bucket"
        assert call["Key"] == f"org/{org_id}/{file_id}"
        assert call["Body"] == b"r2-bytes"
        assert call["ContentType"] == "image/png"

        stored = db.get(StoredFile, file_id)
        assert stored.storage_key == f"org/{org_id}/{file_id}"
        assert stored.data is None
        assert url.endswith(f"/files/{file_id}")  # the returned URL shape is unchanged by R2
    finally:
        db.close()


def test_save_upload_url_contract_unchanged_when_r2_configured(monkeypatch):
    _r2_configured(monkeypatch)
    _, org_id = _register_org("r2_url_contract")
    db = SessionLocal()
    try:
        upload = UploadFile(filename="a.png", file=io.BytesIO(b"xyz"))
        url, size = files_module.save_upload(db, org_id, upload, allow_any=True)
        assert url.endswith(f"/files/{url.rsplit('/', 1)[-1]}")
        assert size == 3
    finally:
        db.close()


def test_save_upload_does_not_create_row_if_r2_upload_fails(monkeypatch):
    mock_client = _r2_configured(monkeypatch)
    mock_client.put_object.side_effect = RuntimeError("simulated R2 outage")
    _, org_id = _register_org("r2_upload_fail")
    db = SessionLocal()
    try:
        before = db.query(StoredFile).count()
        upload = UploadFile(filename="a.png", file=io.BytesIO(b"will-fail"))
        try:
            files_module.save_upload(db, org_id, upload, allow_any=True)
            raised = False
        except RuntimeError:
            raised = True
        assert raised
        db.rollback()
        assert db.query(StoredFile).count() == before
    finally:
        db.close()


# ----------------------------------- R2 GET ---------------------------------------


def test_files_get_redirects_to_presigned_url_for_r2_backed_row(monkeypatch):
    mock_client = _r2_configured(monkeypatch)
    mock_client.generate_presigned_url.return_value = "https://r2.example.com/signed?sig=abc"
    headers, org_id = _register_org("r2_get")
    db = SessionLocal()
    try:
        stored = StoredFile(
            organization_id=org_id, filename="a.png", content_type="image/png", size=3,
            storage_key=f"org/{org_id}/some-key", data=None,
        )
        db.add(stored)
        db.commit()
        file_id = stored.id
    finally:
        db.close()

    r = client.get(f"/files/{file_id}", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "https://r2.example.com/signed?sig=abc"
    assert r.headers["cache-control"] == "private, max-age=300"

    call = mock_client.generate_presigned_url.call_args
    assert call.args[0] == "get_object"
    params = call.kwargs["Params"]
    assert params["Bucket"] == "test-bucket"
    assert params["Key"] == f"org/{org_id}/some-key"
    assert params["ResponseContentType"] == "image/png"
    assert params["ResponseContentDisposition"] == 'inline; filename="a.png"'
    assert call.kwargs["ExpiresIn"] == 3600


# ---------------------------------- R2 DELETE --------------------------------------


def test_files_delete_attempts_r2_deletion_then_deletes_db_row(monkeypatch):
    mock_client = _r2_configured(monkeypatch)
    headers, org_id = _register_org("r2_delete")
    db = SessionLocal()
    try:
        stored = StoredFile(
            organization_id=org_id, filename="a.png", content_type="image/png", size=3,
            storage_key=f"org/{org_id}/to-delete", data=None,
        )
        db.add(stored)
        db.commit()
        file_id = stored.id
    finally:
        db.close()

    r = client.delete(f"/files/{file_id}", headers=headers)
    assert r.status_code == 204
    mock_client.delete_object.assert_called_once_with(Bucket="test-bucket", Key=f"org/{org_id}/to-delete")

    db = SessionLocal()
    try:
        assert db.get(StoredFile, file_id) is None
    finally:
        db.close()


def test_files_delete_r2_failure_is_logged_and_non_fatal(monkeypatch):
    mock_client = _r2_configured(monkeypatch)
    mock_client.delete_object.side_effect = RuntimeError("simulated R2 delete failure")
    headers, org_id = _register_org("r2_delete_fail")
    db = SessionLocal()
    try:
        stored = StoredFile(
            organization_id=org_id, filename="a.png", content_type="image/png", size=3,
            storage_key=f"org/{org_id}/wont-delete-from-r2", data=None,
        )
        db.add(stored)
        db.commit()
        file_id = stored.id
    finally:
        db.close()

    r = client.delete(f"/files/{file_id}", headers=headers)
    assert r.status_code == 204  # the HTTP request itself must not fail

    db = SessionLocal()
    try:
        assert db.get(StoredFile, file_id) is None  # DB row is still deleted
    finally:
        db.close()


# ------------------------------ internal bytes helper -------------------------------


def test_get_bytes_returns_db_data_when_no_storage_key():
    stored = StoredFile(id="x", filename="a", content_type="text/plain", size=5, data=b"hello", storage_key=None)
    assert files_module.get_bytes(stored) == b"hello"


def test_get_bytes_fetches_from_r2_when_storage_key_set(monkeypatch):
    mock_client = _r2_configured(monkeypatch)
    mock_client.get_object.return_value = {"Body": io.BytesIO(b"from-r2")}
    stored = StoredFile(id="x", filename="a", content_type="text/plain", size=7, data=None, storage_key="org/o/x")
    assert files_module.get_bytes(stored) == b"from-r2"
    mock_client.get_object.assert_called_once_with(Bucket="test-bucket", Key="org/o/x")


def test_invoice_file_reference_resolves_r2_backed_row(monkeypatch):
    """The invoice PDF asset path (app/routers/invoices.py::_resolve_file_reference)
    must resolve an R2-backed StoredFile the same way get_bytes() does."""
    mock_client = _r2_configured(monkeypatch)
    mock_client.get_object.return_value = {"Body": io.BytesIO(b"logo-bytes")}
    from app.routers.invoices import _resolve_file_reference

    _, org_id = _register_org("r2_invoice_ref")
    db = SessionLocal()
    try:
        stored = StoredFile(
            organization_id=org_id, filename="logo.png", content_type="image/png", size=10,
            storage_key=f"org/{org_id}/logo-key", data=None,
        )
        db.add(stored)
        db.commit()
        reference = f"/files/{stored.id}"
        result = _resolve_file_reference(db, org_id, reference)
        assert result == b"logo-bytes"
    finally:
        db.close()
