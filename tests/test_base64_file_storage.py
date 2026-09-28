"""Automated Test Suite for Binary File Storage Architecture & Base64 Elimination.

Verifies:
1. New uploads are stored as binary in `stored_files` and return `/files/{id}` links.
2. Legacy `data:image/...;base64,...` strings are migrated to binary stored_files and replaced with `/files/{id}`.
3. API responses never contain Base64 strings for image/file attributes.
4. JWT authentication with standard Base64URL continues to function without issues.
"""

import io
import os
import sys
import uuid

sys.path.insert(0, os.path.abspath("."))

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.main import app
from app.core.database import SessionLocal
from app.models import (
    Category, Customer, CustomerDocument, Expense, Organization, Product, PurchaseInvoice, StoredFile, User,
)
from app.services.file_migration_service import TARGETS, convert_inline_uploads

client = TestClient(app)


def test_new_file_upload_stores_binary_and_returns_url():
    """Test 1: New file upload persists binary in stored_files and returns /files/{id}."""
    email = f"file_user_{uuid.uuid4().hex[:6]}@test.com"
    r = client.post("/auth/register", json={
        "organization_name": "Storage Co",
        "business_type": "Manufacturing",
        "email": email,
        "password": "Password@123",
        "admin_name": "Storage Admin",
    })
    assert r.status_code == 201
    token = r.json()["tokens"]["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    # Upload mock PNG image
    png_bytes = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15c4"
    files = {"file": ("test_logo.png", io.BytesIO(png_bytes), "image/png")}
    res = client.post("/files/upload", headers=headers, files=files)
    assert res.status_code == 201
    body = res.json()

    assert "url" in body
    assert "file_id" in body
    assert "/files/" in body["url"]
    assert "base64," not in body["url"]

    file_id = body["file_id"]

    # Verify directly from DB
    db: Session = SessionLocal()
    try:
        sf = db.get(StoredFile, file_id)
        assert sf is not None
        assert sf.data == png_bytes
        assert sf.content_type == "image/png"
        assert sf.size == len(png_bytes)
    finally:
        db.close()

    # Verify retrieval via GET /files/{file_id}
    fetch_res = client.get(f"/files/{file_id}")
    assert fetch_res.status_code == 200
    assert fetch_res.content == png_bytes
    assert fetch_res.headers["content-type"] == "image/png"


def test_legacy_base64_migration_converts_to_stored_files():
    """Test 2: Inline Base64 data: URLs are migrated to binary stored_files and replaced with URLs."""
    email = f"legacy_user_{uuid.uuid4().hex[:6]}@test.com"
    r = client.post("/auth/register", json={
        "organization_name": "Legacy Co",
        "business_type": "Retail",
        "email": email,
        "password": "Password@123",
        "admin_name": "Legacy Admin",
    })
    assert r.status_code == 201
    org_id = r.json()["user"]["organization_id"]

    sample_png_base64 = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="

    db: Session = SessionLocal()
    try:
        # Seed an organization with inline base64 logo
        org = db.get(Organization, org_id)
        org.logo_url = sample_png_base64
        org.signature_url = sample_png_base64

        # Seed a product with inline base64 cover image
        prod = Product(
            organization_id=org_id,
            name="Legacy Product",
            cover_image=sample_png_base64,
            images=[sample_png_base64],
        )
        db.add(prod)

        # Seed a category with inline base64 image
        cat = Category(
            organization_id=org_id,
            name=f"Cat-{uuid.uuid4().hex[:4]}",
            image=sample_png_base64,
        )
        db.add(cat)
        db.commit()

        prod_id = prod.id
        cat_id = cat.id
    finally:
        db.close()

    # Run migration
    convert_inline_uploads()

    # Verify DB values were replaced with /files/{id}
    db = SessionLocal()
    try:
        org_check = db.get(Organization, org_id)
        assert org_check.logo_url.startswith("/files/")
        assert "base64," not in org_check.logo_url
        assert org_check.signature_url.startswith("/files/")

        prod_check = db.get(Product, prod_id)
        assert prod_check.cover_image.startswith("/files/")
        assert "base64," not in prod_check.cover_image
        assert len(prod_check.images) == 1
        assert prod_check.images[0].startswith("/files/")

        cat_check = db.get(Category, cat_id)
        assert cat_check.image.startswith("/files/")
        assert "base64," not in cat_check.image

        # Verify stored_files has the decoded binary
        file_id = org_check.logo_url.rsplit("/", 1)[-1]
        stored_file = db.get(StoredFile, file_id)
        assert stored_file is not None
        assert len(stored_file.data) > 0
        assert stored_file.content_type == "image/png"
    finally:
        db.close()


def test_customer_target_does_not_scan_documents_relationship():
    """Regression test for the confirmed Phase 1 bug: Customer.documents is a
    SQLAlchemy relationship (list of CustomerDocument ORM objects), not a
    JSON/list column — TARGETS must not list it as one."""
    customer_target = next(t for t in TARGETS if t[0] == "Customer")
    assert customer_target == ("Customer", ["doc_gst_url", "doc_pan_url"], [])


def test_customer_with_documents_does_not_crash_conversion_and_later_models_still_run():
    """A Customer with real CustomerDocument rows attached must not raise
    (the old TARGETS entry made `(entry or {}).get("url")` blow up on a
    CustomerDocument ORM instance), and — since that crash used to abort every
    model processed after Customer in TARGETS — Expense (which comes after
    Customer in the list) must still be converted in the same run.

    Note: Customer's own single_columns (doc_gst_url, doc_pan_url) are a
    separate, pre-existing issue — neither is an actual column on the current
    Customer model (confirmed directly: `hasattr(Customer, "doc_gst_url")` is
    False) — so they are not exercised here; see the Phase 1 implementation
    report for that finding. This test is specifically about the `documents`
    relationship crash, which is what this task's fix targets.
    """
    email = f"docs_user_{uuid.uuid4().hex[:6]}@test.com"
    r = client.post("/auth/register", json={
        "organization_name": "Docs Co",
        "business_type": "Retail",
        "email": email,
        "password": "Password@123",
        "admin_name": "Docs Admin",
    })
    assert r.status_code == 201
    org_id = r.json()["user"]["organization_id"]

    sample_png_base64 = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="

    db: Session = SessionLocal()
    try:
        customer = Customer(organization_id=org_id, name="Documented Customer")
        db.add(customer)
        db.flush()
        db.add(CustomerDocument(
            id=str(uuid.uuid4()),
            customer_id=customer.id,
            organization_id=org_id,
            document_type="gst_certificate",
            name="gst.pdf",
            content_type="application/pdf",
            size=10,
            url="/files/some-existing-id",  # never a data: URL — created via save_upload
        ))
        # A real, working later-in-TARGETS model/column, to prove the crash
        # (if it still happened) wouldn't have silently swallowed this too.
        expense = Expense(
            organization_id=org_id, category="Travel", amount=100.0,
            receipt_url=sample_png_base64,
        )
        db.add(expense)
        db.commit()
        customer_id, expense_id = customer.id, expense.id
    finally:
        db.close()

    result = convert_inline_uploads()  # must not raise
    assert "Customer" not in result.failed_models

    db = SessionLocal()
    try:
        cust = db.get(Customer, customer_id)
        assert len(cust.documents) == 1  # the relationship itself is untouched, no crash

        exp = db.get(Expense, expense_id)
        assert exp.receipt_url.startswith("/files/")
        assert "base64," not in exp.receipt_url
    finally:
        db.close()


def test_one_model_failure_does_not_abort_later_models(monkeypatch):
    """Failure isolation: if one TARGETS model raises during conversion, every
    other model in the list must still be processed and committed."""
    import app.services.file_migration_service as fms

    email = f"isolation_user_{uuid.uuid4().hex[:6]}@test.com"
    r = client.post("/auth/register", json={
        "organization_name": "Isolation Co",
        "business_type": "Retail",
        "email": email,
        "password": "Password@123",
        "admin_name": "Isolation Admin",
    })
    assert r.status_code == 201
    org_id = r.json()["user"]["organization_id"]

    sample_png_base64 = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="

    db = SessionLocal()
    try:
        expense = Expense(
            organization_id=org_id, category="Travel", amount=50.0,
            receipt_url=sample_png_base64,
        )
        db.add(expense)
        db.commit()
        expense_id = expense.id
    finally:
        db.close()

    original_convert_model = fms._convert_model

    def _boom(db, model_name, model, single_columns, list_columns):
        if model_name == "Customer":
            raise RuntimeError("simulated Customer conversion failure")
        return original_convert_model(db, model_name, model, single_columns, list_columns)

    monkeypatch.setattr(fms, "_convert_model", _boom)

    result = fms.convert_inline_uploads()
    assert "Customer" in result.failed_models

    # Expense comes after Customer in TARGETS — must still have been converted.
    db = SessionLocal()
    try:
        exp = db.get(Expense, expense_id)
        assert exp.receipt_url.startswith("/files/")
    finally:
        db.close()


def test_jwt_authentication_untouched():
    """Test 3: Verify standard Base64URL JWT authentication remains operational."""
    email = f"jwt_user_{uuid.uuid4().hex[:6]}@test.com"
    r = client.post("/auth/register", json={
        "organization_name": "Auth Co",
        "business_type": "Services",
        "email": email,
        "password": "Password@123",
        "admin_name": "Auth Admin",
    })
    assert r.status_code == 201
    tokens = r.json()["tokens"]
    assert "access_token" in tokens
    assert "refresh_token" in tokens

    # Validate access token has standard 3 parts separated by dots (header.payload.signature)
    parts = tokens["access_token"].split(".")
    assert len(parts) == 3

    # Authenticate with token on protected endpoint
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}
    me_res = client.get("/auth/me", headers=headers)
    assert me_res.status_code == 200
    assert me_res.json()["user"]["email"] == email


if __name__ == "__main__":
    print("\nRunning Base64 & File Storage Architecture Verification...")
    test_new_file_upload_stores_binary_and_returns_url()
    print("  PASS  New file upload stores binary in stored_files and returns /files/{id}")
    test_legacy_base64_migration_converts_to_stored_files()
    print("  PASS  Legacy Base64 migration converts to binary stored_files and updates DB")
    test_customer_target_does_not_scan_documents_relationship()
    print("  PASS  Customer TARGETS entry no longer scans the documents relationship")
    test_customer_with_documents_does_not_crash_conversion_and_later_models_still_run()
    print("  PASS  Customer with documents no longer crashes conversion; Expense still converts")
    class _FakeMonkeypatch:
        """Minimal stand-in for pytest's monkeypatch fixture, restoring the
        patched attribute afterwards — only used by this __main__ block, which
        pytest itself never executes (pytest calls the test function with its
        own real monkeypatch fixture instead)."""
        def __init__(self):
            self._restore = []

        def setattr(self, obj, name, value):
            self._restore.append((obj, name, getattr(obj, name)))
            setattr(obj, name, value)

        def undo(self):
            for obj, name, value in self._restore:
                setattr(obj, name, value)

    _mp = _FakeMonkeypatch()
    try:
        test_one_model_failure_does_not_abort_later_models(_mp)
    finally:
        _mp.undo()
    print("  PASS  One model's failure does not abort conversion of later models")
    test_jwt_authentication_untouched()
    print("  PASS  JWT Authentication with standard Base64URL is fully operational")
    print("\nAll 3 Base64 storage verification tests passed successfully!")
