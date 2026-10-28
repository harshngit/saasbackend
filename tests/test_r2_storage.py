"""Focused tests for the R2-aware file storage paths added in Phase 1.

Every test that would otherwise need real R2 credentials mocks
`app.core.r2._get_client()` instead — no boto3 client ever actually connects
to Cloudflare, and no real access key/secret is used anywhere in this file.
"""

import io
import os
import sys
import uuid
from unittest.mock import MagicMock, patch

import pytest

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
from app.models import (
    Customer, CustomerDocument, Organization, OrganizationStorageUsage, Quotation, StoredFile, User, UserRole,
)

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


# ------------------- upload responses become absolute with PUBLIC_BASE_URL --------
# public_url()/save_upload()/save_bytes() (tested above) still only ever produce
# and store the relative form — that's what keeps the DB host-independent. These
# cover the separate, response-only layer: normalize_file_url() applied to the
# same upload responses before they're returned to the caller.

_PUBLIC_BASE_URL = "https://crm-saas-backend.bsmart.workers.dev"


def test_files_upload_response_absolute_but_db_row_relative(monkeypatch):
    monkeypatch.setattr(settings, "public_base_url", _PUBLIC_BASE_URL)
    headers, org_id = _register_org("absolute_upload")
    r = client.post(
        "/files/upload",
        files={"file": ("a.png", io.BytesIO(b"absolute-url-bytes"), "image/png")},
        headers=headers,
    )
    assert r.status_code == 201, r.text
    body = r.json()
    file_id = body["file_id"]
    assert body["url"] == f"{_PUBLIC_BASE_URL}/files/{file_id}"

    db = SessionLocal()
    try:
        stored = db.get(StoredFile, file_id)
        # StoredFile itself carries no URL column — the id is the only
        # reference, so "the DB value" for this endpoint is verified via the
        # organization-logo test below, which round-trips an actual *_url column.
        assert stored is not None
    finally:
        db.close()


def test_files_upload_response_relative_when_public_base_url_unset():
    assert settings.public_base_url == ""  # sanity
    headers, org_id = _register_org("relative_upload_default")
    r = client.post(
        "/files/upload",
        files={"file": ("a.png", io.BytesIO(b"default-bytes"), "image/png")},
        headers=headers,
    )
    assert r.json()["url"].startswith("/files/")


def test_organization_logo_upload_response_absolute_db_row_relative(monkeypatch):
    monkeypatch.setattr(settings, "public_base_url", _PUBLIC_BASE_URL)
    headers, org_id = _register_org("absolute_logo")
    r = client.post(
        "/organizations/settings/logo",
        files={"file": ("logo.png", io.BytesIO(b"logo-bytes"), "image/png")},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    url = r.json()["url"]
    assert url.startswith(_PUBLIC_BASE_URL + "/files/")

    db = SessionLocal()
    try:
        org = db.get(Organization, org_id)
        # The database column keeps the relative form — the response-layer
        # PUBLIC_BASE_URL prefix was never written back to it.
        assert org.logo_url.startswith("/files/")
        assert _PUBLIC_BASE_URL not in org.logo_url
        stored_id = org.logo_url.rsplit("/", 1)[-1]
        assert url == f"{_PUBLIC_BASE_URL}/files/{stored_id}"
    finally:
        db.close()


def test_organization_signature_upload_response_absolute_db_row_relative(monkeypatch):
    monkeypatch.setattr(settings, "public_base_url", _PUBLIC_BASE_URL)
    headers, org_id = _register_org("absolute_signature")
    r = client.post(
        "/organizations/settings/signature",
        files={"file": ("sig.png", io.BytesIO(b"sig-bytes"), "image/png")},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    url = r.json()["url"]
    assert url.startswith(_PUBLIC_BASE_URL + "/files/")

    db = SessionLocal()
    try:
        org = db.get(Organization, org_id)
        assert org.signature_url.startswith("/files/")
        assert _PUBLIC_BASE_URL not in org.signature_url
    finally:
        db.close()


def test_organization_upload_file_response_absolute_db_stays_untouched(monkeypatch):
    monkeypatch.setattr(settings, "public_base_url", _PUBLIC_BASE_URL)
    headers, org_id = _register_org("absolute_upload_file")
    r = client.post(
        "/organizations/settings/upload-file",
        files={"file": ("doc.pdf", io.BytesIO(b"doc-bytes"), "application/pdf")},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    url = r.json()["url"]
    assert url.startswith(_PUBLIC_BASE_URL + "/files/")
    file_id = url.rsplit("/", 1)[-1]

    db = SessionLocal()
    try:
        stored = db.get(StoredFile, file_id)
        assert stored is not None  # this endpoint doesn't set any *_url column itself
    finally:
        db.close()


def test_upload_response_host_independent_swapping_public_base_url(monkeypatch):
    """Changing PUBLIC_BASE_URL changes the response host only — no DB row is
    touched by the swap, and no second upload/migration is needed."""
    monkeypatch.setattr(settings, "public_base_url", "https://host-one.example.com")
    headers, org_id = _register_org("host_swap_logo")
    r1 = client.post(
        "/organizations/settings/logo",
        files={"file": ("logo.png", io.BytesIO(b"logo-bytes"), "image/png")},
        headers=headers,
    )
    assert r1.json()["url"].startswith("https://host-one.example.com/files/")

    db = SessionLocal()
    try:
        org = db.get(Organization, org_id)
        relative_value = org.logo_url
        assert relative_value.startswith("/files/")
    finally:
        db.close()

    monkeypatch.setattr(settings, "public_base_url", "https://host-two.example.com")
    r2 = client.get("/organizations/me", headers=headers)
    assert r2.status_code == 200

    db = SessionLocal()
    try:
        org = db.get(Organization, org_id)
        # Same relative DB value as before — swapping the setting alone
        # never rewrote it.
        assert org.logo_url == relative_value
    finally:
        db.close()


def test_other_documents_list_response_absolute_db_stays_relative(monkeypatch):
    """doc_other_files (nested list, OtherDocument.url) — each item's url must
    be absolute in the response while the organization row's JSON list keeps
    the relative form."""
    monkeypatch.setattr(settings, "public_base_url", _PUBLIC_BASE_URL)
    headers, org_id = _register_org("absolute_other_docs")
    r = client.post(
        "/organizations/settings/documents/other",
        files=[("files", ("doc.pdf", io.BytesIO(b"doc-bytes"), "application/pdf"))],
        headers=headers,
    )
    assert r.status_code == 201, r.text
    docs = r.json()
    assert len(docs) == 1
    assert docs[0]["url"].startswith(_PUBLIC_BASE_URL + "/files/")

    db = SessionLocal()
    try:
        org = db.get(Organization, org_id)
        assert len(org.doc_other_files) == 1
        assert org.doc_other_files[0]["url"].startswith("/files/")
        assert _PUBLIC_BASE_URL not in org.doc_other_files[0]["url"]
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


# ------------------------------ download filenames ----------------------------------

# Client filename -> the Content-Disposition every download of it carries: plain ASCII,
# one header line, the original name in RFC 6266 `filename*` when it had to change.
UNSAFE_NAMES = [
    ("report.pdf", 'inline; filename="report.pdf"'),
    ("a\r\nSet-Cookie: x=1.png", "inline; filename=\"aSet-Cookie_ x_1.png\"; filename*=UTF-8''aSet-Cookie%3A%20x%3D1.png"),
    ('he said "hi".png', 'inline; filename="he said hi.png"'),
    ("résumé 履歴書.pdf", "inline; filename=\"resume .pdf\"; filename*=UTF-8''r%C3%A9sum%C3%A9%20%E5%B1%A5%E6%AD%B4%E6%9B%B8.pdf"),
    ("..\\..\\secret/passwd.txt", 'inline; filename="passwd.txt"'),
    ("C:\\fakepath\\photo.jpg", 'inline; filename="photo.jpg"'),
    ("\x00\x07", 'inline; filename="file"'),
]


@pytest.mark.parametrize("name,header", UNSAFE_NAMES)
def test_uploads_store_a_safe_name_and_download_with_a_safe_header(name, header):
    _, org_id = _register_org("fname")
    db = SessionLocal()
    try:
        upload = UploadFile(filename=name, file=io.BytesIO(b"bytes"))
        url, _ = files_module.save_upload(db, org_id, upload, allow_any=True)
        db.commit()
        file_id = url.rsplit("/", 1)[-1]
        stored_name = db.get(StoredFile, file_id).filename
    finally:
        db.close()
    assert not any(ch in stored_name for ch in '\r\n"/\\\x00')

    r = client.get(f"/files/{file_id}")
    assert r.status_code == 200 and r.content == b"bytes"
    assert r.headers["content-disposition"] == header
    assert "set-cookie" not in r.headers


@pytest.mark.parametrize("name,header", UNSAFE_NAMES)
def test_rows_stored_before_the_fix_still_get_a_safe_header(name, header, monkeypatch):
    """Names already in stored_files are cleaned when the header is built, for DB-backed
    rows and for the R2 presigned redirect alike."""
    if chr(0) in name and SessionLocal().get_bind().dialect.name == "postgresql":
        pytest.skip("PostgreSQL cannot store NUL in text, so no such legacy row can exist there")
    _, org_id = _register_org("legacy_fname")
    db = SessionLocal()
    try:
        in_db = StoredFile(organization_id=org_id, filename=name, content_type="image/png", size=1, data=b"x")
        in_r2 = StoredFile(organization_id=org_id, filename=name, content_type="image/png", size=1,
                           storage_key=f"org/{org_id}/legacy-{uuid.uuid4().hex}", data=None)
        db.add_all([in_db, in_r2])
        db.commit()
        db_id, r2_id = in_db.id, in_r2.id
    finally:
        db.close()

    assert client.get(f"/files/{db_id}").headers["content-disposition"] == header

    mock_client = _r2_configured(monkeypatch)
    mock_client.generate_presigned_url.return_value = "https://r2.example.com/signed"
    assert client.get(f"/files/{r2_id}", follow_redirects=False).status_code == 302
    assert mock_client.generate_presigned_url.call_args.kwargs["Params"]["ResponseContentDisposition"] == header


def test_files_upload_answers_with_the_stored_name_and_r2_key_ignores_it(monkeypatch):
    mock_client = _r2_configured(monkeypatch)
    headers, org_id = _register_org("fname_endpoint")
    r = client.post("/files/upload", files={"file": ("résumé 2024.pdf", io.BytesIO(b"%PDF"), "application/pdf")},
                    headers=headers)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["name"] == body["filename"] == "résumé 2024.pdf"
    # The object key is server-generated: nothing from the client's name reaches it.
    assert mock_client.put_object.call_args.kwargs["Key"] == f"org/{org_id}/{body['file_id']}"


# ------------------------- admin profile-photo replacement --------------------------


def _admin_of(org_id: str) -> str:
    db = SessionLocal()
    try:
        return db.query(User).filter(User.organization_id == org_id, User.role == UserRole.ADMIN).first().id
    finally:
        db.close()


def _upload_photo(headers: dict) -> str:
    r = client.post("/files/upload", files={"file": ("p.png", io.BytesIO(b"png-" + uuid.uuid4().bytes), "image/png")},
                    headers=headers)
    assert r.status_code == 201, r.text
    return r.json()["url"]


def _set_photo(headers: dict, user_id: str, url: str | None, **extra):  # noqa: ANN202
    return client.patch(f"/users/{user_id}", json={"basic_information": {"profile_photo": url}, **extra}, headers=headers)


def _exists(url: str) -> bool:
    db = SessionLocal()
    try:
        return db.get(StoredFile, url.rsplit("/", 1)[-1]) is not None
    finally:
        db.close()


def _photo_of(user_id: str) -> str | None:
    db = SessionLocal()
    try:
        return db.get(User, user_id).profile_photo
    finally:
        db.close()


def test_admin_photo_replace_and_clear_delete_the_old_file():
    headers, org_id = _register_org("photo_replace")
    uid = _admin_of(org_id)
    first, second = _upload_photo(headers), _upload_photo(headers)
    assert _set_photo(headers, uid, first).status_code == 200
    assert _set_photo(headers, uid, second).status_code == 200
    assert _photo_of(uid) == second and _exists(second)
    assert not _exists(first)  # before the fix the reference scan still saw `first` on the row
    assert _set_photo(headers, uid, None).status_code == 200
    assert _photo_of(uid) is None and not _exists(second)


def test_admin_photo_old_r2_object_is_removed_only_after_the_commit(monkeypatch):
    mock_client = _r2_configured(monkeypatch)
    headers, org_id = _register_org("photo_r2")
    uid = _admin_of(org_id)
    first, second = _upload_photo(headers), _upload_photo(headers)
    assert _set_photo(headers, uid, first).status_code == 200
    seen = []
    mock_client.delete_object.side_effect = lambda **kw: seen.append((kw["Key"], _photo_of(uid)))
    assert _set_photo(headers, uid, second).status_code == 200
    # One delete, of the old object, made when the new photo was already committed.
    assert seen == [(f"org/{org_id}/{first.rsplit('/', 1)[-1]}", second)]
    assert not _exists(first) and _exists(second)


def test_admin_photo_never_deletes_a_shared_or_foreign_file():
    headers, org_id = _register_org("photo_shared")
    other_headers, _ = _register_org("photo_foreign")
    uid = _admin_of(org_id)
    shared, foreign = _upload_photo(headers), _upload_photo(other_headers)
    db = SessionLocal()
    try:
        db.get(Organization, org_id).logo_url = shared  # also the firm's logo
        db.commit()
    finally:
        db.close()
    assert _set_photo(headers, uid, shared).status_code == 200
    assert _set_photo(headers, uid, _upload_photo(headers)).status_code == 200
    assert _exists(shared)

    db = SessionLocal()
    try:
        db.get(User, uid).profile_photo = foreign  # another firm's file on this row
        db.commit()
    finally:
        db.close()
    assert _set_photo(headers, uid, _upload_photo(headers)).status_code == 200
    assert _exists(foreign)


def test_failed_admin_update_keeps_the_old_photo_and_its_file():
    headers, org_id = _register_org("photo_fail")
    uid = _admin_of(org_id)
    first = _upload_photo(headers)
    assert _set_photo(headers, uid, first).status_code == 200

    taken = f"taken_{uuid.uuid4().hex[:8]}@example.com"
    db = SessionLocal()
    try:
        db.add(User(organization_id=org_id, name="Colleague", email=taken, password_hash="x", system_role="staff"))
        db.commit()
    finally:
        db.close()
    # Rejected before writing (duplicate email) ...
    clash = {"contact_information": {"official_email": taken}}
    assert _set_photo(headers, uid, _upload_photo(headers), **clash).status_code == 409
    assert _photo_of(uid) == first and _exists(first)
    # ... and rolled back at commit (lost the uq_user_org_email race): still untouched.
    with patch("app.routers.users._email_taken", return_value=False):
        assert _set_photo(headers, uid, _upload_photo(headers), **clash).status_code == 409
    assert _photo_of(uid) == first and _exists(first)


def test_old_photo_cleanup_failure_keeps_the_update_and_leaves_only_an_orphan(monkeypatch):
    headers, org_id = _register_org("photo_cleanup_fail")
    uid = _admin_of(org_id)
    first, second = _upload_photo(headers), _upload_photo(headers)
    assert _set_photo(headers, uid, first).status_code == 200

    def boom(*args, **kwargs):
        raise RuntimeError("simulated storage failure")

    monkeypatch.setattr(files_module, "file_is_referenced", boom)
    assert _set_photo(headers, uid, second).status_code == 200
    assert _photo_of(uid) == second and _exists(second)
    assert _exists(first)  # orphaned, never a broken reference


# ------------------------------ customer document downloads ------------------------------


def _customer(org_id: str) -> str:
    db = SessionLocal()
    try:
        customer = Customer(organization_id=org_id, name=f"Doc Customer {uuid.uuid4().hex[:6]}")
        db.add(customer)
        db.commit()
        return customer.id
    finally:
        db.close()


@pytest.mark.parametrize("name,header", UNSAFE_NAMES)
def test_customer_document_rows_stored_before_the_fix_download_with_a_safe_header(name, header, monkeypatch):
    if chr(0) in name and SessionLocal().get_bind().dialect.name == "postgresql":
        pytest.skip("PostgreSQL cannot store NUL in text, so no such legacy row can exist there")
    headers, org_id = _register_org("cust_doc_legacy")
    customer_id = _customer(org_id)
    db = SessionLocal()
    try:
        in_db = StoredFile(organization_id=org_id, filename=name, content_type="application/pdf", size=3, data=b"pdf")
        in_r2 = StoredFile(organization_id=org_id, filename=name, content_type="application/pdf", size=3,
                           storage_key=f"org/{org_id}/legacy-{uuid.uuid4().hex}", data=None)
        db.add_all([in_db, in_r2])
        db.flush()
        for stored in (in_db, in_r2):
            db.add(CustomerDocument(id=stored.id, customer_id=customer_id, organization_id=org_id,
                                    document_type="other", name=name, content_type="application/pdf", size=3,
                                    url=f"/files/{stored.id}", file_id=stored.id))
        db.commit()
        db_id, r2_id = in_db.id, in_r2.id
    finally:
        db.close()

    attachment = header.replace("inline;", "attachment;", 1)
    r = client.get(f"/customers/{customer_id}/documents/{db_id}/download", headers=headers)
    assert r.status_code == 200 and r.content == b"pdf"
    assert r.headers["content-disposition"] == attachment and "set-cookie" not in r.headers

    mock_client = _r2_configured(monkeypatch)
    mock_client.get_object.return_value = {"Body": io.BytesIO(b"pdf")}
    r = client.get(f"/customers/{customer_id}/documents/{r2_id}/download", headers=headers)
    assert r.status_code == 200 and r.headers["content-disposition"] == attachment


def test_customer_document_upload_stores_the_safe_name_and_downloads_normally():
    headers, org_id = _register_org("cust_doc_upload")
    customer_id = _customer(org_id)
    r = client.post(f"/customers/{customer_id}/documents", data={"document_type": "other"},
                    # httpx itself escapes quotes in a multipart filename; the quote cases
                    # are covered by the stored-row tests above.
                    files={"file": ("..\\scans\\GST final ✓.pdf", io.BytesIO(b"%PDF-1"), "application/pdf")},
                    headers=headers)
    assert r.status_code == 201, r.text
    doc = r.json()
    assert doc["name"] == "GST final ✓.pdf"
    r = client.get(f"/customers/{customer_id}/documents/{doc['id']}/download", headers=headers)
    assert r.status_code == 200 and r.content == b"%PDF-1"
    assert r.headers["content-disposition"] == \
        "attachment; filename=\"GST final .pdf\"; filename*=UTF-8''GST%20final%20%E2%9C%93.pdf"

    plain = client.post(f"/customers/{customer_id}/documents", data={"document_type": "other"},
                        files={"file": ("pan.pdf", io.BytesIO(b"%PDF-2"), "application/pdf")}, headers=headers)
    r = client.get(f"/customers/{customer_id}/documents/{plain.json()['id']}/download", headers=headers)
    assert r.headers["content-disposition"] == 'attachment; filename="pan.pdf"'


def test_customer_document_delete_removes_the_file_only_after_the_commit(monkeypatch):
    mock_client = _r2_configured(monkeypatch)
    headers, org_id = _register_org("cust_doc_delete")
    customer_id = _customer(org_id)
    doc = client.post(f"/customers/{customer_id}/documents", data={"document_type": "other"},
                      files={"file": ("a.pdf", io.BytesIO(b"%PDF"), "application/pdf")}, headers=headers).json()
    seen = []

    def _delete_object(**kw):
        db = SessionLocal()
        try:
            seen.append((kw["Key"], db.get(CustomerDocument, doc["id"])))
        finally:
            db.close()

    mock_client.delete_object.side_effect = _delete_object
    assert client.delete(f"/customers/{customer_id}/documents/{doc['id']}", headers=headers).status_code == 204
    assert seen == [(f"org/{org_id}/{doc['id']}", None)]  # the document row was already gone
    assert not _exists(doc["url"])


# ------------------------------ document-number PDF headers ------------------------------


def test_document_numbers_never_break_the_pdf_header(monkeypatch):
    monkeypatch.setattr("app.services.entitlement_service.check_entitlement", lambda *a, **k: None)
    headers, org_id = _register_org("quote_header")
    db = SessionLocal()
    try:
        quotation = Quotation(organization_id=org_id, quotation_number='QT/2026/7\r\nX-Evil: 1"')
        db.add(quotation)
        db.commit()
        quotation_id = quotation.id
    finally:
        db.close()
    r = client.get(f"/quotations/{quotation_id}/pdf", headers=headers)
    assert r.status_code == 200, r.text
    assert r.headers["content-disposition"] == \
        "inline; filename=\"QT-2026-7X-Evil_ 1.pdf\"; filename*=UTF-8''QT-2026-7X-Evil%3A%201.pdf"
    assert "x-evil" not in r.headers
    assert files_module.content_disposition(files_module.document_filename("INV/2026/001.pdf"), "attachment") == \
        'attachment; filename="INV-2026-001.pdf"'


# ------------------------------ employee document delete ------------------------------


def _with_document(headers: dict, uid: str) -> str:
    url = _upload_photo(headers)
    r = client.patch(f"/users/{uid}", json={"documents": {"other_documents": [url]}}, headers=headers)
    assert r.status_code == 200, r.text
    return url


def _delete_document(headers: dict, uid: str, url: str, **kw):  # noqa: ANN202
    return kw.pop("_client", client).delete(
        f"/users/{uid}/documents/other_documents", params={"document_id": url.rsplit("/", 1)[-1]}, headers=headers
    )


def _documents_of(uid: str) -> list:
    db = SessionLocal()
    try:
        return [d.get("url") for d in (db.get(User, uid).uploaded_documents or [])]
    finally:
        db.close()


def test_employee_document_delete_removes_the_file_after_the_commit(monkeypatch):
    mock_client = _r2_configured(monkeypatch)
    headers, org_id = _register_org("emp_doc_delete")
    uid = _admin_of(org_id)
    url = _with_document(headers, uid)
    seen = []
    mock_client.delete_object.side_effect = lambda **kw: seen.append((kw["Key"], _documents_of(uid)))
    r = _delete_document(headers, uid, url)
    assert r.status_code == 200, r.text
    assert seen == [(f"org/{org_id}/{url.rsplit('/', 1)[-1]}", [])]  # list already saved without it
    assert not _exists(url)


def test_employee_document_failed_commit_keeps_document_and_file(monkeypatch):
    mock_client = _r2_configured(monkeypatch)
    headers, org_id = _register_org("emp_doc_fail")
    uid = _admin_of(org_id)
    url = _with_document(headers, uid)

    from sqlalchemy.exc import OperationalError
    from sqlalchemy.orm import Session as OrmSession

    real_commit = OrmSession.commit
    calls = {"n": 0}

    def failing_commit(self):  # the route's own commit fails once
        calls["n"] += 1
        if calls["n"] == 1:
            raise OperationalError("COMMIT", {}, Exception("simulated database failure"))
        return real_commit(self)

    monkeypatch.setattr(OrmSession, "commit", failing_commit)
    r = _delete_document(headers, uid, url, _client=TestClient(app, raise_server_exceptions=False))
    monkeypatch.setattr(OrmSession, "commit", real_commit)
    assert r.status_code == 500
    assert _documents_of(uid) == [url] and _exists(url)
    mock_client.delete_object.assert_not_called()


def test_employee_document_cleanup_failure_leaves_an_orphan_not_a_broken_document(monkeypatch):
    headers, org_id = _register_org("emp_doc_cleanup")
    uid = _admin_of(org_id)
    url = _with_document(headers, uid)

    def boom(*args, **kwargs):
        raise RuntimeError("simulated storage failure")

    monkeypatch.setattr(files_module, "file_is_referenced", boom)
    assert _delete_document(headers, uid, url).status_code == 200
    assert _documents_of(uid) == [] and _exists(url)


def test_employee_document_delete_keeps_shared_and_foreign_files():
    headers, org_id = _register_org("emp_doc_shared")
    other_headers, _ = _register_org("emp_doc_foreign")
    uid = _admin_of(org_id)
    shared = _with_document(headers, uid)
    db = SessionLocal()
    try:
        db.get(Organization, org_id).logo_url = shared  # the same file is the firm's logo
        db.commit()
    finally:
        db.close()
    assert _delete_document(headers, uid, shared).status_code == 200
    assert _exists(shared)

    foreign = _upload_photo(other_headers)
    db = SessionLocal()
    try:
        db.get(User, uid).uploaded_documents = [{"id": foreign.rsplit("/", 1)[-1], "url": foreign}]
        db.commit()
    finally:
        db.close()
    assert _delete_document(headers, uid, foreign).status_code == 200
    assert _exists(foreign)


# ------------------------------ cleanup vs a concurrent attach ------------------------------


def _usage(org_id: str) -> int:
    db = SessionLocal()
    try:
        row = db.query(OrganizationStorageUsage).filter(OrganizationStorageUsage.organization_id == org_id).first()
        return row.total_used_bytes if row else 0
    finally:
        db.close()


def test_photo_cleanup_keeps_a_file_attached_elsewhere_meanwhile(monkeypatch):
    """Deterministic interleaving: after the cleanup committed the row's removal, another
    request attaches the same file. The re-check restores the row (bytes untouched in R2)."""
    mock_client = _r2_configured(monkeypatch)
    headers, org_id = _register_org("photo_race")
    uid = _admin_of(org_id)
    first, second = _upload_photo(headers), _upload_photo(headers)
    assert _set_photo(headers, uid, first).status_code == 200
    before = _usage(org_id)

    real_check = files_module.file_is_referenced
    calls = {"n": 0}

    def interleaved(db, file_id):  # noqa: ANN001, ANN202
        calls["n"] += 1
        if calls["n"] == 2:  # the post-commit re-check: a colleague attached it just now
            other = SessionLocal()
            try:
                other.get(Organization, org_id).logo_url = first
                other.commit()
            finally:
                other.close()
        return real_check(db, file_id)

    monkeypatch.setattr(files_module, "file_is_referenced", interleaved)
    assert _set_photo(headers, uid, second).status_code == 200
    assert calls["n"] == 2
    assert _exists(first)  # restored, and the logo is not broken
    mock_client.delete_object.assert_not_called()
    assert _usage(org_id) == before  # counted out, then back in


def test_two_cleanups_of_one_file_delete_it_once(monkeypatch):
    mock_client = _r2_configured(monkeypatch)
    headers, org_id = _register_org("photo_twice")
    first = _upload_photo(headers)
    db = SessionLocal()
    try:
        files_module.delete_replaced_file(db, first, org_id)
        files_module.delete_replaced_file(db, first, org_id)  # a retry or a concurrent twin
    finally:
        db.close()
    assert mock_client.delete_object.call_count == 1 and not _exists(first)


# ------------------------- firm branding, documents and theme cleanup -------------------------


def _org_column(org_id: str, column: str) -> str | None:
    db = SessionLocal()
    try:
        return getattr(db.get(Organization, org_id), column)
    finally:
        db.close()


def test_logo_signature_and_business_document_replacements_delete_the_old_file():
    """Before: the reference check ran before the new value was saved, so the old file
    was always 'still in use' and never deleted."""
    headers, org_id = _register_org("branding_replace")
    png = lambda: {"file": ("x.png", io.BytesIO(b"png-" + uuid.uuid4().bytes), "image/png")}  # noqa: E731
    for route, column in (("/organizations/settings/logo", "logo_url"),
                          ("/organizations/settings/signature", "signature_url"),
                          ("/organizations/settings/documents/gst_certificate", "doc_gst_url")):
        assert client.post(route, files=png(), headers=headers).status_code in (200, 201), route
        first = _org_column(org_id, column)
        assert client.post(route, files=png(), headers=headers).status_code in (200, 201), route
        second = _org_column(org_id, column)
        assert first != second and _exists(second)
        assert not _exists(first), route
    r = client.delete("/organizations/settings/documents/gst_certificate", headers=headers)
    assert r.status_code == 200 and _org_column(org_id, "doc_gst_url") is None and not _exists(second)


def test_other_business_document_remove_commits_and_deletes_the_file(monkeypatch):
    from app.models import ActivityLog

    mock_client = _r2_configured(monkeypatch)
    headers, org_id = _register_org("org_other_doc")
    docs = client.post("/organizations/settings/documents/other", headers=headers,
                       files=[("files", ("a.pdf", io.BytesIO(b"%PDF-a"), "application/pdf")),
                              ("files", ("b.pdf", io.BytesIO(b"%PDF-b"), "application/pdf"))]).json()
    first, second = docs[0], docs[1]
    seen = []
    mock_client.delete_object.side_effect = lambda **kw: seen.append(
        [d["id"] for d in (_org_column(org_id, "doc_other_files") or [])])
    r = client.delete(f"/organizations/settings/documents/other/{first['id']}", headers=headers)
    assert r.status_code == 200 and [d["id"] for d in r.json()] == [second["id"]]
    assert seen == [[second["id"]]]  # the list was saved before the object went
    assert not _exists(first["url"]) and _exists(second["url"])
    db = SessionLocal()
    try:  # before: the activity entry was added after the commit and lost
        assert db.query(ActivityLog).filter(ActivityLog.organization_id == org_id,
                                            ActivityLog.title == "Document removed").count() == 1
    finally:
        db.close()
    assert client.delete("/organizations/settings/documents/other", headers=headers).status_code == 204
    assert not _exists(second["url"])


def test_theme_background_is_removed_after_commit_but_never_while_shared():
    headers, org_id = _register_org("theme_cleanup")

    def upload() -> str:
        r = client.post("/organization/theme/background", headers=headers,
                        files={"file": ("bg.png", io.BytesIO(b"bg-" + uuid.uuid4().bytes), "image/png")})
        assert r.status_code == 200, r.text
        return r.json()["background"]["url"]

    first = upload()
    second = upload()
    assert not _exists(first) and _exists(second)  # replaced background deleted

    db = SessionLocal()
    try:  # the same file is now also the firm's logo
        db.get(Organization, org_id).logo_url = second
        db.commit()
    finally:
        db.close()
    r = client.post("/organization/theme/reset", headers=headers)
    assert r.status_code == 200 and r.json()["background"]["url"] is None
    assert _exists(second)  # before: deleted with no reference check, breaking the logo

    third = upload()
    r = client.delete("/organization/theme/background", headers=headers)
    assert r.status_code == 200 and r.json()["background"]["url"] is None and not _exists(third)


def test_product_file_slot_and_expense_receipt_replacements_delete_the_old_file(monkeypatch):
    from app.models import Expense, Product

    monkeypatch.setattr("app.services.entitlement_service.check_entitlement", lambda *a, **k: None)
    headers, org_id = _register_org("product_expense_files")
    db = SessionLocal()
    try:
        product = Product(organization_id=org_id, name=f"P {uuid.uuid4().hex[:6]}")
        expense = Expense(organization_id=org_id, category="Travel", amount=10)
        db.add_all([product, expense])
        db.commit()
        product_id, expense_id = product.id, expense.id
    finally:
        db.close()

    def column(model, row_id: str, name: str) -> str | None:  # noqa: ANN001
        session = SessionLocal()
        try:
            return getattr(session.get(model, row_id), name)
        finally:
            session.close()

    brochure = lambda: {"file": ("b.pdf", io.BytesIO(b"%PDF-" + uuid.uuid4().bytes), "application/pdf")}  # noqa: E731
    route = f"/products/{product_id}/files/product_catalog_brochure"
    assert client.post(route, files=brochure(), headers=headers).status_code == 200
    first = column(Product, product_id, "product_catalog_brochure")
    assert client.post(route, files=brochure(), headers=headers).status_code == 200
    second = column(Product, product_id, "product_catalog_brochure")
    assert not _exists(first) and _exists(second)
    assert client.delete(route, headers=headers).status_code == 200
    assert column(Product, product_id, "product_catalog_brochure") is None and not _exists(second)

    receipt = lambda: {"file": ("r.pdf", io.BytesIO(b"%PDF-" + uuid.uuid4().bytes), "application/pdf")}  # noqa: E731
    assert client.post(f"/expenses/{expense_id}/receipt", files=receipt(), headers=headers).status_code == 200
    old = column(Expense, expense_id, "receipt_url")
    assert client.post(f"/expenses/{expense_id}/receipt", files=receipt(), headers=headers).status_code == 200
    new = column(Expense, expense_id, "receipt_url")
    assert old != new and not _exists(old) and _exists(new)
