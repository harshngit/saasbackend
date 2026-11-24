"""Receipt numbers after reconcile -> void, vehicle load-batch errors on a closed day, and the
Day-End approve / reject 403 wording. Two firms.

1. Reconcile gives the collection a receipt (CustomerPayment). Void deletes that payment, so the
   receipt number leaves every list and is never handed out again: a gap. This test pins the
   current behaviour (numbering is not changed in this round) and that the reversal journal
   entry still records the voided receipt number -- the trail the read-only SQL uses.
2. POST /deliveries/load-batch: a structured error (DAY_END_CLOSED) comes back per item as
   `code` + `message`, with `detail` the readable message (not a stringified dict); the old
   fields (delivery_id, ok, detail, delivery) are unchanged.
3. Day-End approve / reject without Deliveries > Approve: a Day-End message, not the
   collections one.
"""

import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import test_dayend_lock_reject_pro_limit as h  # noqa: E402  (shared firm / delivery helpers)
from test_dayend_lock_reject_pro_limit import _schema  # noqa: E402,F401  (module schema fixture)

from app.core.database import SessionLocal  # noqa: E402
from app.models import CustomerPayment, DeliveryCollection  # noqa: E402
from app.models.accounting import JournalEntry  # noqa: E402
from app.routers.deliveries import COLLECTIONS_APPROVE_DETAIL  # noqa: E402
from app.routers.delivery_day_end import DAY_END_APPROVE_DETAIL  # noqa: E402

c = h.c


# ------------------------------------------------------- 1. reconcile -> void -> receipt number
def _collection(org_id, customer_id, partner_id, amount):
    with SessionLocal() as s:
        coll = DeliveryCollection(organization_id=org_id, customer_id=customer_id, delivery_partner_id=partner_id,
                                  amount=amount, payment_mode="cash", reconciliation_status="recorded")
        s.add(coll)
        s.commit()
        return coll.id


def _receipt_of(collection_id):
    with SessionLocal() as s:
        pid = s.get(DeliveryCollection, collection_id).customer_payment_id
        return s.get(CustomerPayment, pid).receipt_number


def _listed_receipts(d):
    body = c.get(f"/customers/{d['cust']}/payments/collected", headers=d["admin"], params={"page_size": 100}).json()
    return {r["receipt_number"] for r in body["items"]}


def test_reconcile_then_void_leaves_a_receipt_gap_but_the_journal_keeps_the_number():
    d = h._new_day()
    first = _collection(d["org"], d["cust"], d["partner"], 100.0)
    second = _collection(d["org"], d["cust"], d["partner"], 50.0)
    assert c.post(f"/deliveries/collections/{first}/reconcile", headers=d["admin"]).status_code == 200
    assert c.post(f"/deliveries/collections/{second}/reconcile", headers=d["admin"]).status_code == 200
    n1, n2 = _receipt_of(first), _receipt_of(second)
    seq = lambda n: int(n.rsplit("-", 1)[1])  # noqa: E731
    assert seq(n2) == seq(n1) + 1
    assert {n1, n2} <= _listed_receipts(d)

    r = c.post(f"/deliveries/collections/{first}/void", headers=d["admin"])
    assert r.status_code == 200 and r.json()["reconciliation_status"] == "voided"
    listed = _listed_receipts(d)
    assert n1 not in listed and n2 in listed                    # gone from the receipt list
    with SessionLocal() as s:
        assert s.query(CustomerPayment).filter(CustomerPayment.receipt_number == n1).count() == 0   # row deleted
        coll = s.get(DeliveryCollection, first)
        assert coll.customer_payment_id is None and coll.reconciled_at is not None
        # The reversal journal entry is the only place the voided number survives.
        trail = s.query(JournalEntry).filter(JournalEntry.organization_id == d["org"],
                                             JournalEntry.source_type == "journal_entry_reversal",
                                             JournalEntry.description.like(f"%Voided payment {n1}%")).count()
        assert trail == 1

    third = _collection(d["org"], d["cust"], d["partner"], 25.0)
    assert c.post(f"/deliveries/collections/{third}/reconcile", headers=d["admin"]).status_code == 200
    assert seq(_receipt_of(third)) == seq(n2) + 1               # n1 is never reused: the gap stays

    # Another firm's numbering is independent and its list does not see this firm's receipts.
    other = h._new_day()
    o = _collection(other["org"], other["cust"], other["partner"], 10.0)
    assert c.post(f"/deliveries/collections/{o}/reconcile", headers=other["admin"]).status_code == 200
    assert c.post(f"/deliveries/collections/{first}/void", headers=other["admin"]).status_code == 404
    assert n2 not in _listed_receipts(other)


# --------------------------------------------------- 2. load-batch on a closed day: code + message
OLD_FIELDS = {"delivery_id", "ok", "detail", "delivery"}


def test_load_batch_returns_structured_code_and_message():
    d = h._new_day()
    ok_id, _ = h._ready(d)
    r = c.post("/deliveries/load-batch", json={"delivery_ids": [ok_id]}, headers=d["admin"])
    assert r.status_code == 200, r.text
    item = r.json()["results"][0]
    assert OLD_FIELDS <= set(item)
    assert item["ok"] is True and item["detail"] == "Loaded successfully" and item["delivery"] is not None
    assert item["code"] is None and item["message"] == "Loaded successfully"

    h._approved_day_end(d, d["partner"])                         # today's Day-End approved -> closed
    closed_id, _ = h._ready(d)
    other = h._new_day()
    foreign_id, _ = h._ready(other)                              # another firm's delivery
    r = c.post("/deliveries/load-batch", json={"delivery_ids": [closed_id, ok_id, foreign_id]}, headers=d["admin"])
    assert r.status_code == 200, r.text
    closed, again, foreign = r.json()["results"]

    assert OLD_FIELDS <= set(closed)
    assert closed["ok"] is False and closed["delivery"] is None
    assert closed["code"] == "DAY_END_CLOSED"
    assert closed["message"].startswith("The delivery partner's Day-End for ") and "approved" in closed["message"]
    assert closed["detail"] == closed["message"] and not closed["detail"].startswith("{")

    assert again["ok"] is True and again["detail"] == "Delivery is already loaded" and again["code"] is None
    assert foreign["ok"] is False and foreign["code"] is None and foreign["delivery"] is None
    assert foreign["detail"] == foreign["message"] and "not found" in foreign["detail"].lower()

    with SessionLocal() as s:                                    # nothing was loaded for the closed day
        from app.models import Delivery
        assert s.get(Delivery, closed_id).status == "ready"


# ------------------------------------------------------- 3. Day-End approve / reject 403 wording
def test_day_end_403_has_day_end_wording(tmp_path):
    org_id, admin = h._org()
    partner, _ = h._staff(admin)
    view_only = h._role_user(org_id, {"deliveries": {"view": True}})
    from datetime import timedelta
    with SessionLocal() as s:
        de = h.DeliveryDayEnd(organization_id=org_id, delivery_partner_id=partner, status="submitted",
                              business_date=h.day_end_service.today(s, org_id) - timedelta(days=1))
        s.add(de)
        s.commit()
        de_id = de.id
    for action, body in (("approve", {}), ("reject", {"reason": "x"})):
        r = c.post(f"/delivery-day-ends/{de_id}/{action}", json=body, headers=view_only)
        assert r.status_code == 403
        assert r.json()["detail"] == DAY_END_APPROVE_DETAIL
        assert "collection" not in r.json()["detail"].lower()
    # Collections keep their own wording.
    coll = _collection(org_id, None, partner, 5.0)
    r = c.post(f"/deliveries/collections/{coll}/reconcile", headers=view_only)
    assert r.status_code == 403 and r.json()["detail"] == COLLECTIONS_APPROVE_DETAIL
    assert c.post(f"/delivery-day-ends/{de_id}/approve", json={}, headers=admin).status_code == 200
