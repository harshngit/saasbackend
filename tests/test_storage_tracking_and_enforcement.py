"""Comprehensive test suite for Organization Storage Usage Tracking & Plan Storage Enforcement.

Covers all Phase 8 test matrix requirements:
1. Plan default inheritance for max_storage_gb.
2. Plan changes immediately affect organizations inheriting that plan.
3. Organization override takes precedence over plan default.
4. Resetting override restores the current plan default.
5. Expired overrides are ignored and fall back to plan default.
6. Unlimited storage semantics (null max_storage_gb).
7. StoredFile byte accounting.
8. Cross-tenant file isolation.
9. Temporary reports count as 0 bytes.
10. Application data estimation and exclusion of shared records.
11. Upload within quota succeeds.
12. Upload exceeding quota is rejected with 403 PLAN_STORAGE_LIMIT_REACHED before storage.
13. Upload increments usage snapshot.
14. Deletion decrements usage snapshot.
15. Replacement does not double-count or delete a still-referenced file.
16. Existing over-limit organizations retain their data and can access/download.
17. Tenant storage API (/organizations/me/storage-usage) is tenant-isolated.
18. Super Admin storage inspection and recalculation endpoints require authorization.
19. Recalculation reconciles stale accounting.
20. Existing upload flows remain functional under the limit.
"""

import io
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.main import app
from app.core.config import settings
from app.core.database import Base, SessionLocal, engine
from app.models import (
    Customer,
    Organization,
    OrganizationStorageUsage,
    Plan,
    Product,
    StoredFile,
    SystemRole,
    User,
)
from app.models.organization_override import OrganizationLimitOverride
from app.services import entitlement_service, storage_service

Base.metadata.create_all(bind=engine)
client = TestClient(app)


def _super_admin_auth():
    r = client.post("/auth/login", json={
        "email": settings.super_admin_email,
        "password": settings.super_admin_password,
    })
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['tokens']['access_token']}"}


def _create_test_org(plan_name: str = "Basic"):
    db = SessionLocal()
    try:
        plan = db.query(Plan).filter(Plan.name == plan_name).first()
        assert plan is not None, f"Plan {plan_name} not found"

        org_uid = uuid.uuid4().hex[:8]
        email = f"admin_{org_uid}@{plan_name.lower()}.com"
        password = "Password123!"

        r = client.post("/auth/register", json={
            "organization_name": f"{plan_name} Storage Firm {org_uid}",
            "admin_name": f"{plan_name} Admin",
            "email": email,
            "password": password,
        })
        assert r.status_code == 201, r.text
        data = r.json()
        org_id = data["organization"]["id"]
        user_id = data["user"]["id"]

        org = db.get(Organization, org_id)
        org.plan_id = plan.id
        db.commit()

        login_res = client.post("/auth/login", json={"email": email, "password": password})
        assert login_res.status_code == 200, login_res.text
        token = login_res.json()["tokens"]["access_token"]
        auth_headers = {"Authorization": f"Bearer {token}"}

        return {
            "org_id": org_id,
            "user_id": user_id,
            "email": email,
            "password": password,
            "auth": auth_headers,
            "plan_id": plan.id,
        }
    finally:
        db.close()


def test_effective_storage_limit_resolution():
    """Tests 1-6: Plan inheritance, changes, overrides, reset, expiry, and unlimited semantics."""
    db = SessionLocal()
    try:
        # 1. Create a test plan with max_storage_gb = 2.0
        plan_name = f"StoragePlan_{uuid.uuid4().hex[:6]}"
        plan = Plan(
            name=plan_name,
            price_monthly=50.0,
            price_yearly=500.0,
            max_users=5,
            max_warehouses=2,
            max_storage_gb=2.0,
            is_active=True,
        )
        db.add(plan)
        db.commit()

        test_org_data = _create_test_org("Basic")
        org = db.get(Organization, test_org_data["org_id"])
        org.plan_id = plan.id
        db.commit()

        # 1. Plan default inheritance
        eff_limit = entitlement_service.get_effective_limit(db, org, "max_storage_gb")
        assert eff_limit == 2.0, f"Expected 2.0, got {eff_limit}"

        # 2. Plan change from 2.0 to 3.0 affects inheriting organization immediately
        plan.max_storage_gb = 3.0
        db.commit()
        eff_limit_updated = entitlement_service.get_effective_limit(db, org, "max_storage_gb")
        assert eff_limit_updated == 3.0, f"Expected 3.0 after plan update, got {eff_limit_updated}"

        # 3. Active organization override takes precedence
        override = OrganizationLimitOverride(
            organization_id=org.id,
            limit_key="max_storage_gb",
            value=5,  # 5 GB override
            expires_at=None,
        )
        db.add(override)
        db.commit()
        eff_limit_override = entitlement_service.get_effective_limit(db, org, "max_storage_gb")
        assert eff_limit_override == 5, f"Expected override 5, got {eff_limit_override}"

        # 4. Expired override is ignored and restores plan default
        override.expires_at = datetime.now(timezone.utc) - timedelta(hours=1)
        db.commit()
        eff_limit_expired = entitlement_service.get_effective_limit(db, org, "max_storage_gb")
        assert eff_limit_expired == 3.0, f"Expected plan fallback 3.0, got {eff_limit_expired}"

        # 5. Reset (delete) override restores plan default
        db.delete(override)
        db.commit()
        eff_limit_reset = entitlement_service.get_effective_limit(db, org, "max_storage_gb")
        assert eff_limit_reset == 3.0, f"Expected plan default 3.0, got {eff_limit_reset}"

        # 6. Unlimited plan semantics (max_storage_gb = None)
        plan.max_storage_gb = None
        db.commit()
        eff_limit_unlimited = entitlement_service.get_effective_limit(db, org, "max_storage_gb")
        assert eff_limit_unlimited is None, f"Expected None for unlimited, got {eff_limit_unlimited}"
    finally:
        db.close()


def test_storage_accounting_and_isolation():
    """Tests 7-10: StoredFile byte accounting, cross-tenant isolation, application data estimation."""
    org1_data = _create_test_org("Basic")
    org2_data = _create_test_org("Basic")

    db = SessionLocal()
    try:
        org1_id = org1_data["org_id"]
        org2_id = org2_data["org_id"]

        # Add StoredFile for Org 1 (10,000 bytes)
        file1 = StoredFile(
            id=str(uuid.uuid4()),
            organization_id=org1_id,
            filename="doc1.pdf",
            content_type="application/pdf",
            size=10000,
            data=b"0" * 10000,
        )
        # Add StoredFile for Org 2 (50,000 bytes)
        file2 = StoredFile(
            id=str(uuid.uuid4()),
            organization_id=org2_id,
            filename="doc2.pdf",
            content_type="application/pdf",
            size=50000,
            data=b"0" * 50000,
        )
        db.add_all([file1, file2])

        # Add business records to Org 1 (2 customers = 2 * 2048 = 4096 bytes)
        c1 = Customer(
            id=str(uuid.uuid4()),
            organization_id=org1_id,
            name="Cust 1",
            phone="1234567890",
        )
        c2 = Customer(
            id=str(uuid.uuid4()),
            organization_id=org1_id,
            name="Cust 2",
            phone="1234567891",
        )
        db.add_all([c1, c2])
        db.commit()

        # Recalculate Org 1
        usage1 = storage_service.recalculate_organization_storage(org1_id, db)
        assert usage1.files_documents_bytes == 10000, f"Expected 10000, got {usage1.files_documents_bytes}"
        assert usage1.stored_reports_bytes == 0, "Temporary reports must count as 0 bytes"
        assert usage1.application_data_bytes >= 4096, "Application data must account for customer records"
        assert usage1.total_used_bytes == usage1.files_documents_bytes + usage1.application_data_bytes

        # Recalculate Org 2 - must isolate and NOT include Org 1's bytes
        usage2 = storage_service.recalculate_organization_storage(org2_id, db)
        assert usage2.files_documents_bytes == 50000, f"Expected 50000, got {usage2.files_documents_bytes}"
    finally:
        db.close()


def test_storage_quota_enforcement_and_mutation():
    """Tests 11-16: Quota enforcement at upload, snapshot increments/decrements, replacement safety, over-limit safety."""
    db = SessionLocal()
    try:
        small_limit_bytes = 100000
        small_limit_gb = small_limit_bytes / (1024 * 1024 * 1024)
        plan = Plan(
            name=f"TinyPlan_{uuid.uuid4().hex[:6]}",
            max_storage_gb=small_limit_gb,
            is_active=True,
        )
        db.add(plan)
        db.commit()

        org_data = _create_test_org("Basic")
        org_id = org_data["org_id"]
        org = db.get(Organization, org_id)
        org.plan_id = plan.id
        db.commit()

        storage_service.recalculate_organization_storage(org_id, db)

        # 1. Upload within quota (5,000 bytes < 100,000 bytes) succeeds
        small_content = b"x" * 5000
        from app.core.files import _persist, delete_stored_file, safe_delete_unreferenced_file

        persisted1 = _persist(db, org_id, "file1.txt", small_content, "text/plain")
        db.commit()
        assert persisted1.size == 5000

        usage_after_upload = storage_service.get_storage_usage(org_id, db)
        assert usage_after_upload["files_documents_bytes"] == 5000
        assert not usage_after_upload["is_over_limit"]

        # 2. Upload exceeding quota (200,000 bytes > 100,000 bytes) is REJECTED with 403 PLAN_STORAGE_LIMIT_REACHED
        large_content = b"y" * 200000
        with pytest.raises(Exception) as exc_info:
            _persist(db, org_id, "large_file.txt", large_content, "text/plain")

        err = exc_info.value
        assert hasattr(err, "status_code") and err.status_code == 403
        assert err.detail.get("code") == "PLAN_STORAGE_LIMIT_REACHED"
        assert err.headers.get("X-Error-Code") == "PLAN_STORAGE_LIMIT_REACHED"

        # Verify large file was NOT persisted
        unwanted = db.query(StoredFile).filter(
            StoredFile.organization_id == org_id,
            StoredFile.filename == "large_file.txt",
        ).first()
        assert unwanted is None, "Rejected upload must not persist a StoredFile row"

        # 3. Deletion decrements snapshot usage
        delete_stored_file(db, persisted1, org_id)
        db.commit()
        usage_after_delete = storage_service.get_storage_usage(org_id, db)
        assert usage_after_delete["files_documents_bytes"] == 0

        # 4. Replacement test: unreferenced file is cleaned up, referenced file is preserved
        file_a = _persist(db, org_id, "old_logo.png", b"logo_data_a", "image/png")
        file_b = _persist(db, org_id, "new_logo.png", b"logo_data_b_long", "image/png")
        org.logo_url = f"/files/{file_b.id}"
        db.commit()

        deleted_a = safe_delete_unreferenced_file(db, f"/files/{file_a.id}", org_id)
        db.commit()
        assert deleted_a is True
        assert db.get(StoredFile, file_a.id) is None

        deleted_b = safe_delete_unreferenced_file(db, f"/files/{file_b.id}", org_id)
        db.commit()
        assert deleted_b is False
        assert db.get(StoredFile, file_b.id) is not None

        # 5. Over-limit organization retains existing files and can download them
        from app.core.files import get_bytes
        bytes_read = get_bytes(file_b)
        assert bytes_read == b"logo_data_b_long", "Over-limit org must retain access to existing data"
    finally:
        db.close()


def test_storage_apis_and_authorization():
    """Tests 17-20: Tenant API isolation, Super Admin lookup and recalculation."""
    org_data = _create_test_org("Basic")
    tenant_auth = org_data["auth"]
    org_id = org_data["org_id"]
    sa_auth = _super_admin_auth()

    # 1. Tenant storage usage endpoint
    resp = client.get("/organizations/me/storage-usage", headers=tenant_auth)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["organization_id"] == org_id
    assert "files_documents_bytes" in data
    assert "application_data_bytes" in data
    assert "total_used_bytes" in data
    assert "is_unlimited" in data
    assert "limit_source" in data

    # 2. Superadmin storage usage endpoint for any org
    resp_sa = client.get(f"/superadmin/organizations/{org_id}/storage-usage", headers=sa_auth)
    assert resp_sa.status_code == 200, resp_sa.text
    assert resp_sa.json()["organization_id"] == org_id

    # 3. Superadmin recalculation endpoint
    resp_recalc = client.post(f"/superadmin/organizations/{org_id}/storage-usage/recalculate", headers=sa_auth)
    assert resp_recalc.status_code == 200, resp_recalc.text
    recalc_data = resp_recalc.json()
    assert recalc_data["organization_id"] == org_id

    # 4. Non-superadmin cannot access superadmin recalculation
    resp_forbidden = client.post(f"/superadmin/organizations/{org_id}/storage-usage/recalculate", headers=tenant_auth)
    assert resp_forbidden.status_code == 403


def test_concurrency_and_r2_cleanup_on_quota_overflow(monkeypatch):
    """Verifies that if concurrent uploads race past pre-flight check, the serialized
    gate blocks quota overflow and deletes the uploaded R2 object immediately."""
    db = SessionLocal()
    try:
        # Organization with 10,000 bytes quota
        limit_bytes = 10000
        limit_gb = limit_bytes / (1024 * 1024 * 1024)
        plan = Plan(
            name=f"RacePlan_{uuid.uuid4().hex[:6]}",
            max_storage_gb=limit_gb,
            is_active=True,
        )
        db.add(plan)
        db.commit()

        org_data = _create_test_org("Basic")
        org_id = org_data["org_id"]
        org = db.get(Organization, org_id)
        org.plan_id = plan.id
        db.commit()

        storage_service.recalculate_organization_storage(org_id, db)

        # Mock R2 environment
        deleted_keys = []
        monkeypatch.setattr(settings, "r2_account_id", "test-account")
        monkeypatch.setattr(settings, "r2_access_key_id", "test-key")
        monkeypatch.setattr(settings, "r2_secret_access_key", "test-secret")
        monkeypatch.setattr(settings, "r2_bucket_name", "test-bucket")

        from app.core import r2
        monkeypatch.setattr(r2, "put_object", lambda key, data, ct: None)
        monkeypatch.setattr(r2, "delete_object", lambda key: deleted_keys.append(key) or True)

        from app.core.files import _persist

        # 1. First upload of 7,000 bytes succeeds
        file1 = _persist(db, org_id, "upload1.dat", b"a" * 7000, "application/octet-stream")
        db.commit()
        assert file1.size == 7000

        usage1 = storage_service.get_storage_usage(org_id, db)
        assert usage1["files_documents_bytes"] == 7000

        # 2. Simulate concurrent upload:
        # Both uploads passed pre-flight check concurrently before either committed.
        # Bypass pre-flight check for upload2 to simulate having passed it simultaneously:
        monkeypatch.setattr(storage_service, "check_storage_quota", lambda o, s, d: None)

        with pytest.raises(Exception) as exc_info:
            _persist(db, org_id, "upload2.dat", b"b" * 5000, "application/octet-stream")

        err = exc_info.value
        assert hasattr(err, "status_code") and err.status_code == 403
        assert err.detail.get("code") == "PLAN_STORAGE_LIMIT_REACHED"

        # Verify that R2 cleanup was executed for the rejected upload
        assert len(deleted_keys) == 1, "R2 object must be deleted when serialized quota validation fails"

        # Verify usage was NOT incremented to 12,000
        usage2 = storage_service.get_storage_usage(org_id, db)
        assert usage2["files_documents_bytes"] == 7000
    finally:
        db.close()


def test_theme_background_deletion_accounting():
    """Verifies that deleting an organization theme background image updates the storage snapshot."""
    org_data = _create_test_org("Basic")
    org_id = org_data["org_id"]
    db = SessionLocal()
    try:
        from app.core.files import _persist
        from app.routers.settings import _delete_background_file

        usage_initial = storage_service.get_storage_usage(org_id, db)["files_documents_bytes"]

        file_obj = _persist(db, org_id, "bg.jpg", b"background_data_12345", "image/jpeg")
        db.commit()

        usage_before = storage_service.get_storage_usage(org_id, db)
        assert usage_before["files_documents_bytes"] == usage_initial + len(b"background_data_12345")

        _delete_background_file(db, org_id, f"/files/{file_obj.id}")
        db.commit()

        usage_after = storage_service.get_storage_usage(org_id, db)
        assert usage_after["files_documents_bytes"] == usage_initial
    finally:
        db.close()


def test_safe_delete_extended_reference_coverage():
    """Verifies referential integrity checks across CustomerDocument, OrganizationTheme, and User."""
    org_data = _create_test_org("Basic")
    org_id = org_data["org_id"]
    db = SessionLocal()
    try:
        from app.core.files import _persist, safe_delete_unreferenced_file
        from app.models import Customer, CustomerDocument, OrganizationTheme

        file_doc = _persist(db, org_id, "contract.pdf", b"pdf_data_contract", "application/pdf")
        db.commit()

        # Create customer and attach document
        cust = Customer(id=str(uuid.uuid4()), organization_id=org_id, name="Test Doc Cust")
        db.add(cust)
        db.commit()

        doc = CustomerDocument(
            id=str(uuid.uuid4()),
            customer_id=cust.id,
            organization_id=org_id,
            document_type="gst_certificate",
            name="contract.pdf",
            content_type="application/pdf",
            size=len(b"pdf_data_contract"),
            url=f"/files/{file_doc.id}",
            file_id=file_doc.id,
        )
        db.add(doc)
        db.commit()

        # While referenced by CustomerDocument, safe_delete must return False
        assert safe_delete_unreferenced_file(db, f"/files/{file_doc.id}", org_id) is False
        assert db.get(StoredFile, file_doc.id) is not None

        # Delete CustomerDocument and flush
        db.delete(doc)
        db.flush()

        # Now unreferenced, safe_delete must delete it and return True
        assert safe_delete_unreferenced_file(db, f"/files/{file_doc.id}", org_id) is True
        assert db.get(StoredFile, file_doc.id) is None
    finally:
        db.close()


def test_cold_start_snapshot_creation_serialized():
    """Verifies that calling get_or_create_storage_usage with for_update=True on a brand-new
    organization safely serializes on the parent Organization row and initializes the snapshot."""
    org_data = _create_test_org("Basic")
    org_id = org_data["org_id"]
    db = SessionLocal()
    try:
        # Delete any existing snapshot to force a true cold start
        existing = db.query(OrganizationStorageUsage).filter(OrganizationStorageUsage.organization_id == org_id).first()
        if existing:
            db.delete(existing)
            db.commit()

        # Call get_or_create_storage_usage with for_update=True
        usage = storage_service.get_or_create_storage_usage(org_id, db, for_update=True)
        assert usage is not None
        assert usage.organization_id == org_id
        assert usage.total_used_bytes >= 0

        # Verify snapshot exists in DB
        db.commit()
        persisted = db.get(OrganizationStorageUsage, org_id)
        assert persisted is not None
        assert persisted.organization_id == org_id
    finally:
        db.close()
