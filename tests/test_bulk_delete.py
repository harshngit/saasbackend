"""Focused test suite for the system-wide bulk-delete endpoints added on top of
the existing single-delete implementations (see the delete-API audit / this
change's implementation report).

Covers, per resource: a valid multi-record bulk delete, atomic rollback when
one requested record is a nonexistent id / a foreign-tenant id / protected by
the same guard single-delete already enforces, and a permission check. The
generic bulk-delete mechanics shared by every endpoint (empty-list rejection,
duplicate-id de-duplication) are covered once, on Customer, rather than
repeated identically ten times — every endpoint uses the exact same
`BulkDelete` schema and `dict.fromkeys` de-duplication.

Fixtures are inserted directly via SessionLocal (the same shortcut
tests/test_supplier_invoice_module.py already uses for its setup data) rather
than driven through each resource's full creation workflow: the thing under
test is the bulk-delete endpoint's validation/atomicity/tenant-isolation, not
each resource's own create flow (which has its own dedicated tests elsewhere).
"""

import os
import sys
import uuid
from datetime import datetime, timezone

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi.testclient import TestClient

from app.core.database import SessionLocal
from app.core.security import create_access_token
from app.main import app
from app.models import (
    Customer,
    CustomerPayment,
    Expense,
    FollowUp,
    GoodsReceiptNote,
    Lead,
    PurchaseInvoice,
    Quotation,
    SalesOrder,
    Supplier,
    SupplierInvoice,
    User,
    Visit,
)
from app.models.enums import UserRole

client = TestClient(app)


def check(msg: str, condition: bool, detail: str = "") -> None:
    """Print a PASS/FAIL line (matching this project's other test scripts) and
    — unlike a print-only checker — actually raise, so pytest correctly reports
    a failing check as a failing test rather than silently passing."""
    if condition:
        print(f"  PASS  {msg}")
    else:
        print(f"  FAIL  {msg}  {detail}")
    assert condition, f"{msg}  {detail}"


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
        admin = db.query(User).filter(User.email == email).first()
        org_id, admin_id = admin.organization_id, admin.id
    finally:
        db.close()
    return {"Authorization": f"Bearer {token}"}, org_id


def _no_permission_headers(org_id: str) -> dict:
    """A real staff user in this org with no custom role (no permissions at
    all granted) — every require_permission(module, "delete") check denies
    them, the same as any staff member nobody has granted delete to."""
    db = SessionLocal()
    try:
        user = User(
            organization_id=org_id,
            name="No Permission Staff",
            email=f"noperm_{uuid.uuid4().hex[:8]}@example.com",
            password_hash="not-a-real-hash",
            role=UserRole.SALES_OFFICER,
            role_id=None,
            system_role=None,
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        uid = user.id
    finally:
        db.close()
    token = create_access_token(uid, UserRole.SALES_OFFICER.value, org_id)
    return {"Authorization": f"Bearer {token}"}


def db_session():
    return SessionLocal()


# ---------------------------------------------------------------------------
# Test 0 — permission catalog fix
# ---------------------------------------------------------------------------
print("\n=== Test 0: permission catalog registers supplier_invoices/supplier_payments/accounts_payable ===")


def test_permission_catalog_registers_missing_modules():
    from app.core import permissions as p

    for m in ("supplier_invoices", "supplier_payments", "accounts_payable"):
        check(f"'{m}' is in MODULES", m in p.MODULES)
        check(f"'{m}' has a label", m in p.MODULE_LABELS)

    norm = p.normalize_permissions({"supplier_invoices": {"delete": True}})
    check(
        "a role can now actually be granted supplier_invoices:delete",
        norm.get("supplier_invoices", {}).get("delete") is True,
        str(norm),
    )
    full = p.full_access_matrix()
    check("Admin's full-access matrix includes supplier_invoices:delete", full["supplier_invoices"]["delete"] is True)

    defaults = p.default_role_matrices()
    check(
        "the catalog fix did not grant any seeded role new access",
        "supplier_invoices" not in defaults["Accountant"]
        and "supplier_payments" not in defaults["Accountant"]
        and "accounts_payable" not in defaults["Accountant"],
    )


# ---------------------------------------------------------------------------
# Test 1 — Customer bulk delete + cascade behaviour
# ---------------------------------------------------------------------------
print("\n=== Test 1: Customer bulk delete ===")


def test_customer_bulk_delete_valid_and_dedup_and_empty():
    auth, org_id = _register_org("cust_ok")
    db = db_session()
    try:
        c1 = Customer(organization_id=org_id, name="Cust One")
        c2 = Customer(organization_id=org_id, name="Cust Two")
        db.add_all([c1, c2])
        db.commit()
        db.refresh(c1)
        db.refresh(c2)
        id1, id2 = c1.id, c2.id
    finally:
        db.close()

    # empty list rejected before anything runs (shared BulkDelete schema, min_length=1)
    r = client.post("/customers/bulk-delete", json={"ids": []}, headers=auth)
    check("empty ids list -> 422", r.status_code == 422, r.text)

    # duplicate ids collapse to one deletion attempt each
    r = client.post("/customers/bulk-delete", json={"ids": [id1, id1, id2, id2]}, headers=auth)
    check("bulk delete 2 unique customers (4 ids incl. duplicates) -> 200", r.status_code == 200, r.text)
    check("deleted count reflects unique records, not the duplicate-inflated list", r.json().get("deleted") == 2, r.text)

    db = db_session()
    try:
        check("customer 1 is actually gone", db.get(Customer, id1) is None)
        check("customer 2 is actually gone", db.get(Customer, id2) is None)
    finally:
        db.close()


def test_customer_bulk_delete_cascade_vs_preserve():
    """Business decision under test: a customer's own data (payments, follow-ups,
    visits) is deleted with it; records that only reference the customer
    (SalesOrder) are preserved with customer_id set to NULL."""
    auth, org_id = _register_org("cust_cascade")
    db = db_session()
    try:
        cust = Customer(organization_id=org_id, name="Cascade Customer")
        db.add(cust)
        db.commit()
        db.refresh(cust)

        payment = CustomerPayment(
            organization_id=org_id, customer_id=cust.id, amount=100.0, payment_mode="cash",
        )
        visit = Visit(organization_id=org_id, customer_id=cust.id, visit_date=datetime.now(timezone.utc))
        follow_up = FollowUp(
            organization_id=org_id, customer_id=cust.id, title="Call back", due_date=datetime.now(timezone.utc),
        )
        order = SalesOrder(organization_id=org_id, order_number=f"SO-{uuid.uuid4().hex[:8]}", customer_id=cust.id)
        db.add_all([payment, visit, follow_up, order])
        db.commit()
        db.refresh(payment)
        db.refresh(visit)
        db.refresh(follow_up)
        db.refresh(order)
        cust_id, pay_id, visit_id, fu_id, order_id = cust.id, payment.id, visit.id, follow_up.id, order.id
    finally:
        db.close()

    r = client.delete(f"/customers/{cust_id}", headers=auth)
    check("DELETE /customers/{id} -> 204", r.status_code == 204, r.text)

    db = db_session()
    try:
        check("customer's own payment was deleted with it", db.get(CustomerPayment, pay_id) is None)
        check("customer's own visit was deleted with it", db.get(Visit, visit_id) is None)
        check("customer's own follow-up was deleted with it", db.get(FollowUp, fu_id) is None)
        order_after = db.get(SalesOrder, order_id)
        check("the sales order that only referenced the customer still exists", order_after is not None)
        check("the sales order's customer_id was set to NULL, not left dangling", order_after.customer_id is None)
    finally:
        db.close()


def test_customer_bulk_delete_nonexistent_id_is_atomic():
    auth, org_id = _register_org("cust_atomic")
    db = db_session()
    try:
        c1 = Customer(organization_id=org_id, name="Keep Me")
        db.add(c1)
        db.commit()
        db.refresh(c1)
        keep_id = c1.id
    finally:
        db.close()

    r = client.post("/customers/bulk-delete", json={"ids": [keep_id, "does-not-exist"]}, headers=auth)
    check("one nonexistent id in the batch -> 404, whole request fails", r.status_code == 404, r.text)

    db = db_session()
    try:
        check("the valid customer in that same batch was NOT deleted", db.get(Customer, keep_id) is not None)
    finally:
        db.close()


def test_customer_bulk_delete_cross_tenant_is_atomic_and_invisible():
    auth_a, org_a = _register_org("cust_tenant_a")
    auth_b, org_b = _register_org("cust_tenant_b")
    db = db_session()
    try:
        mine = Customer(organization_id=org_a, name="Mine")
        theirs = Customer(organization_id=org_b, name="Theirs")
        db.add_all([mine, theirs])
        db.commit()
        db.refresh(mine)
        db.refresh(theirs)
        mine_id, theirs_id = mine.id, theirs.id
    finally:
        db.close()

    r = client.post("/customers/bulk-delete", json={"ids": [mine_id, theirs_id]}, headers=auth_a)
    check("a foreign-org id in the batch -> 404 (not leaked as 403), whole request fails", r.status_code == 404, r.text)

    db = db_session()
    try:
        check("tenant A's own customer was NOT deleted (all-or-nothing)", db.get(Customer, mine_id) is not None)
        check("tenant B's customer was untouched", db.get(Customer, theirs_id) is not None)
    finally:
        db.close()

    # And confirm tenant B truly cannot reach it either way via their own call:
    r2 = client.post("/customers/bulk-delete", json={"ids": [theirs_id]}, headers=auth_b)
    check("tenant B deleting their own customer works normally", r2.status_code == 200, r2.text)


def test_customer_bulk_delete_requires_permission():
    auth, org_id = _register_org("cust_noperm")
    no_perm = _no_permission_headers(org_id)
    db = db_session()
    try:
        c = Customer(organization_id=org_id, name="Untouchable")
        db.add(c)
        db.commit()
        db.refresh(c)
        cid = c.id
    finally:
        db.close()

    r = client.post("/customers/bulk-delete", json={"ids": [cid]}, headers=no_perm)
    check("a user with no customers:delete permission -> 403", r.status_code == 403, r.text)

    db = db_session()
    try:
        check("nothing was deleted", db.get(Customer, cid) is not None)
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Test 2 — Supplier bulk delete (guard: historical purchases)
# ---------------------------------------------------------------------------
print("\n=== Test 2: Supplier bulk delete ===")


def test_supplier_bulk_delete_valid_and_protected_and_permission():
    auth, org_id = _register_org("supp")
    no_perm = _no_permission_headers(org_id)
    db = db_session()
    try:
        clean = Supplier(organization_id=org_id, name="Clean Supplier", is_active=True)
        protected = Supplier(organization_id=org_id, name="Protected Supplier", is_active=True)
        db.add_all([clean, protected])
        db.commit()
        db.refresh(clean)
        db.refresh(protected)
        pi = PurchaseInvoice(
            organization_id=org_id, supplier_id=protected.id,
            invoice_number=f"PI-{uuid.uuid4().hex[:8]}", status="draft",
        )
        db.add(pi)
        db.commit()
        clean_id, protected_id = clean.id, protected.id
    finally:
        db.close()

    # permission
    r = client.post("/suppliers/bulk-delete", json={"ids": [clean_id]}, headers=no_perm)
    check("no suppliers:delete permission -> 403", r.status_code == 403, r.text)

    # a protected supplier in the batch blocks the whole batch, same guard as single delete
    r = client.post("/suppliers/bulk-delete", json={"ids": [clean_id, protected_id]}, headers=auth)
    check("a supplier with historical purchases in the batch -> 400, whole batch fails", r.status_code == 400, r.text)
    db = db_session()
    try:
        check("the otherwise-deletable supplier in that batch was NOT deleted", db.get(Supplier, clean_id) is not None)
    finally:
        db.close()

    # valid, deletable batch
    r = client.post("/suppliers/bulk-delete", json={"ids": [clean_id]}, headers=auth)
    check("a clean supplier deletes normally", r.status_code == 200 and r.json()["deleted"] == 1, r.text)


# ---------------------------------------------------------------------------
# Test 3 — Quotation bulk delete (guard: accepted/converted)
# ---------------------------------------------------------------------------
print("\n=== Test 3: Quotation bulk delete ===")


def test_quotation_bulk_delete_valid_and_protected():
    auth, org_id = _register_org("quote")
    no_perm = _no_permission_headers(org_id)
    db = db_session()
    try:
        draft = Quotation(organization_id=org_id, quotation_number=f"QT-{uuid.uuid4().hex[:8]}", status="draft")
        accepted = Quotation(organization_id=org_id, quotation_number=f"QT-{uuid.uuid4().hex[:8]}", status="accepted")
        db.add_all([draft, accepted])
        db.commit()
        db.refresh(draft)
        db.refresh(accepted)
        draft_id, accepted_id = draft.id, accepted.id
    finally:
        db.close()

    r = client.post("/quotations/bulk-delete", json={"ids": [draft_id]}, headers=no_perm)
    check("no quotations:delete permission -> 403", r.status_code == 403, r.text)

    r = client.post("/quotations/bulk-delete", json={"ids": [draft_id, accepted_id]}, headers=auth)
    check("an accepted quotation in the batch -> 400, whole batch fails", r.status_code == 400, r.text)
    db = db_session()
    try:
        check("the draft quotation in that batch was NOT deleted", db.get(Quotation, draft_id) is not None)
    finally:
        db.close()

    r = client.post("/quotations/bulk-delete", json={"ids": [draft_id]}, headers=auth)
    check("a draft quotation deletes normally", r.status_code == 200 and r.json()["deleted"] == 1, r.text)


# ---------------------------------------------------------------------------
# Test 4 — Lead bulk delete (guard: converted)
# ---------------------------------------------------------------------------
print("\n=== Test 4: Lead bulk delete ===")


def test_lead_bulk_delete_valid_and_protected():
    auth, org_id = _register_org("lead")
    no_perm = _no_permission_headers(org_id)
    db = db_session()
    try:
        new_lead = Lead(organization_id=org_id, name="New Lead", mobile_number="9999999999", lead_status="new")
        won_lead = Lead(
            organization_id=org_id, name="Won Lead", mobile_number="9999999998", lead_status="won",
            converted_at=datetime.now(timezone.utc), customer_id=None,
        )
        db.add_all([new_lead, won_lead])
        db.commit()
        db.refresh(new_lead)
        db.refresh(won_lead)
        # Give the won lead a customer_id so is_converted() (whatever its exact
        # check) reads it as genuinely converted, not just status=="won".
        cust = Customer(organization_id=org_id, name="Converted From Lead")
        db.add(cust)
        db.commit()
        db.refresh(cust)
        won_lead.customer_id = cust.id
        db.commit()
        new_id, won_id = new_lead.id, won_lead.id
    finally:
        db.close()

    r = client.post("/leads/bulk-delete", json={"ids": [new_id]}, headers=no_perm)
    check("no leads:delete permission -> 403", r.status_code == 403, r.text)

    r = client.post("/leads/bulk-delete", json={"ids": [new_id, won_id]}, headers=auth)
    check("a converted lead in the batch -> 400, whole batch fails", r.status_code == 400, r.text)
    db = db_session()
    try:
        check("the new lead in that batch was NOT deleted", db.get(Lead, new_id) is not None)
    finally:
        db.close()

    r = client.post("/leads/bulk-delete", json={"ids": [new_id]}, headers=auth)
    check("a fresh lead deletes normally", r.status_code == 200 and r.json()["deleted"] == 1, r.text)


# ---------------------------------------------------------------------------
# Test 5 — GRN bulk delete (guard: confirmed / cancelled)
# ---------------------------------------------------------------------------
print("\n=== Test 5: GRN bulk delete ===")


def test_grn_bulk_delete_valid_and_protected():
    auth, org_id = _register_org("grn")
    no_perm = _no_permission_headers(org_id)
    db = db_session()
    try:
        supplier = Supplier(organization_id=org_id, name="GRN Supplier")
        db.add(supplier)
        db.commit()
        db.refresh(supplier)
        purchase = PurchaseInvoice(
            organization_id=org_id, supplier_id=supplier.id,
            invoice_number=f"PI-{uuid.uuid4().hex[:8]}", status="confirmed",
        )
        db.add(purchase)
        db.commit()
        db.refresh(purchase)

        draft_grn = GoodsReceiptNote(
            organization_id=org_id, purchase_id=purchase.id,
            grn_number=f"GRN-{uuid.uuid4().hex[:8]}", status="draft",
            received_date=datetime.now(timezone.utc),
        )
        confirmed_grn = GoodsReceiptNote(
            organization_id=org_id, purchase_id=purchase.id,
            grn_number=f"GRN-{uuid.uuid4().hex[:8]}", status="confirmed",
            received_date=datetime.now(timezone.utc),
        )
        db.add_all([draft_grn, confirmed_grn])
        db.commit()
        db.refresh(draft_grn)
        db.refresh(confirmed_grn)
        draft_id, confirmed_id = draft_grn.id, confirmed_grn.id
    finally:
        db.close()

    r = client.post("/grns/bulk-delete", json={"ids": [draft_id]}, headers=no_perm)
    check("no grn:delete permission -> 403", r.status_code == 403, r.text)

    r = client.post("/grns/bulk-delete", json={"ids": [draft_id, confirmed_id]}, headers=auth)
    check("a confirmed GRN in the batch -> 400, whole batch fails", r.status_code == 400, r.text)
    db = db_session()
    try:
        check("the draft GRN in that batch was NOT deleted", db.get(GoodsReceiptNote, draft_id) is not None)
    finally:
        db.close()

    r = client.post("/grns/bulk-delete", json={"ids": [draft_id]}, headers=auth)
    check("a draft GRN deletes normally", r.status_code == 200 and r.json()["deleted"] == 1, r.text)


# ---------------------------------------------------------------------------
# Test 6 — PurchaseInvoice bulk delete (guard: approved/confirmed/closed)
# ---------------------------------------------------------------------------
print("\n=== Test 6: PurchaseInvoice bulk delete ===")


def test_purchase_bulk_delete_valid_and_protected():
    auth, org_id = _register_org("purch")
    no_perm = _no_permission_headers(org_id)
    db = db_session()
    try:
        supplier = Supplier(organization_id=org_id, name="Purchase Supplier")
        db.add(supplier)
        db.commit()
        db.refresh(supplier)
        draft = PurchaseInvoice(
            organization_id=org_id, supplier_id=supplier.id,
            invoice_number=f"PI-{uuid.uuid4().hex[:8]}", status="draft",
        )
        confirmed = PurchaseInvoice(
            organization_id=org_id, supplier_id=supplier.id,
            invoice_number=f"PI-{uuid.uuid4().hex[:8]}", status="confirmed",
        )
        db.add_all([draft, confirmed])
        db.commit()
        db.refresh(draft)
        db.refresh(confirmed)
        draft_id, confirmed_id = draft.id, confirmed.id
    finally:
        db.close()

    for path in ("/purchase-invoices/bulk-delete", "/purchases/bulk-delete"):
        r = client.post(path, json={"ids": []}, headers=auth)
        check(f"{path}: empty ids -> 422 (dual-mounted router shares the same schema)", r.status_code == 422, r.text)

    r = client.post("/purchase-invoices/bulk-delete", json={"ids": [draft_id]}, headers=no_perm)
    check("no purchases:delete permission -> 403", r.status_code == 403, r.text)

    r = client.post("/purchase-invoices/bulk-delete", json={"ids": [draft_id, confirmed_id]}, headers=auth)
    check("a confirmed purchase invoice in the batch -> 400, whole batch fails", r.status_code == 400, r.text)
    db = db_session()
    try:
        check("the draft purchase invoice in that batch was NOT deleted", db.get(PurchaseInvoice, draft_id) is not None)
    finally:
        db.close()

    r = client.post("/purchases/bulk-delete", json={"ids": [draft_id]}, headers=auth)
    check("a draft purchase invoice deletes normally via the /purchases alias", r.status_code == 200 and r.json()["deleted"] == 1, r.text)


# ---------------------------------------------------------------------------
# Test 7 — SupplierInvoice bulk delete (guard: recorded/cancelled)
# ---------------------------------------------------------------------------
print("\n=== Test 7: SupplierInvoice bulk delete ===")


def test_supplier_invoice_bulk_delete_valid_and_protected():
    auth, org_id = _register_org("supinv")
    no_perm = _no_permission_headers(org_id)
    db = db_session()
    try:
        supplier = Supplier(organization_id=org_id, name="SI Supplier")
        db.add(supplier)
        db.commit()
        db.refresh(supplier)
        purchase = PurchaseInvoice(
            organization_id=org_id, supplier_id=supplier.id,
            invoice_number=f"PI-{uuid.uuid4().hex[:8]}", status="confirmed",
        )
        db.add(purchase)
        db.commit()
        db.refresh(purchase)

        draft_si = SupplierInvoice(
            organization_id=org_id, supplier_id=supplier.id, purchase_id=purchase.id,
            supplier_invoice_number=f"SI-{uuid.uuid4().hex[:8]}", status="draft",
        )
        recorded_si = SupplierInvoice(
            organization_id=org_id, supplier_id=supplier.id, purchase_id=purchase.id,
            supplier_invoice_number=f"SI-{uuid.uuid4().hex[:8]}", status="recorded",
        )
        db.add_all([draft_si, recorded_si])
        db.commit()
        db.refresh(draft_si)
        db.refresh(recorded_si)
        draft_id, recorded_id = draft_si.id, recorded_si.id
    finally:
        db.close()

    r = client.post("/supplier-invoices/bulk-delete", json={"ids": [draft_id]}, headers=no_perm)
    check("no supplier_invoices:delete permission -> 403 (permission catalog fix in effect)", r.status_code == 403, r.text)

    r = client.post("/supplier-invoices/bulk-delete", json={"ids": [draft_id, recorded_id]}, headers=auth)
    check("a recorded supplier invoice in the batch -> 400, whole batch fails", r.status_code == 400, r.text)
    db = db_session()
    try:
        check("the draft supplier invoice in that batch was NOT deleted", db.get(SupplierInvoice, draft_id) is not None)
    finally:
        db.close()

    r = client.post("/supplier-invoices/bulk-delete", json={"ids": [draft_id]}, headers=auth)
    check("a draft supplier invoice deletes normally", r.status_code == 200 and r.json()["deleted"] == 1, r.text)


# ---------------------------------------------------------------------------
# Test 8 — Expense bulk delete (no guard, preserved as-is)
# ---------------------------------------------------------------------------
print("\n=== Test 8: Expense bulk delete ===")


def test_expense_bulk_delete_valid_and_cross_tenant():
    auth, org_id = _register_org("exp")
    auth_b, org_b = _register_org("exp_b")
    no_perm = _no_permission_headers(org_id)
    db = db_session()
    try:
        e1 = Expense(organization_id=org_id, category="Travel", amount=500.0)
        e2 = Expense(organization_id=org_id, category="Meals", amount=200.0)
        foreign = Expense(organization_id=org_b, category="Travel", amount=999.0)
        db.add_all([e1, e2, foreign])
        db.commit()
        db.refresh(e1)
        db.refresh(e2)
        db.refresh(foreign)
        id1, id2, foreign_id = e1.id, e2.id, foreign.id
    finally:
        db.close()

    r = client.post("/expenses/bulk-delete", json={"ids": [id1]}, headers=no_perm)
    check("no expenses:delete permission -> 403", r.status_code == 403, r.text)

    r = client.post("/expenses/bulk-delete", json={"ids": [id1, foreign_id]}, headers=auth)
    check("a foreign-org expense id in the batch -> 404, whole batch fails", r.status_code == 404, r.text)
    db = db_session()
    try:
        check("the own-org expense in that batch was NOT deleted", db.get(Expense, id1) is not None)
        check("the foreign expense is untouched", db.get(Expense, foreign_id) is not None)
    finally:
        db.close()

    r = client.post("/expenses/bulk-delete", json={"ids": [id1, id1, id2]}, headers=auth)
    check("valid multi-expense bulk delete with a duplicate id -> 200, deleted=2", r.status_code == 200 and r.json()["deleted"] == 2, r.text)


# ---------------------------------------------------------------------------
# Test 9 — Visit bulk delete
# ---------------------------------------------------------------------------
print("\n=== Test 9: Visit bulk delete ===")


def test_visit_bulk_delete_valid_and_permission():
    auth, org_id = _register_org("visit")
    no_perm = _no_permission_headers(org_id)
    db = db_session()
    try:
        v1 = Visit(organization_id=org_id, visit_date=datetime.now(timezone.utc))
        v2 = Visit(organization_id=org_id, visit_date=datetime.now(timezone.utc))
        db.add_all([v1, v2])
        db.commit()
        db.refresh(v1)
        db.refresh(v2)
        id1, id2 = v1.id, v2.id
    finally:
        db.close()

    r = client.post("/visits/bulk-delete", json={"ids": [id1]}, headers=no_perm)
    check("no visits:delete permission -> 403", r.status_code == 403, r.text)

    r = client.post("/visits/bulk-delete", json={"ids": [id1, id2]}, headers=auth)
    check("valid multi-visit bulk delete -> 200, deleted=2", r.status_code == 200 and r.json()["deleted"] == 2, r.text)
    db = db_session()
    try:
        check("both visits are actually gone", db.get(Visit, id1) is None and db.get(Visit, id2) is None)
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Test 10 — FollowUp bulk delete
# ---------------------------------------------------------------------------
print("\n=== Test 10: FollowUp bulk delete ===")


def test_follow_up_bulk_delete_valid_and_permission():
    auth, org_id = _register_org("fu")
    no_perm = _no_permission_headers(org_id)
    db = db_session()
    try:
        f1 = FollowUp(organization_id=org_id, title="Call", due_date=datetime.now(timezone.utc))
        f2 = FollowUp(organization_id=org_id, title="Email", due_date=datetime.now(timezone.utc))
        db.add_all([f1, f2])
        db.commit()
        db.refresh(f1)
        db.refresh(f2)
        id1, id2 = f1.id, f2.id
    finally:
        db.close()

    r = client.post("/follow-ups/bulk-delete", json={"ids": [id1]}, headers=no_perm)
    check("no follow_ups:delete permission -> 403", r.status_code == 403, r.text)

    r = client.post("/follow-ups/bulk-delete", json={"ids": [id1, id2]}, headers=auth)
    check("valid multi-follow-up bulk delete -> 200, deleted=2", r.status_code == 200 and r.json()["deleted"] == 2, r.text)
    db = db_session()
    try:
        check("both follow-ups are actually gone", db.get(FollowUp, id1) is None and db.get(FollowUp, id2) is None)
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Test 11 — Sales Order bulk delete still requires its permission (unchanged)
# ---------------------------------------------------------------------------
print("\n=== Test 11: Sales Order bulk/single delete permission unchanged (not re-implemented) ===")


def test_sales_order_delete_permission_unchanged():
    auth, org_id = _register_org("so_perm")
    no_perm = _no_permission_headers(org_id)
    db = db_session()
    try:
        order = SalesOrder(organization_id=org_id, order_number=f"SO-{uuid.uuid4().hex[:8]}")
        db.add(order)
        db.commit()
        db.refresh(order)
        oid = order.id
    finally:
        db.close()

    r = client.delete(f"/orders/{oid}", headers=no_perm)
    check("DELETE /orders/{id} still requires sales_orders:delete -> 403 for a user without it", r.status_code == 403, r.text)
    r = client.post("/orders/bulk-delete", json={"ids": [oid]}, headers=no_perm)
    check("POST /orders/bulk-delete still requires sales_orders:delete -> 403 for a user without it", r.status_code == 403, r.text)

    db = db_session()
    try:
        check("the order was untouched by either denied attempt", db.get(SalesOrder, oid) is not None)
    finally:
        db.close()
