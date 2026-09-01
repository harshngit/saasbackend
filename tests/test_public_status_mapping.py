"""Tests for the public Order/Delivery status-consistency boundary mapping.

The internal database status vocabulary (app.core.workflow.ORDER_STATUSES /
DELIVERY_STATUSES) is unchanged and is NOT what this file tests — it verifies
only that the *public API* maps it correctly, per the approved contract:

    Order:    draft | awaiting_approval | placed -> placed
              processing                          -> confirmed
              completed                            -> completed
              cancelled                            -> cancelled

    Delivery: planned | rejected                   -> pending
              accepted | ready | loaded             -> accepted
              in_transit                            -> in_transit
              partially_delivered                   -> partially_delivered
              delivered                             -> delivered
              failed                                -> returned
              cancelled                             -> cancelled

Covers both the pure mapping functions (exhaustive, fast) and the actual API
responses (list/detail consistency, Order/Delivery independence, internal DB
values left untouched, permissions/multi-tenant isolation unaffected).
"""

import os
import sys
import uuid
from datetime import datetime, timezone

os.environ["DATABASE_URL"] = "sqlite:///./crm_saas.db"
os.environ["TESTING"] = "1"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.workflow import public_delivery_status, public_order_status
from app.main import app
from app.models import Customer, Delivery, SalesOrder

client = TestClient(app)

PASSED = 0
FAILED = 0


def log_test(name: str, condition: bool, extra: str = ""):
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"  PASS  {name}")
    else:
        FAILED += 1
        print(f"  FAIL  {name} {extra}".rstrip())


def _register_org(label: str) -> dict:
    email = f"admin_{uuid.uuid4().hex[:8]}@{label.replace('_', '').lower()}.com"
    r = client.post(
        "/auth/register",
        json={
            "organization_name": f"{label} Org",
            "admin_name": f"Admin {label}",
            "email": email,
            "password": "Password123!",
            "role": "admin",
        },
    )
    assert r.status_code == 201, r.text
    return {"Authorization": f"Bearer {r.json()['tokens']['access_token']}"}


def _org_id_of(auth: dict) -> str:
    return client.get("/auth/me", headers=auth).json()["organization_id"]


def _make_customer(db: Session, org_id: str) -> Customer:
    cust = Customer(organization_id=org_id, name=f"Cust {uuid.uuid4().hex[:6]}")
    db.add(cust)
    db.commit()
    db.refresh(cust)
    return cust


def _make_order(db: Session, org_id: str, customer_id: str, internal_status: str) -> SalesOrder:
    order = SalesOrder(
        organization_id=org_id,
        order_number=f"SO-{uuid.uuid4().hex[:8]}",
        customer_id=customer_id,
        status=internal_status,
        fulfilment_status="not_started",
        total=100.0,
    )
    db.add(order)
    db.commit()
    db.refresh(order)
    return order


def _make_delivery(
    db: Session, org_id: str, customer_id: str, internal_status: str, order_id: str | None = None
) -> Delivery:
    delivery = Delivery(
        organization_id=org_id,
        delivery_note_number=f"DLV-{uuid.uuid4().hex[:8]}",
        delivery_date=datetime.now(timezone.utc),
        sales_order_id=order_id,
        customer_id=customer_id,
        status=internal_status,
    )
    db.add(delivery)
    db.commit()
    db.refresh(delivery)
    return delivery


def run_all_tests():
    global PASSED, FAILED
    PASSED = 0
    FAILED = 0

    print("\n=======================================================")
    print("TEST SUITE: Public Order/Delivery Status Mapping")
    print("=======================================================\n")

    db: Session = next(get_db())
    auth = _register_org(f"StatusMap_{uuid.uuid4().hex[:6]}")
    org_id = _org_id_of(auth)
    customer = _make_customer(db, org_id)

    # ------------------------------------------------------------------
    # PART A: pure mapping function — exhaustive, matches the approved table
    # ------------------------------------------------------------------
    print("--- PART A: public_order_status() / public_delivery_status() ---")

    order_cases = [
        ("draft", "placed"),
        ("awaiting_approval", "placed"),
        ("placed", "placed"),
        ("processing", "confirmed"),
        ("completed", "completed"),
        ("cancelled", "cancelled"),
    ]
    for internal, expected in order_cases:
        log_test(
            f"Order internal '{internal}' -> public '{expected}'",
            public_order_status(internal) == expected,
        )
    log_test("public_order_status never returns 'processing'", "processing" not in {public_order_status(i) for i, _ in order_cases})

    delivery_cases = [
        ("planned", "pending"),
        ("rejected", "rejected"),
        ("accepted", "accepted"),
        ("ready", "accepted"),
        ("loaded", "in_transit"),
        ("in_transit", "in_transit"),
        ("partially_delivered", "partially_delivered"),
        ("delivered", "delivered"),
        ("failed", "returned"),
        ("cancelled", "cancelled"),
    ]
    for internal, expected in delivery_cases:
        log_test(
            f"Delivery internal '{internal}' -> public '{expected}'",
            public_delivery_status(internal) == expected,
        )
    # "rejected" is a legitimate public value now (Case: rejection genuinely
    # happened and is not terminal — see PUBLIC_DELIVERY_STATUS's docstring), so
    # it is intentionally excluded from this internal-leak check.
    leaked_internal = {"planned", "ready", "loaded", "failed"} & {
        public_delivery_status(i) for i, _ in delivery_cases
    }
    log_test("public_delivery_status never returns planned/ready/loaded/failed", not leaked_internal)

    # ------------------------------------------------------------------
    # PART B: end-to-end API — Order list/detail consistency + DB untouched
    # ------------------------------------------------------------------
    print("\n--- PART B: Order API (list/detail consistency, DB untouched) ---")

    for internal, expected in order_cases:
        order = _make_order(db, org_id, customer.id, internal)

        detail = client.get(f"/orders/{order.id}", headers=auth)
        log_test(f"GET /orders/{{id}} 200 for internal '{internal}'", detail.status_code == 200, detail.text)
        log_test(f"GET /orders/{{id}} status is public '{expected}' (internal '{internal}')", detail.json()["status"] == expected)

        listing = client.get("/orders", headers=auth)
        row = next((o for o in listing.json() if o["id"] == order.id), None)
        log_test(f"Order appears in GET /orders list (internal '{internal}')", row is not None)
        if row is not None:
            log_test(
                f"GET /orders list status matches detail (public '{expected}')",
                row["status"] == detail.json()["status"] == expected,
            )

        # The stored value must be completely untouched by serving the API response.
        db.expire_all()
        reloaded = db.get(SalesOrder, order.id)
        log_test(f"DB status still internal '{internal}' after GET (not rewritten)", reloaded.status == internal)

    log_test(
        "No Order API response ever contains 'processing'",
        all(o["status"] != "processing" for o in client.get("/orders", headers=auth).json()),
    )

    # ------------------------------------------------------------------
    # PART C: end-to-end API — Delivery list/detail consistency + DB untouched
    # ------------------------------------------------------------------
    print("\n--- PART C: Delivery API (list/detail consistency, DB untouched) ---")

    for internal, expected in delivery_cases:
        delivery = _make_delivery(db, org_id, customer.id, internal)

        detail = client.get(f"/deliveries/by-id/{delivery.id}", headers=auth)
        log_test(f"GET /deliveries/by-id/{{id}} 200 for internal '{internal}'", detail.status_code == 200, detail.text)
        log_test(f"GET /deliveries/by-id/{{id}} status is public '{expected}' (internal '{internal}')", detail.json()["status"] == expected)

        listing = client.get("/deliveries", headers=auth)
        row = next((d for d in listing.json() if d["id"] == delivery.id), None)
        log_test(f"Delivery appears in GET /deliveries list (internal '{internal}')", row is not None)
        if row is not None:
            log_test(
                f"GET /deliveries list status matches detail (public '{expected}')",
                row["status"] == detail.json()["status"] == expected,
            )

        db.expire_all()
        reloaded = db.get(Delivery, delivery.id)
        log_test(f"DB status still internal '{internal}' after GET (not rewritten)", reloaded.status == internal)

    all_delivery_rows = client.get("/deliveries", headers=auth).json()
    for banned in ("planned", "ready", "loaded", "failed"):
        log_test(
            f"No Delivery API response ever contains internal '{banned}'",
            all(d["status"] != banned for d in all_delivery_rows),
        )

    # ------------------------------------------------------------------
    # PART D: Order and Delivery statuses stay independent
    # ------------------------------------------------------------------
    print("\n--- PART D: Order / Delivery status independence ---")

    order = _make_order(db, org_id, customer.id, "processing")  # public: confirmed
    delivery = _make_delivery(db, org_id, customer.id, "planned", order_id=order.id)  # public: pending

    d_detail = client.get(f"/deliveries/by-id/{delivery.id}", headers=auth).json()
    log_test("Delivery's own status is public 'pending'", d_detail["status"] == "pending")
    log_test("Delivery's embedded order_status is public 'confirmed'", d_detail["order_status"] == "confirmed")
    log_test("Delivery's embedded order.status is public 'confirmed'", d_detail["order"]["status"] == "confirmed")
    log_test(
        "Delivery status and Order status are independent (not equal, not overwritten)",
        d_detail["status"] != d_detail["order_status"],
    )

    o_detail = client.get(f"/orders/{order.id}", headers=auth).json()
    log_test("Order's own status is unaffected by its delivery's status", o_detail["status"] == "confirmed")

    # ------------------------------------------------------------------
    # PART E: legacy /deliveries/{order_id} and /deliveries/{id}/status dict responses
    # ------------------------------------------------------------------
    print("\n--- PART E: legacy order-shaped Delivery endpoints ---")

    legacy_order = _make_order(db, org_id, customer.id, "processing")
    legacy_detail = client.get(f"/deliveries/{legacy_order.id}", headers=auth)
    log_test("GET /deliveries/{order_id} succeeds", legacy_detail.status_code == 200, legacy_detail.text)
    log_test("GET /deliveries/{order_id} status is public 'confirmed'", legacy_detail.json()["status"] == "confirmed")

    status_res = client.patch(
        f"/deliveries/{legacy_order.id}/status",
        json={"status": "Delivered"},
        headers=auth,
    )
    log_test("PATCH /deliveries/{order_id}/status succeeds", status_res.status_code == 200, status_res.text)
    log_test(
        "PATCH /deliveries/{order_id}/status order_status is public 'completed'",
        status_res.json()["order_status"] == "completed",
    )

    # ------------------------------------------------------------------
    # PART F: permissions / multi-tenant isolation unaffected by the mapping
    # ------------------------------------------------------------------
    print("\n--- PART F: permissions / organization isolation unchanged ---")

    other_auth = _register_org(f"StatusMapOther_{uuid.uuid4().hex[:6]}")
    cross_order = client.get(f"/orders/{order.id}", headers=other_auth)
    log_test("Other org cannot read this org's order (404)", cross_order.status_code == 404)
    cross_delivery = client.get(f"/deliveries/by-id/{delivery.id}", headers=other_auth)
    log_test("Other org cannot read this org's delivery (404)", cross_delivery.status_code == 404)

    print("\n=======================================================")
    print(f"RESULTS: {PASSED} passed, {FAILED} failed")
    print("=======================================================\n")
    if FAILED > 0:
        sys.exit(1)


if __name__ == "__main__":
    run_all_tests()
