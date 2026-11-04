"""List endpoints load a page in a fixed number of queries, with unchanged responses.

- GET /deliveries: every item equals GET /deliveries/by-id/{id} (minus the detail-only
  timeline), and a page of 12 deliveries costs the same queries as a page of 3 (<= 10).
- GET /deliveries/assigned (the partner's open orders) works -- it used to fail with a
  500 -- and each item equals GET /orders/{id}.
- GET /orders and GET /customers stay flat as well.
"""

import uuid
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.orm import Session

from app.core.database import Base, engine
from app.core.security import create_access_token
from app.main import app
from app.models import (
    Customer, CustomerPayment, DeliveryCollection, Invoice, Organization, Plan, Product, Role, User, Vehicle,
    Warehouse,
)
from app.seed import seed_plans
from app.services import delivery_service, order_service, role_service, stock_service


@pytest.fixture
def db():
    Base.metadata.create_all(bind=engine)
    session = Session(bind=engine)
    seed_plans(session)
    session.commit()
    try:
        yield session
    finally:
        session.close()


def _client(user: User) -> TestClient:
    client = TestClient(app)
    client.headers = {"Authorization": "Bearer " + create_access_token(
        user.id, role=user.system_role, organization_id=user.organization_id)}
    return client


def _firm(db: Session, n: int):
    """A firm with `n` orders, each planned into a delivery for one partner; every other
    order is invoiced (some paid), some have a collection."""
    plan = db.query(Plan).filter(Plan.name == "Enterprise").first()
    org = Organization(id=str(uuid.uuid4()), name=f"Lists {uuid.uuid4().hex[:6]}",
                       company_code=f"CMP-{uuid.uuid4().int % 90000 + 10000}", plan_id=plan.id, status="active")
    db.add(org)
    db.commit()
    role_service.seed_default_roles(db, org.id)
    partner_role = db.query(Role).filter(Role.organization_id == org.id, Role.name == "Delivery Partner").first()
    admin = User(id=str(uuid.uuid4()), organization_id=org.id, email=f"l_{uuid.uuid4().hex[:10]}@x.com",
                 name="Admin", password_hash="h", role="admin", system_role="admin", is_active=True)
    partner = User(id=str(uuid.uuid4()), organization_id=org.id, email=f"d_{uuid.uuid4().hex[:10]}@x.com",
                   name="Driver", password_hash="h", role="delivery_partner", system_role="staff", is_active=True,
                   role_id=partner_role.id)
    wh = Warehouse(id=str(uuid.uuid4()), organization_id=org.id, name="Main", code=f"W{uuid.uuid4().hex[:5]}")
    vehicle = Vehicle(id=str(uuid.uuid4()), organization_id=org.id, vehicle_number=f"MH{uuid.uuid4().hex[:6]}",
                      vehicle_type="Van", default_driver_id=partner.id)
    products = [Product(id=str(uuid.uuid4()), organization_id=org.id, name=f"P{i}", price=50 + i, tax_rate=0,
                        inventory_tracking=True, weight=500, weight_unit="g") for i in range(3)]
    customers = [Customer(id=str(uuid.uuid4()), organization_id=org.id, name=f"Shop {i}",
                          phone=f"98{uuid.uuid4().int % 10**8:08d}", credit_limit=1_000_000) for i in range(3)]
    db.add_all([admin, partner, wh, vehicle, *products, *customers])
    db.commit()
    for p in products:
        stock_service.adjust_on_hand(db, org.id, wh.id, p.id, None, 900, "adjustment")
    db.commit()
    deliveries = []
    for i in range(n):
        order, _ = order_service.place_order(
            db, admin, customers[i % 3],
            [order_service.OrderLine(product_id=products[i % 3].id, quantity=2 + i % 4),
             order_service.OrderLine(product_id=products[(i + 1) % 3].id, quantity=1)],
            warehouse_id=wh.id)
        db.flush()
        delivery = delivery_service.plan(db, admin, order, delivery_partner=partner, vehicle_id=vehicle.id,
                                         warehouse_id=None, scheduled_date=None, delivery_address=None, notes=None,
                                         wanted=None)
        order.assigned_delivery_partner_id = partner.id
        order.fulfilment_status = "planned"
        db.flush()
        if i % 2 == 0:
            db.add(Invoice(id=str(uuid.uuid4()), organization_id=org.id, invoice_number=f"INV-{uuid.uuid4().hex[:6]}",
                           order_id=order.id, delivery_id=delivery.id if i % 4 == 0 else None,
                           customer_id=order.customer_id, subtotal=order.subtotal, tax=order.tax, total=order.total,
                           amount_paid=order.total / 2 if i % 4 == 0 else 0.0, status="partial",
                           invoice_date=datetime.now(timezone.utc)))
            customers[i % 3].outstanding_balance = (customers[i % 3].outstanding_balance or 0) + order.total
        if i % 3 == 0:
            db.add(DeliveryCollection(id=str(uuid.uuid4()), organization_id=org.id, delivery_id=delivery.id,
                                      sales_order_id=order.id, customer_id=order.customer_id,
                                      delivery_partner_id=partner.id, amount=5.0, payment_mode="cash",
                                      reconciliation_status="recorded", collected_at=datetime.now(timezone.utc)))
            db.add(CustomerPayment(id=str(uuid.uuid4()), organization_id=org.id, customer_id=order.customer_id,
                                   order_id=order.id, amount=3.0, payment_mode="upi",
                                   received_on=datetime.now(timezone.utc)))
        deliveries.append(delivery)
    db.commit()
    return admin, partner, [d.id for d in deliveries]


def _count(client: TestClient, path: str, params: dict) -> tuple[dict, int]:
    client.get(path, params=params)  # warm the per-request caches the way a second click would
    n = [0]

    def bump(*_a, **_k):
        n[0] += 1

    event.listen(engine, "before_cursor_execute", bump)
    try:
        res = client.get(path, params=params)
    finally:
        event.remove(engine, "before_cursor_execute", bump)
    assert res.status_code == 200, res.text
    return res.json(), n[0]


def test_delivery_list_matches_the_detail_and_costs_the_same_for_any_page(db):
    admin, partner, ids = _firm(db, 12)
    small_admin, _, _ = _firm(db, 3)
    client = _client(admin)
    page, n_large = _count(client, "/deliveries", {"page_size": 100})
    _, n_small = _count(_client(small_admin), "/deliveries", {"page_size": 100})
    assert page["total"] == 12
    assert n_large == n_small <= 10, (n_small, n_large)

    for item in page["items"]:
        detail = client.get(f"/deliveries/by-id/{item['id']}").json()
        detail.pop("timeline")
        item.pop("timeline")
        assert item == detail
    # The preloaded figures are real ones, not defaults.
    assert any(i["amount_due"] for i in page["items"]) and any(i["previous_pending_balance"] for i in page["items"])
    assert all(line["warehouse_available"] is not None for i in page["items"] for line in i["items"])

    # A delivery partner sees only their own, through the same loader.
    mine, n_partner = _count(_client(partner), "/deliveries", {"page_size": 100})
    assert mine["total"] == 12 and n_partner <= 10


def test_assigned_deliveries_list_works_and_matches_the_order(db):
    admin, partner, _ = _firm(db, 5)
    page, n = _count(_client(partner), "/deliveries/assigned", {"page_size": 100})
    assert page["total"] == 5 and n <= 20
    client = _client(admin)
    for item in page["items"]:
        assert item == client.get(f"/orders/{item['id']}").json()


def test_order_and_customer_lists_stay_flat(db):
    big_admin, _, _ = _firm(db, 12)
    small_admin, _, _ = _firm(db, 3)
    _, orders_big = _count(_client(big_admin), "/orders", {"page_size": 100})
    _, orders_small = _count(_client(small_admin), "/orders", {"page_size": 100})
    assert orders_big == orders_small <= 18, (orders_small, orders_big)
    _, cust_big = _count(_client(big_admin), "/customers", {"page_size": 100})
    _, cust_small = _count(_client(small_admin), "/customers", {"page_size": 100})
    assert cust_big == cust_small <= 7, (cust_small, cust_big)
