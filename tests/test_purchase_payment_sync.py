"""Purchase -> Supplier Invoice -> Supplier Payment -> Accounts Payable: one financial truth.

Supplier payment allocations are the only record of payment. A supplier invoice's
amount_paid is the sum of its allocations from recorded payments; a purchase's is the sum
over its non-cancelled supplier invoices; Accounts Payable reads the supplier invoice. These
tests drive the real API and check that purchase detail, purchase list, supplier invoice and
Accounts Payable agree after every step.
"""
import os
import sys
import uuid
from datetime import datetime, timezone

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi.testclient import TestClient

from app.core.database import SessionLocal
from app.main import app
from app.models import ActivityLog, Product, PurchaseInvoice, Supplier, SupplierPayment, User, Warehouse
from app.models.supplier_invoice import SupplierInvoice
from app.models.supplier_payment_allocation import SupplierPaymentAllocation
from app.seed import main as seed_main
from app.services import supplier_payment_service

seed_main()
client = TestClient(app)


# ------------------------------------------------------------------ helpers


def _org(prefix: str = "Sync Firm"):
    email = f"sync_{uuid.uuid4().hex[:10]}@example.com"
    r = client.post("/auth/register", json={
        "organization_name": f"{prefix} {uuid.uuid4().hex[:6]}", "admin_name": "Sync Admin",
        "email": email, "password": "Password123!", "role": "admin",
    })
    assert r.status_code == 201, r.text
    auth = {"Authorization": f"Bearer {r.json()['tokens']['access_token']}"}
    db = SessionLocal()
    try:
        org_id = db.query(User).filter(User.email == email).first().organization_id
        wh = Warehouse(organization_id=org_id, name="Main Warehouse", code=f"WH-{uuid.uuid4().hex[:4]}")
        sup = Supplier(organization_id=org_id, name=f"Acme Supplies {uuid.uuid4().hex[:4]}", is_active=True)
        prod = Product(organization_id=org_id, name="Widget", sku=f"SKU-{uuid.uuid4().hex[:6]}", price=1000.0)
        db.add_all([wh, sup, prod])
        db.commit()
        return {"auth": auth, "org_id": org_id, "wh": wh.id, "wh_name": wh.name, "sup": sup.id,
                "sup_name": sup.name, "prod": prod.id}
    finally:
        db.close()


def _purchase(env, qty: int = 10, price: float = 1000.0, confirm: bool = True, **extra) -> dict:
    body = {"invoice_number": f"TEST-PUR-{uuid.uuid4().hex[:6]}", "supplier_id": env["sup"],
            "warehouse_id": env["wh"], "items": [{"product_id": env["prod"], "ordered_qty": qty, "purchase_price": price}]}
    body.update(extra)
    r = client.post("/purchases", json=body, headers=env["auth"])
    assert r.status_code == 201, r.text
    purchase = r.json()
    if confirm:
        assert client.post(f"/purchases/{purchase['id']}/confirm", headers=env["auth"]).status_code == 200
    return purchase


def _auto_invoice_id(env, purchase_id: str) -> str | None:
    db = SessionLocal()
    try:
        inv = db.query(SupplierInvoice).filter(
            SupplierInvoice.organization_id == env["org_id"], SupplierInvoice.purchase_id == purchase_id,
            SupplierInvoice.status != "cancelled").first()
        return inv.id if inv else None
    finally:
        db.close()


def _receive(env, purchase: dict, qty: int) -> None:
    item_id = purchase["items"][0]["id"]
    grn = client.post("/grns", json={"purchase_id": purchase["id"], "supplier_id": env["sup"], "warehouse_id": env["wh"],
                                     "items": [{"purchase_item_id": item_id, "product_id": env["prod"], "received_qty": qty}]},
                      headers=env["auth"])
    assert grn.status_code == 201, grn.text
    assert client.post(f"/grns/{grn.json()['id']}/confirm", headers=env["auth"]).status_code == 200


def _pay(env, allocations: list[tuple[str, float]]) -> dict:
    amount = round(sum(a for _, a in allocations), 2)
    r = client.post("/supplier-payments", json={
        "supplier_id": env["sup"], "amount": amount, "payment_method": "bank_transfer",
        "allocations": [{"supplier_invoice_id": i, "amount": a} for i, a in allocations]}, headers=env["auth"])
    assert r.status_code == 201, r.text
    return r.json()


def _void(env, payment_id: str):
    return client.post(f"/supplier-payments/{payment_id}/void", json={"reason": "test"}, headers=env["auth"])


def _state(env, purchase_id: str, invoice_ids: list[str]) -> dict:
    """Everything the API says about this purchase's money, from every angle."""
    detail = client.get(f"/purchases/{purchase_id}", headers=env["auth"]).json()
    listed = client.get("/purchases", params={"search": detail["purchase_number"]}, headers=env["auth"]).json()["items"]
    row = next(p for p in listed if p["id"] == purchase_id)
    invoices = [client.get(f"/supplier-invoices/{i}", headers=env["auth"]).json() for i in invoice_ids]
    ap = client.get("/accounts-payable", params={"supplier_id": env["sup"]}, headers=env["auth"]).json()["items"]
    ap_by_id = {a["supplier_invoice_id"]: a for a in ap}
    return {"detail": detail, "row": row, "invoices": invoices, "ap": ap_by_id}


def _money(p: dict) -> tuple:
    return round(p["amount_paid"], 2), round(p["outstanding_balance"], 2), p["payment_status"]


def _assert_in_sync(state: dict, paid: float, outstanding: float, status: str) -> None:
    assert _money(state["detail"]) == (paid, outstanding, status), state["detail"]
    assert _money(state["row"]) == _money(state["detail"])  # list == detail
    assert round(sum(i["amount_paid"] for i in state["invoices"]), 2) == paid  # purchase == its invoices
    for inv in state["invoices"]:
        ap = state["ap"].get(inv["id"])
        if inv["outstanding_amount"] > 0:  # open payables list
            assert ap is not None
            assert (round(ap["amount_paid"], 2), round(ap["outstanding_amount"], 2), ap["payment_status"]) == \
                (round(inv["amount_paid"], 2), round(inv["outstanding_amount"], 2), inv["payment_status"])


# ------------------------------------------------------------------ tests


def test_new_purchase_is_unpaid_everywhere_and_invoice_carries_its_links():
    env = _org()
    inv_date = "2026-10-20T00:00:00+00:00"
    purchase = _purchase(env, invoice_date=inv_date, purchase_date="2026-10-26T00:00:00+00:00")
    si_id = _auto_invoice_id(env, purchase["id"])
    assert si_id is not None

    state = _state(env, purchase["id"], [si_id])
    _assert_in_sync(state, 0.0, 10000.0, "unpaid")
    si = state["invoices"][0]
    assert si["supplier_id"] == env["sup"] and si["supplier_name"] == env["sup_name"]
    assert si["purchase_id"] == purchase["id"] and si["purchase_number"] == purchase["purchase_number"]
    assert si["supplier_invoice_number"] == purchase["invoice_number"]
    # The supplier's invoice date as entered -- not the purchase date
    assert si["supplier_invoice_date"].startswith("2026-10-20") and si["invoice_date"].startswith("2026-10-20")
    # No goods received yet: prices match, quantities can't -- so pending, not matched
    assert si["grand_total"] == 10000.0 and si["verification_status"] == "pending"
    assert (si["price_match"], si["quantity_match"]) == (True, False)
    assert (si["amount_paid"], si["outstanding_amount"], si["payment_status"]) == (0.0, 10000.0, "unpaid")
    # Warehouse name in list and detail alike
    assert state["detail"]["warehouse_name"] == env["wh_name"] == state["row"]["warehouse_name"]
    assert state["row"]["warehouse_id"] == env["wh"]


def test_partial_then_full_payment_and_voids_reverse_exactly_once():
    env = _org()
    purchase = _purchase(env)
    si = _auto_invoice_id(env, purchase["id"])
    _receive(env, purchase, 4)
    _receive(env, purchase, 6)

    p1 = _pay(env, [(si, 4000.0)])
    _assert_in_sync(_state(env, purchase["id"], [si]), 4000.0, 6000.0, "partially_paid")

    # Remaining 6,000 from the purchase screen -> a real supplier payment
    r = client.patch(f"/purchases/{purchase['id']}/payment-status", json={"payment_status": "paid"}, headers=env["auth"])
    assert r.status_code == 200, r.text
    _assert_in_sync(_state(env, purchase["id"], [si]), 10000.0, 0.0, "paid")
    db = SessionLocal()
    try:
        payments = db.query(SupplierPayment).filter(SupplierPayment.organization_id == env["org_id"]).all()
        assert sorted(p.amount for p in payments) == [4000.0, 6000.0]
        p2_id = next(p.id for p in payments if p.amount == 6000.0)
    finally:
        db.close()

    # Closed + fully received + paid; closing never required payment
    assert client.post(f"/purchases/{purchase['id']}/close", headers=env["auth"]).status_code == 200
    detail = client.get(f"/purchases/{purchase['id']}", headers=env["auth"]).json()
    assert (detail["status"], detail["receiving_status"]) == ("closed", "fully_received")

    assert _void(env, p2_id).status_code == 200
    _assert_in_sync(_state(env, purchase["id"], [si]), 4000.0, 6000.0, "partially_paid")
    assert _void(env, p2_id).status_code == 400  # second void refused...
    _assert_in_sync(_state(env, purchase["id"], [si]), 4000.0, 6000.0, "partially_paid")  # ...and changed nothing
    assert _void(env, p1["id"]).status_code == 200
    _assert_in_sync(_state(env, purchase["id"], [si]), 0.0, 10000.0, "unpaid")  # never negative


def test_closed_fully_received_unpaid_is_valid():
    env = _org()
    purchase = _purchase(env)
    si = _auto_invoice_id(env, purchase["id"])
    _receive(env, purchase, 10)
    assert client.post(f"/purchases/{purchase['id']}/close", headers=env["auth"]).status_code == 200
    _assert_in_sync(_state(env, purchase["id"], [si]), 0.0, 10000.0, "unpaid")


def test_purchase_payment_endpoint_only_records_real_supplier_payments():
    env = _org()
    # Payment can't be typed in at creation
    r = client.post("/purchases", json={"invoice_number": "X-1", "supplier_id": env["sup"], "amount_paid": 500,
                                        "items": [{"product_id": env["prod"], "ordered_qty": 1, "purchase_price": 500}]},
                    headers=env["auth"])
    assert r.status_code == 400 and "amount_paid" in r.text

    draft = _purchase(env, confirm=False)
    r = client.patch(f"/purchases/{draft['id']}/payment-status", json={"payment_status": "paid"}, headers=env["auth"])
    assert r.status_code == 400 and "no recorded supplier invoice" in r.text

    purchase = _purchase(env)
    si = _auto_invoice_id(env, purchase["id"])
    url = f"/purchases/{purchase['id']}/payment-status"

    def payments() -> list[tuple[float, list]]:
        db = SessionLocal()
        try:
            rows = db.query(SupplierPayment).filter(SupplierPayment.organization_id == env["org_id"],
                                                    SupplierPayment.status == "recorded").all()
            return sorted((p.amount, [(a.supplier_invoice_id, a.amount) for a in p.allocations]) for p in rows)
        finally:
            db.close()

    r = client.patch(url, json={"payment_status": "partial", "amount_paid": 2500, "payment_method": "upi",
                                "payment_reference": "UTR123"}, headers=env["auth"])
    assert r.status_code == 200, r.text
    assert payments() == [(2500.0, [(si, 2500.0)])]
    _assert_in_sync(_state(env, purchase["id"], [si]), 2500.0, 7500.0, "partially_paid")

    # Re-sending the same total adds nothing
    assert client.patch(url, json={"payment_status": "partially_paid", "amount_paid": 2500}, headers=env["auth"]).status_code == 200
    assert payments() == [(2500.0, [(si, 2500.0)])]
    # Lowering, overpaying or a partial without an amount are refused and change nothing
    for body in ({"payment_status": "partial", "amount_paid": 1000},
                 {"payment_status": "unpaid"},
                 {"payment_status": "paid", "amount_paid": 20000},
                 {"payment_status": "partially_paid"}):
        assert client.patch(url, json=body, headers=env["auth"]).status_code == 400, body
    assert payments() == [(2500.0, [(si, 2500.0)])]
    _assert_in_sync(_state(env, purchase["id"], [si]), 2500.0, 7500.0, "partially_paid")


def test_multiple_supplier_invoices_on_one_purchase():
    env = _org()
    purchase = _purchase(env)
    auto = _auto_invoice_id(env, purchase["id"])
    assert client.post(f"/supplier-invoices/{auto}/cancel", headers=env["auth"]).status_code == 200
    _receive(env, purchase, 10)
    item_id = purchase["items"][0]["id"]
    ids = []
    for qty, num in ((4, "SUP-A"), (6, "SUP-B")):
        r = client.post("/supplier-invoices", json={"supplier_id": env["sup"], "purchase_id": purchase["id"],
                                                    "supplier_invoice_number": f"{num}-{uuid.uuid4().hex[:4]}",
                                                    "items": [{"purchase_item_id": item_id, "billed_qty": qty, "unit_price": 1000}]},
                        headers=env["auth"])
        assert r.status_code == 201, r.text
        assert client.post(f"/supplier-invoices/{r.json()['id']}/record", headers=env["auth"]).status_code == 200
        ids.append(r.json()["id"])
    a, b = ids

    _pay(env, [(a, 4000.0), (b, 1000.0)])
    state = _state(env, purchase["id"], ids)
    _assert_in_sync(state, 5000.0, 5000.0, "partially_paid")
    assert [i["payment_status"] for i in state["invoices"]] == ["paid", "partially_paid"]

    # Paying the rest from the purchase allocates only to what is still open (invoice B)
    r = client.patch(f"/purchases/{purchase['id']}/payment-status", json={"payment_status": "paid"}, headers=env["auth"])
    assert r.status_code == 200, r.text
    db = SessionLocal()
    try:
        allocs = db.query(SupplierPaymentAllocation).filter(SupplierPaymentAllocation.organization_id == env["org_id"]).all()
        assert sorted((x.supplier_invoice_id == b, x.amount) for x in allocs) == [(False, 4000.0), (True, 1000.0), (True, 5000.0)]
    finally:
        db.close()
    _assert_in_sync(_state(env, purchase["id"], ids), 10000.0, 0.0, "paid")


def test_legacy_purchase_figures_never_override_the_payment_ledger():
    env = _org()
    purchase = _purchase(env)
    si = _auto_invoice_id(env, purchase["id"])
    other = _purchase(env)
    other_si = _auto_invoice_id(env, other["id"])
    # Simulate the production record: Paid on the purchase, nothing behind it; and an old
    # auto-created invoice that copied a purchase amount with no allocation.
    db = SessionLocal()
    try:
        db.get(PurchaseInvoice, purchase["id"]).amount_paid = 10000.0
        db.get(PurchaseInvoice, purchase["id"]).payment_status = "paid"
        db.get(SupplierInvoice, other_si).amount_paid = 3000.0
        db.commit()
    finally:
        db.close()

    findings = {f["id"]: f for f in supplier_payment_service.reconcile_purchase_payments(SessionLocal(), env["org_id"])}
    assert findings[purchase["id"]]["stored_amount_paid"] == 10000.0
    assert findings[purchase["id"]]["canonical_amount_paid"] == 0.0
    assert findings[other_si]["canonical_amount_paid"] == 0.0
    db = SessionLocal()
    try:  # dry run wrote nothing, invented nothing
        assert db.get(PurchaseInvoice, purchase["id"]).amount_paid == 10000.0
        assert db.query(SupplierPayment).filter(SupplierPayment.organization_id == env["org_id"]).count() == 0
    finally:
        db.close()

    # The next real payment is what counts
    _pay(env, [(si, 4000.0)])
    _assert_in_sync(_state(env, purchase["id"], [si]), 4000.0, 6000.0, "partially_paid")

    # Apply reconciles the rest from the ledger, logs it, and still creates no payment
    db = SessionLocal()
    supplier_payment_service.reconcile_purchase_payments(db, env["org_id"], apply=True)
    db.close()
    _assert_in_sync(_state(env, other["id"], [other_si]), 0.0, 10000.0, "unpaid")
    db = SessionLocal()
    try:
        assert db.query(SupplierPayment).filter(SupplierPayment.organization_id == env["org_id"]).count() == 1
        logs = db.query(ActivityLog).filter(ActivityLog.organization_id == env["org_id"],
                                           ActivityLog.title == "Payment summary reconciled").all()
        assert any("3000" in (log.description or "") for log in logs)
    finally:
        db.close()
    assert supplier_payment_service.reconcile_purchase_payments(SessionLocal(), env["org_id"]) == []


def test_tenant_isolation():
    env_a, env_b = _org("Firm A"), _org("Firm B")
    purchase = _purchase(env_a)
    si = _auto_invoice_id(env_a, purchase["id"])
    assert client.patch(f"/purchases/{purchase['id']}/payment-status", json={"payment_status": "paid"},
                        headers=env_b["auth"]).status_code == 404
    assert client.get(f"/supplier-invoices/{si}", headers=env_b["auth"]).status_code == 404
    assert client.get(f"/purchases/{purchase['id']}", headers=env_b["auth"]).status_code == 404
    ap_b = client.get("/accounts-payable", headers=env_b["auth"]).json()["items"]
    assert all(item["supplier_invoice_id"] != si for item in ap_b)
    _assert_in_sync(_state(env_a, purchase["id"], [si]), 0.0, 10000.0, "unpaid")


def test_partial_supplier_payment_updates_purchase_paid_outstanding_and_status_together():
    """The runtime scenario: 10 x 1,000, GRNs 4 + 6, a 4,000 supplier payment."""
    env = _org()
    purchase = _purchase(env)
    si = _auto_invoice_id(env, purchase["id"])
    _receive(env, purchase, 4)
    _receive(env, purchase, 6)
    _pay(env, [(si, 4000.0)])

    state = _state(env, purchase["id"], [si])
    for view in (state["detail"], state["row"]):  # detail and list: the same three values
        assert (view["amount_paid"], view["outstanding_balance"], view["payment_status"]) == (4000.0, 6000.0, "partially_paid")
    _assert_in_sync(state, 4000.0, 6000.0, "partially_paid")

    # One recalculation sets all three together, whatever was stored before
    db = SessionLocal()
    try:
        stored = db.get(PurchaseInvoice, purchase["id"])
        stored.amount_paid, stored.payment_status = 0.0, "unpaid"
        db.commit()
        supplier_payment_service.recalculate_supplier_invoice(db, db.get(SupplierInvoice, si))
        db.commit()
        stored = db.get(PurchaseInvoice, purchase["id"])
        assert (stored.amount_paid, stored.outstanding_balance, stored.payment_status) == (4000.0, 6000.0, "partially_paid")
    finally:
        db.close()
    for paid, expected in ((0.0, "unpaid"), (4000.0, "partially_paid"), (9999.99, "paid"), (10000.0, "paid")):
        assert supplier_payment_service._payment_status(paid, 0.0, 10000.0) == expected


def test_supplier_invoice_items_carry_the_purchase_line_and_its_match_checks():
    env = _org()
    purchase = _purchase(env)
    si = _auto_invoice_id(env, purchase["id"])

    def invoice() -> dict:
        return client.get(f"/supplier-invoices/{si}", headers=env["auth"]).json()

    def listed() -> dict:
        rows = client.get("/supplier-invoices", params={"purchase_id": purchase["id"]}, headers=env["auth"]).json()["items"]
        return next(r for r in rows if r["id"] == si)

    db = SessionLocal()
    try:
        sku = db.get(Product, env["prod"]).sku
    finally:
        db.close()

    # Created on confirmation, before any goods: prices match, quantities not yet received
    inv = invoice()
    item = inv["items"][0]
    assert (item["product_name"], item["sku"], item["ordered_qty"], item["received_qty"]) == ("Widget", sku, 10, 0)
    assert (item["billed_qty"], item["unit_price"], item["purchase_price"]) == (10, 1000.0, 1000.0)
    assert (item["discount_amount"], item["tax_amount"], item["line_total"]) == (0.0, 0.0, 10000.0)
    assert (item["price_match"], item["quantity_match"]) == (True, False)
    assert (inv["price_match"], inv["quantity_match"], inv["verification_status"]) == (True, False, "pending")

    _receive(env, purchase, 4)
    assert (invoice()["items"][0]["received_qty"], invoice()["verification_status"]) == (4, "pending")
    _receive(env, purchase, 6)
    inv = invoice()
    item = inv["items"][0]
    assert (item["ordered_qty"], item["received_qty"], item["available_qty"]) == (10, 10, 10)
    assert (item["quantity_match"], item["price_match"]) == (True, True)
    assert (inv["quantity_match"], inv["price_match"], inv["verification_status"]) == (True, True, "matched")
    # The list returns the same, computed in one batch
    row = listed()
    assert {k: row["items"][0][k] for k in item} == item
    assert (row["quantity_match"], row["price_match"], row["verification_status"]) == (True, True, "matched")
    # Accounts Payable shows the same verification
    ap = client.get("/accounts-payable", params={"supplier_id": env["sup"]}, headers=env["auth"]).json()["items"]
    assert next(a for a in ap if a["supplier_invoice_id"] == si)["verification_status"] == "matched"


def test_price_difference_is_reported_as_the_failing_check():
    env = _org()
    purchase = _purchase(env, qty=2)
    auto = _auto_invoice_id(env, purchase["id"])
    assert client.post(f"/supplier-invoices/{auto}/cancel", headers=env["auth"]).status_code == 200
    _receive(env, purchase, 2)
    r = client.post("/supplier-invoices", json={"supplier_id": env["sup"], "purchase_id": purchase["id"],
                                                "supplier_invoice_number": f"PRICEY-{uuid.uuid4().hex[:4]}",
                                                "items": [{"purchase_item_id": purchase["items"][0]["id"],
                                                           "billed_qty": 2, "unit_price": 1100}]},
                    headers=env["auth"])
    assert r.status_code == 201, r.text
    assert r.json()["verification_status"] == "pending"  # drafts are not verified until recorded
    rec = client.post(f"/supplier-invoices/{r.json()['id']}/record", headers=env["auth"]).json()
    assert (rec["verification_status"], rec["price_match"], rec["quantity_match"]) == ("mismatched", False, True)
    assert (rec["items"][0]["unit_price"], rec["items"][0]["purchase_price"], rec["items"][0]["price_match"]) == (1100.0, 1000.0, False)


def test_voided_payment_stays_in_invoice_history_and_reports_voided():
    env = _org()
    purchase = _purchase(env)
    si = _auto_invoice_id(env, purchase["id"])
    p1 = _pay(env, [(si, 4000.0)])
    p2 = _pay(env, [(si, 6000.0)])
    assert _void(env, p2["id"]).status_code == 200

    history = client.get(f"/supplier-invoices/{si}/payments", headers=env["auth"]).json()
    rows = {r["supplier_payment_id"]: r for r in history}
    assert set(rows) == {p1["id"], p2["id"]}  # the voided payment is still listed, for audit
    assert (rows[p2["id"]]["status"], rows[p2["id"]]["payment_status"]) == ("voided", "voided")
    assert rows[p2["id"]]["voided_at"] is not None and rows[p2["id"]]["void_reason"] == "test"
    assert rows[p2["id"]]["payment_number"] == p2["payment_number"]
    assert (rows[p1["id"]]["status"], rows[p1["id"]]["payment_status"]) == ("recorded", "recorded")
    assert rows[p1["id"]]["voided_at"] is None
    # Same status as the Supplier Payments module reports
    assert client.get(f"/supplier-payments/{p2['id']}", headers=env["auth"]).json()["status"] == "voided"
    _assert_in_sync(_state(env, purchase["id"], [si]), 4000.0, 6000.0, "partially_paid")


def test_accounts_payable_summary_counts_every_open_invoice():
    env = _org()
    db = SessionLocal()
    try:
        second = Supplier(organization_id=env["org_id"], name=f"Second Supplier {uuid.uuid4().hex[:4]}", is_active=True)
        db.add(second)
        db.commit()
        second_id = second.id
    finally:
        db.close()

    no_due = _purchase(env)                                   # 10,000 open, no due date
    partial = _purchase(env)                                  # 10,000, 4,000 paid -> 6,000 open
    paid = _purchase(env)                                     # fully paid -> not open
    overdue = _purchase({**env, "sup": second_id}, qty=2,     # 2,000 open, second supplier, overdue
                        due_date="2026-01-01T00:00:00+00:00")
    si_no_due, si_partial, si_paid, si_overdue = (_auto_invoice_id(env, p["id"]) for p in (no_due, partial, paid, overdue))
    p_partial = _pay(env, [(si_partial, 4000.0)])
    _pay(env, [(si_paid, 10000.0)])

    def summary() -> dict:
        r = client.get("/accounts-payable/summary", headers=env["auth"])
        assert r.status_code == 200, r.text
        return r.json()

    s = summary()
    assert s["summary"]["total_payable"] == 18000.0           # 10,000 + 6,000 + 2,000
    assert s["summary"]["open_invoice_count"] == 3            # the fully paid invoice is excluded
    assert s["summary"]["supplier_count"] == 2
    assert s["summary"]["total_overdue"] == 2000.0            # only the invoice with a past due date
    assert round(sum(s["ageing"].values()), 2) == 2000.0      # the no-due-date invoice is in no bucket

    # The list endpoint carries the same summary, and its rows add up to it
    listing = client.get("/accounts-payable", headers=env["auth"]).json()
    assert listing["summary"] == s["summary"]
    rows = {i["supplier_invoice_id"]: i for i in listing["items"]}
    assert set(rows) == {si_no_due, si_partial, si_overdue}
    assert round(sum(i["outstanding_amount"] for i in rows.values()), 2) == 18000.0
    assert rows[si_partial]["outstanding_amount"] == 6000.0
    assert rows[si_no_due]["due_date"] is None and rows[si_no_due]["is_overdue"] is False
    assert rows[si_overdue]["is_overdue"] is True
    # ...and agree with the supplier invoices themselves
    invoices = [client.get(f"/supplier-invoices/{i}", headers=env["auth"]).json() for i in rows]
    assert round(sum(i["outstanding_amount"] for i in invoices), 2) == 18000.0

    # Voiding the 4,000 payment puts it back into the payable total
    assert _void(env, p_partial["id"]).status_code == 200
    s = summary()["summary"]
    assert (s["total_payable"], s["open_invoice_count"], s["supplier_count"]) == (22000.0, 3, 2)

    # Another firm sees none of it
    other = _org("AP Other")
    o = client.get("/accounts-payable/summary", headers=other["auth"]).json()["summary"]
    assert (o["total_payable"], o["open_invoice_count"], o["supplier_count"]) == (0.0, 0, 0)
