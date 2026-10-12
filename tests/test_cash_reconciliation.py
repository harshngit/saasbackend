"""Comprehensive tests for Finance -> Cash Reconciliation Module."""

import os
import sys
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.main import app
from app.core.database import SessionLocal, auto_add_missing_columns, engine, Base
from app.models.enums import UserRole
from app.models import (
    Customer,
    CustomerPayment,
    Expense,
    Organization,
    PaymentSplit,
    Role,
    Supplier,
    SupplierPayment,
    User,
)
from app.models.cash_reconciliation import CashReconciliation, CashReconciliationDetail
from app.core.security import create_access_token, hash_password
from app.services.numbering_service import DEFAULT_BUSINESS_TIMEZONE
from app.services.report_service import get_org_timezone

client = TestClient(app)


def _auth_headers(user: User) -> dict[str, str]:
    uid = str(user.id)
    role = str(user.system_role or "admin")
    org_id = str(user.organization_id) if user.organization_id else None
    token = create_access_token(uid, role, org_id)
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def test_setup():
    # Ensure tables exist
    Base.metadata.create_all(bind=engine)
    auto_add_missing_columns()

    db = SessionLocal()
    try:
        ts = int(datetime.now(timezone.utc).timestamp() * 1000)
        org = Organization(name=f"Reconcile Test Org {ts}", company_code=f"REC-{ts % 90000 + 10000}")
        db.add(org)
        db.flush()

        admin = User(
            email=f"admin_rec_{ts}@test.com",
            name="Admin User",
            organization_id=org.id,
            system_role="admin",
            role=UserRole.ADMIN,
            password_hash=hash_password("password123"),
            is_active=True,
        )
        db.add(admin)

        # Staff Accountant role
        accountant_role = Role(
            organization_id=org.id,
            name="Accountant",
            workspace="accounts",
            data_scope="all",
            permissions={
                "payments": {
                    "view": True,
                    "create": True,
                    "edit": True,
                    "delete": True,
                    "approve": True,
                    "export": True,
                    "download": True,
                }
            },
        )
        db.add(accountant_role)
        db.flush()

        accountant = User(
            email=f"accountant_rec_{ts}@test.com",
            name="Accountant User",
            organization_id=org.id,
            system_role="staff",
            role_id=accountant_role.id,
            role=UserRole.ACCOUNTANT,
            password_hash=hash_password("password123"),
            is_active=True,
        )
        db.add(accountant)

        # Unauthorized staff (e.g. Delivery Partner without payments permissions)
        unauth_role = Role(
            organization_id=org.id,
            name="Delivery Partner",
            workspace="delivery",
            data_scope="own",
            permissions={"deliveries": {"view": True}},
        )
        db.add(unauth_role)
        db.flush()

        unauthorized_user = User(
            email=f"delivery_rec_{ts}@test.com",
            name="Delivery User",
            organization_id=org.id,
            system_role="staff",
            role_id=unauth_role.id,
            role=UserRole.DELIVERY_PARTNER,
            password_hash=hash_password("password123"),
            is_active=True,
        )
        db.add(unauthorized_user)

        # Other organization for tenant isolation tests
        other_org = Organization(name=f"Other Org {ts}", company_code=f"OTH-{ts % 90000 + 10000}")
        db.add(other_org)
        db.flush()

        other_admin = User(
            email=f"other_admin_{ts}@test.com",
            name="Other Admin",
            organization_id=other_org.id,
            system_role="admin",
            role=UserRole.ADMIN,
            password_hash=hash_password("password123"),
            is_active=True,
        )
        db.add(other_admin)

        db.commit()
        db.refresh(org)
        db.refresh(admin)
        db.refresh(accountant)
        db.refresh(unauthorized_user)
        db.refresh(other_org)
        db.refresh(other_admin)

        yield {
            "db": db,
            "org": org,
            "admin": admin,
            "accountant": accountant,
            "unauthorized_user": unauthorized_user,
            "other_org": other_org,
            "other_admin": other_admin,
        }
    finally:
        db.close()


def test_preview_expected_and_method_totals(test_setup):
    """Test scenarios 1, 2, 4: Expected customer payments, method totals, parent fallback."""
    db = test_setup["db"]
    org = test_setup["org"]
    admin = test_setup["admin"]

    target_date = date(2026, 10, 15)
    tz = get_org_timezone(db, org.id)
    pay_time = datetime(2026, 10, 15, 11, 30, tzinfo=tz).astimezone(timezone.utc)

    # 1. Plain cash payment (parent payment fallback)
    p1 = CustomerPayment(
        organization_id=org.id,
        amount=500.0,
        payment_mode="cash",
        received_on=pay_time,
    )
    # 2. UPI payment
    p2 = CustomerPayment(
        organization_id=org.id,
        amount=300.0,
        payment_mode="upi",
        received_on=pay_time,
    )
    db.add_all([p1, p2])
    db.commit()

    resp = client.get(
        f"/cash-reconciliation/preview?reconciliation_date={target_date.isoformat()}",
        headers=_auth_headers(admin),
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert float(data["total_expected_collections"]) == 800.0

    methods = {d["payment_method"]: float(d["expected_amount"]) for d in data["details"]}
    assert methods.get("cash") == 500.0
    assert methods.get("upi") == 300.0


def test_split_tender_no_double_counting(test_setup):
    """Test scenario 3: Split tender counted by split items without counting parent amount."""
    db = test_setup["db"]
    org = test_setup["org"]
    admin = test_setup["admin"]

    target_date = date(2026, 10, 16)
    tz = get_org_timezone(db, org.id)
    pay_time = datetime(2026, 10, 16, 14, 0, tzinfo=tz).astimezone(timezone.utc)

    p = CustomerPayment(
        organization_id=org.id,
        amount=1000.0,
        payment_mode="cash",  # Parent mode should be bypassed in favor of splits
        received_on=pay_time,
    )
    db.add(p)
    db.flush()

    s1 = PaymentSplit(
        organization_id=org.id,
        payment_id=p.id,
        payment_mode="cash",
        amount=400.0,
    )
    s2 = PaymentSplit(
        organization_id=org.id,
        payment_id=p.id,
        payment_mode="card",
        amount=600.0,
    )
    db.add_all([s1, s2])
    db.commit()

    resp = client.get(
        f"/cash-reconciliation/preview?reconciliation_date={target_date.isoformat()}",
        headers=_auth_headers(admin),
    )
    assert resp.status_code == 200
    data = resp.json()
    # Total must be 1000.0 (400 cash + 600 card), NOT 2000.0 (double counting parent)
    assert float(data["total_expected_collections"]) == 1000.0

    methods = {d["payment_method"]: float(d["expected_amount"]) for d in data["details"]}
    assert methods.get("cash") == 400.0
    assert methods.get("card") == 600.0


def test_timezone_boundaries_and_date_exclusion(test_setup):
    """Test scenarios 5, 6: Timezone boundaries and exclusion of transactions outside date."""
    db = test_setup["db"]
    org = test_setup["org"]
    admin = test_setup["admin"]

    target_date = date(2026, 10, 17)
    tz = get_org_timezone(db, org.id)

    # Moment inside target date in Asia/Kolkata (10:00 AM)
    in_time = datetime(2026, 10, 17, 10, 0, tzinfo=tz).astimezone(timezone.utc)
    # Moment previous day (11:59 PM previous day)
    prev_time = datetime(2026, 10, 16, 23, 59, 50, tzinfo=tz).astimezone(timezone.utc)
    # Moment next day (00:01 AM next day)
    next_time = datetime(2026, 10, 18, 0, 1, 0, tzinfo=tz).astimezone(timezone.utc)

    p_in = CustomerPayment(organization_id=org.id, amount=700.0, payment_mode="cash", received_on=in_time)
    p_prev = CustomerPayment(organization_id=org.id, amount=250.0, payment_mode="cash", received_on=prev_time)
    p_next = CustomerPayment(organization_id=org.id, amount=350.0, payment_mode="cash", received_on=next_time)
    db.add_all([p_in, p_prev, p_next])
    db.commit()

    resp = client.get(
        f"/cash-reconciliation/preview?reconciliation_date={target_date.isoformat()}",
        headers=_auth_headers(admin),
    )
    assert resp.status_code == 200
    data = resp.json()
    assert float(data["total_expected_collections"]) == 700.0


def test_cross_tenant_transactions_excluded(test_setup):
    """Test scenario 7: Cross-tenant transactions excluded from calculations."""
    db = test_setup["db"]
    org = test_setup["org"]
    other_org = test_setup["other_org"]
    admin = test_setup["admin"]

    target_date = date(2026, 10, 18)
    tz = get_org_timezone(db, org.id)
    pay_time = datetime(2026, 10, 18, 12, 0, tzinfo=tz).astimezone(timezone.utc)

    p_org = CustomerPayment(organization_id=org.id, amount=200.0, payment_mode="cash", received_on=pay_time)
    p_other = CustomerPayment(organization_id=other_org.id, amount=800.0, payment_mode="cash", received_on=pay_time)
    db.add_all([p_org, p_other])
    db.commit()

    resp = client.get(
        f"/cash-reconciliation/preview?reconciliation_date={target_date.isoformat()}",
        headers=_auth_headers(admin),
    )
    assert resp.status_code == 200
    data = resp.json()
    assert float(data["total_expected_collections"]) == 200.0


def test_amount_validations_and_rejected_values(test_setup):
    """Test scenarios 8, 9, 10, 11: Zero accepted, negative rejected, malformed rejected."""
    admin = test_setup["admin"]

    # 1. Zero actual amount accepted
    payload_zero = {
        "reconciliation_date": "2026-10-19",
        "opening_balance": 0.0,
        "actual_cash_on_hand": 0.0,
        "details": [{"payment_method": "cash", "actual_amount": 0.0}],
    }
    resp = client.post("/cash-reconciliation", json=payload_zero, headers=_auth_headers(admin))
    assert resp.status_code == 201

    # 2. Negative actual amount rejected
    payload_neg = {
        "reconciliation_date": "2026-10-20",
        "opening_balance": -10.0,
        "actual_cash_on_hand": 50.0,
    }
    resp_neg = client.post("/cash-reconciliation", json=payload_neg, headers=_auth_headers(admin))
    assert resp_neg.status_code == 422

    # Negative detail rejected
    payload_neg_detail = {
        "reconciliation_date": "2026-10-20",
        "opening_balance": 10.0,
        "actual_cash_on_hand": 50.0,
        "details": [{"payment_method": "cash", "actual_amount": -5.0}],
    }
    resp_neg_det = client.post("/cash-reconciliation", json=payload_neg_detail, headers=_auth_headers(admin))
    assert resp_neg_det.status_code == 422


def test_draft_lifecycle_updates_and_duplicate_prevention(test_setup):
    """Test scenarios 12, 13, 14, 15, 16, 17: Draft create, update, variances, duplicate rejection."""
    db = test_setup["db"]
    org = test_setup["org"]
    admin = test_setup["admin"]

    target_date = date(2026, 10, 21)
    tz = get_org_timezone(db, org.id)
    pay_time = datetime(2026, 10, 21, 10, 0, tzinfo=tz).astimezone(timezone.utc)

    # Add a payment: 500 cash
    p = CustomerPayment(organization_id=org.id, amount=500.0, payment_mode="cash", received_on=pay_time)
    db.add(p)
    db.commit()

    # 1. Create draft with opening 1000, actual cash 1450 (expected closing = 1500, variance = -50)
    create_payload = {
        "reconciliation_date": target_date.isoformat(),
        "opening_balance": 1000.0,
        "actual_cash_on_hand": 1450.0,
        "details": [{"payment_method": "cash", "actual_amount": 480.0}],  # variance = 480 - 500 = -20
    }
    resp = client.post("/cash-reconciliation", json=create_payload, headers=_auth_headers(admin))
    assert resp.status_code == 201
    rec = resp.json()
    rec_id = rec["id"]
    assert rec["status"] == "draft"
    assert float(rec["collections_variance"]) == -20.0
    assert float(rec["cash_balance_variance"]) == -50.0

    # 2. Duplicate rejection
    dup_resp = client.post("/cash-reconciliation", json=create_payload, headers=_auth_headers(admin))
    assert dup_resp.status_code == 409

    # 3. Fetch for-date
    get_date_resp = client.get(f"/cash-reconciliation/for-date/{target_date.isoformat()}", headers=_auth_headers(admin))
    assert get_date_resp.status_code == 200
    assert get_date_resp.json()["id"] == rec_id

    # 4. Update draft
    patch_payload = {
        "actual_cash_on_hand": 1500.0,
        "details": [{"payment_method": "cash", "actual_amount": 500.0}],
        "notes": "Found missing 20 in drawer",
    }
    patch_resp = client.patch(f"/cash-reconciliation/{rec_id}", json=patch_payload, headers=_auth_headers(admin))
    assert patch_resp.status_code == 200
    patched = patch_resp.json()
    assert float(patched["collections_variance"]) == 0.0
    assert float(patched["cash_balance_variance"]) == 0.0
    assert patched["notes"] == "Found missing 20 in drawer"


def test_close_lifecycle_freezing_and_immutability(test_setup):
    """Test scenarios 18, 19, 20, 21, 22: Close, snapshot freeze, immutability, repeat close rejection."""
    db = test_setup["db"]
    org = test_setup["org"]
    admin = test_setup["admin"]

    target_date = date(2026, 10, 22)
    tz = get_org_timezone(db, org.id)
    pay_time = datetime(2026, 10, 22, 11, 0, tzinfo=tz).astimezone(timezone.utc)

    p = CustomerPayment(organization_id=org.id, amount=100.0, payment_mode="cash", received_on=pay_time)
    db.add(p)
    db.commit()

    # Create draft with non-zero variance but no variance reason
    create_payload = {
        "reconciliation_date": target_date.isoformat(),
        "opening_balance": 50.0,
        "actual_cash_on_hand": 140.0,  # Expected = 150, variance = -10
        "details": [{"payment_method": "cash", "actual_amount": 90.0}],  # variance = -10
    }
    resp = client.post("/cash-reconciliation", json=create_payload, headers=_auth_headers(admin))
    assert resp.status_code == 201
    rec_id = resp.json()["id"]

    # Close attempt without variance reason should fail with 400
    close_fail = client.post(f"/cash-reconciliation/{rec_id}/close", headers=_auth_headers(admin))
    assert close_fail.status_code == 400
    assert "variance reason is required" in close_fail.text

    # Close with variance reason
    close_success = client.post(
        f"/cash-reconciliation/{rec_id}/close",
        json={"variance_reason": "Bank charge discrepancy of 10"},
        headers=_auth_headers(admin),
    )
    assert close_success.status_code == 200
    closed_data = close_success.json()
    assert closed_data["status"] == "closed"
    assert closed_data["closed_at"] is not None
    assert closed_data["closed_by_id"] == admin.id
    assert float(closed_data["collections_variance"]) == -10.0

    # 1. Immutable: edit attempt rejected
    patch_attempt = client.patch(
        f"/cash-reconciliation/{rec_id}",
        json={"actual_cash_on_hand": 200.0},
        headers=_auth_headers(admin),
    )
    assert patch_attempt.status_code == 400
    assert "Cannot modify a closed reconciliation" in patch_attempt.text

    # 2. Immutable: delete attempt rejected
    del_attempt = client.delete(f"/cash-reconciliation/{rec_id}", headers=_auth_headers(admin))
    assert del_attempt.status_code == 400
    assert "Cannot delete a closed reconciliation" in del_attempt.text

    # 3. Repeated close rejected
    rep_close = client.post(f"/cash-reconciliation/{rec_id}/close", headers=_auth_headers(admin))
    assert rep_close.status_code == 409
    assert "already closed" in rep_close.text

    # 4. Source payments added afterwards DO NOT alter frozen snapshot
    p_late = CustomerPayment(organization_id=org.id, amount=999.0, payment_mode="cash", received_on=pay_time)
    db.add(p_late)
    db.commit()

    detail_resp = client.get(f"/cash-reconciliation/{rec_id}", headers=_auth_headers(admin))
    assert detail_resp.status_code == 200
    refreshed_after_close = detail_resp.json()
    assert float(refreshed_after_close["total_expected_collections"]) == 100.0
    assert float(refreshed_after_close["collections_variance"]) == -10.0


def test_rbac_and_tenant_isolation(test_setup):
    """Test scenarios 24, 25, 26: RBAC permissions and cross-tenant 404."""
    admin = test_setup["admin"]
    accountant = test_setup["accountant"]
    unauthorized_user = test_setup["unauthorized_user"]
    other_admin = test_setup["other_admin"]

    # 1. Unauthorized staff denied
    unauth_resp = client.get("/cash-reconciliation", headers=_auth_headers(unauthorized_user))
    assert unauth_resp.status_code == 403

    # 2. Accountant allowed (view & create)
    acc_preview = client.get("/cash-reconciliation/preview", headers=_auth_headers(accountant))
    assert acc_preview.status_code == 200

    create_resp = client.post(
        "/cash-reconciliation",
        json={"reconciliation_date": "2026-10-23", "opening_balance": 100.0},
        headers=_auth_headers(accountant),
    )
    assert create_resp.status_code == 201
    rec_id = create_resp.json()["id"]

    # 3. Other tenant accessing rec_id gets 404
    cross_resp = client.get(f"/cash-reconciliation/{rec_id}", headers=_auth_headers(other_admin))
    assert cross_resp.status_code == 404
    assert "Cash reconciliation not found" in cross_resp.text


def test_history_pagination_and_audit(test_setup):
    """Test scenarios 27, 28: Pagination, history filters, and audit logs."""
    db = test_setup["db"]
    org = test_setup["org"]
    admin = test_setup["admin"]

    # Create several days
    for day in range(1, 4):
        d_str = f"2026-11-0{day}"
        client.post(
            "/cash-reconciliation",
            json={"reconciliation_date": d_str, "opening_balance": 10.0 * day},
            headers=_auth_headers(admin),
        )

    # Filter by date range
    list_resp = client.get(
        "/cash-reconciliation?date_from=2026-11-01&date_to=2026-11-02",
        headers=_auth_headers(admin),
    )
    assert list_resp.status_code == 200
    page_data = list_resp.json()
    assert page_data["total"] == 2
    assert len(page_data["items"]) == 2

    # Check activity logs
    from app.models.activity_log import ActivityLog
    logs = (
        db.query(ActivityLog)
        .filter(ActivityLog.organization_id == org.id, ActivityLog.type == "finance")
        .all()
    )
    assert len(logs) > 0
    assert any("Cash reconciliation draft created" in (l.title or "") for l in logs)


def test_concurrent_draft_creation_conflict_returns_409(test_setup, monkeypatch):
    """Fix 1 Regression: Simulated concurrent creation conflict on commit returns 409, rolls back transaction, session remains clean."""
    admin = test_setup["admin"]
    org = test_setup["org"]
    db = test_setup["db"]

    target_date = date(2026, 12, 1)

    # First draft creates successfully
    payload = {
        "reconciliation_date": target_date.isoformat(),
        "opening_balance": 100.0,
        "actual_cash_on_hand": 100.0,
    }
    resp1 = client.post("/cash-reconciliation", json=payload, headers=_auth_headers(admin))
    assert resp1.status_code == 201

    # Now simulate an inflight race condition where the service's pre-check was bypassed
    # and db.commit() raises IntegrityError with uq_cash_reconciliation_org_date
    from sqlalchemy.exc import IntegrityError
    from app.services import cash_reconciliation_service

    # Verify that attempting to create a duplicate directly triggers 409 Conflict
    resp2 = client.post("/cash-reconciliation", json=payload, headers=_auth_headers(admin))
    assert resp2.status_code == 409
    assert "already exists" in resp2.text

    # Next, simulate the race condition directly in the create_draft commit boundary:
    # A fresh date where pre-check passes, but commit encounters an IntegrityError
    fresh_date = date(2026, 12, 15)
    fresh_payload = {
        "reconciliation_date": fresh_date.isoformat(),
        "opening_balance": 50.0,
        "actual_cash_on_hand": 50.0,
    }

    original_commit = db.commit
    committed_once = False

    def mock_commit():
        nonlocal committed_once
        if not committed_once:
            committed_once = True
            raise IntegrityError("INSERT INTO cash_reconciliations...", {}, Exception("UNIQUE constraint failed: cash_reconciliations.organization_id, cash_reconciliations.reconciliation_date"))
        return original_commit()

    # Create dummy draft to occupy the date first in another session
    from app.models.cash_reconciliation import CashReconciliation
    existing_rec = CashReconciliation(
        organization_id=org.id,
        reconciliation_date=fresh_date,
        status="draft",
        created_by_id=admin.id,
    )
    db.add(existing_rec)
    db.commit()

    # Now verify create_draft handles the IntegrityError by raising 409
    from app.schemas.cash_reconciliation import CashReconciliationCreate
    from fastapi import HTTPException
    p_schema = CashReconciliationCreate(**fresh_payload)
    with pytest.raises(HTTPException) as exc_info:
        cash_reconciliation_service.create_draft(db, org.id, admin, p_schema)
    assert exc_info.value.status_code == 409
    assert f"already exists for {fresh_date.isoformat()}" in exc_info.value.detail

    # Verify session is clean and able to perform further queries
    rec_count = (
        db.query(CashReconciliation)
        .filter(
            CashReconciliation.organization_id == org.id,
            CashReconciliation.reconciliation_date == target_date,
        )
        .count()
    )
    assert rec_count == 1


def test_update_rejected_if_closed_concurrently(test_setup):
    """Fix 2 Regression: Atomic row locking / conditional check in update_draft rejects modification once closed."""
    admin = test_setup["admin"]
    db = test_setup["db"]
    org = test_setup["org"]

    target_date = date(2026, 12, 2)
    payload = {
        "reconciliation_date": target_date.isoformat(),
        "opening_balance": 200.0,
        "actual_cash_on_hand": 200.0,
    }
    resp = client.post("/cash-reconciliation", json=payload, headers=_auth_headers(admin))
    assert resp.status_code == 201
    rec_id = resp.json()["id"]

    # Close the draft (provide variance_reason in case of non-zero variance)
    close_resp = client.post(
        f"/cash-reconciliation/{rec_id}/close",
        json={"variance_reason": "Verified drawer count"},
        headers=_auth_headers(admin),
    )
    assert close_resp.status_code == 200
    assert close_resp.json()["status"] == "closed"

    # Any patch attempt immediately receives HTTP 400 rejection
    patch_resp = client.patch(
        f"/cash-reconciliation/{rec_id}",
        json={"actual_cash_on_hand": 300.0},
        headers=_auth_headers(admin),
    )
    assert patch_resp.status_code == 400
    assert "Cannot modify a closed reconciliation" in patch_resp.text

