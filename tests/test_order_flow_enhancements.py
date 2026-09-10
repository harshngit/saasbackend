"""Integration test suite for Order Flow Enhancement (Phase 2).

Tests cover Scenarios 1-20:
 1. Takeaway Full Payment
 2. Takeaway Partial Payment
 3. Takeaway Pending Payment
 4. Home Delivery with Partner & Vehicle
 5. Home Delivery without Partner (planned state)
 6. Home Delivery Address requirement validation (HTTP 400)
 7. Takeaway Address non-requirement
 8. Takeaway does NOT create Delivery record
 9. Partial Payment calculation
 10. Full Payment calculation
 11. Invalid Overpayment rejection (HTTP 400)
 12. Negative Payment rejection (HTTP 400)
 13. Invalid Payment Status combination (HTTP 400)
 14. Delivery Partner validation (HTTP 400)
 15. Vehicle validation (HTTP 400)
 16. Backward compatibility (fulfilment_method / payment_type)
 17. Order Detail API response structure
 18. Order List API response structure
 19. Conflicting Delivery fields rejection (HTTP 400)
 20. Conflicting Payment fields rejection (HTTP 400)
"""

import os
import sys
import uuid
from datetime import datetime, timezone

os.environ["DATABASE_URL"] = "sqlite:///./crm_saas.db"
os.environ["TESTING"] = "1"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from fastapi.testclient import TestClient

from app.core.database import get_db
from app.main import app
from app.models import (
    Customer,
    Delivery,
    Invoice,
    Product,
    Role,
    SalesOrder,
    User,
    UserRole,
    Vehicle,
    Warehouse,
    WarehouseStock,
)

client = TestClient(app)


def _register_org(label: str) -> tuple[dict, str]:
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
    auth = {"Authorization": f"Bearer {r.json()['tokens']['access_token']}"}
    return auth, r.json()["user"]["organization_id"]


def _setup_entities(auth: dict) -> tuple[str, str, str]:
    """Create customer, warehouse, product with stock, return (cust_id, wh_id, prod_id)."""
    # Create customer
    c_res = client.post(
        "/customers",
        json={
            "name": f"Customer {uuid.uuid4().hex[:6]}",
            "phone": "9876543210",
        },
        headers=auth,
    )
    assert c_res.status_code == 201, c_res.text
    cust_id = c_res.json()["id"]
    org_id = c_res.json()["organization_id"]

    # Get warehouse via API or create one
    wh_res = client.get("/warehouses", headers=auth)
    if wh_res.status_code == 200 and len(wh_res.json()) > 0:
        wh_id = wh_res.json()[0]["id"]
    else:
        w_create = client.post(
            "/warehouses",
            json={"name": "Main Warehouse", "code": f"WH-{uuid.uuid4().hex[:4]}"},
            headers=auth,
        )
        assert w_create.status_code == 201, w_create.text
        wh_id = w_create.json()["id"]

    # Create product
    p_res = client.post(
        "/products",
        json={
            "name": f"Widget {uuid.uuid4().hex[:6]}",
            "sku": f"SKU-{uuid.uuid4().hex[:6]}",
            "selling_price": 500.0,
            "cost_price": 200.0,
        },
        headers=auth,
    )
    assert p_res.status_code == 201, p_res.text
    prod_id = p_res.json()["id"]

    # Add stock to warehouse
    db = next(get_db())
    stock = db.query(WarehouseStock).filter(
        WarehouseStock.warehouse_id == wh_id,
        WarehouseStock.product_id == prod_id,
    ).first()
    if stock:
        stock.on_hand_quantity = 500.0
    else:
        stock = WarehouseStock(
            organization_id=org_id,
            warehouse_id=wh_id,
            product_id=prod_id,
            on_hand_quantity=500.0,
        )
        db.add(stock)
    db.commit()
    db.close()
    return cust_id, wh_id, prod_id


def test_01_takeaway_full_payment():
    auth, org_id = _register_org("TakeawayFull")
    cust_id, wh_id, prod_id = _setup_entities(auth)

    r = client.post(
        "/orders",
        json={
            "customer_id": cust_id,
            "warehouse_id": wh_id,
            "delivery_method": "takeaway",
            "payment_status": "paid",
            "paid_amount": 1000.0,
            "payment_method": "upi",
            "items": [{"product_id": prod_id, "quantity": 2, "unit_price": 500.0}],
        },
        headers=auth,
    )
    assert r.status_code == 201, r.text
    data = r.json()

    assert data["delivery_method"] == "takeaway"
    assert data["payment_status"] == "paid"
    assert data["paid_amount"] == 1000.0
    assert data["remaining_amount"] == 0.0
    assert data["delivery_partner"] is None
    assert data["status"] == "completed"

    # DB verify no Delivery created
    db = next(get_db())
    del_count = db.query(Delivery).filter(Delivery.sales_order_id == data["id"]).count()
    assert del_count == 0

    # Invoice exists and is paid
    inv = db.query(Invoice).filter(Invoice.order_id == data["id"]).first()
    assert inv is not None
    assert inv.amount_paid == 1000.0
    assert inv.status == "paid"


def test_02_takeaway_partial_payment():
    auth, org_id = _register_org("TakeawayPartial")
    cust_id, wh_id, prod_id = _setup_entities(auth)

    r = client.post(
        "/orders",
        json={
            "customer_id": cust_id,
            "warehouse_id": wh_id,
            "delivery_method": "takeaway",
            "payment_status": "partial",
            "paid_amount": 700.0,
            "payment_method": "upi",
            "items": [{"product_id": prod_id, "quantity": 2, "unit_price": 500.0}],
        },
        headers=auth,
    )
    assert r.status_code == 201, r.text
    data = r.json()

    assert data["delivery_method"] == "takeaway"
    assert data["payment_status"] == "partial"
    assert data["paid_amount"] == 700.0
    assert data["remaining_amount"] == 300.0
    assert data["status"] == "completed"

    # DB verify no Delivery created
    db = next(get_db())
    del_count = db.query(Delivery).filter(Delivery.sales_order_id == data["id"]).count()
    assert del_count == 0

    # Customer outstanding balance increased by remaining 300.0
    cust = db.get(Customer, cust_id)
    assert cust.outstanding_balance == 300.0


def test_03_takeaway_pending_payment():
    auth, org_id = _register_org("TakeawayPending")
    cust_id, wh_id, prod_id = _setup_entities(auth)

    r = client.post(
        "/orders",
        json={
            "customer_id": cust_id,
            "warehouse_id": wh_id,
            "delivery_method": "takeaway",
            "payment_status": "pending",
            "paid_amount": 0.0,
            "items": [{"product_id": prod_id, "quantity": 2, "unit_price": 500.0}],
        },
        headers=auth,
    )
    assert r.status_code == 201, r.text
    data = r.json()

    assert data["delivery_method"] == "takeaway"
    assert data["payment_status"] == "pending"
    assert data["paid_amount"] == 0.0
    assert data["remaining_amount"] == 1000.0
    assert data["status"] == "completed"

    db = next(get_db())
    cust = db.get(Customer, cust_id)
    assert cust.outstanding_balance == 1000.0


def test_04_home_delivery_with_partner_and_vehicle():
    auth, org_id = _register_org("HomeDeliveryPartner")
    cust_id, wh_id, prod_id = _setup_entities(auth)

    db = next(get_db())
    partner = User(
        id=str(uuid.uuid4()),
        organization_id=org_id,
        name="Delivery Partner Bob",
        email=f"bob_{uuid.uuid4().hex[:6]}@partner.com",
        password_hash="dummy",
        role=UserRole.DELIVERY_PARTNER,
        is_active=True,
    )
    db.add(partner)

    # Create vehicle
    veh = Vehicle(
        id=str(uuid.uuid4()),
        organization_id=org_id,
        vehicle_number=f"MH-12-{uuid.uuid4().hex[:4].upper()}",
        vehicle_type="Truck",
        status="active",
    )
    db.add(veh)
    db.commit()

    r = client.post(
        "/orders",
        json={
            "customer_id": cust_id,
            "warehouse_id": wh_id,
            "delivery_method": "home_delivery",
            "delivery_address": "123 Green Street, Sector 5",
            "payment_status": "paid",
            "paid_amount": 1000.0,
            "payment_method": "cash",
            "delivery_partner_id": partner.id,
            "vehicle_id": veh.id,
            "items": [{"product_id": prod_id, "quantity": 2, "unit_price": 500.0}],
        },
        headers=auth,
    )
    assert r.status_code == 201, r.text
    data = r.json()

    assert data["delivery_method"] == "home_delivery"
    assert data["delivery_partner"] is not None
    assert data["delivery_partner"]["id"] == partner.id

    # Verify Delivery record created in planned state
    d_row = db.query(Delivery).filter(Delivery.sales_order_id == data["id"]).first()
    assert d_row is not None
    assert d_row.status == "planned"
    assert d_row.delivery_partner_id == partner.id
    assert d_row.vehicle_id == veh.id


def test_05_home_delivery_without_partner():
    auth, org_id = _register_org("HomeDeliveryUnassigned")
    cust_id, wh_id, prod_id = _setup_entities(auth)

    r = client.post(
        "/orders",
        json={
            "customer_id": cust_id,
            "warehouse_id": wh_id,
            "delivery_method": "home_delivery",
            "delivery_address": "456 Blue Avenue",
            "payment_status": "pending",
            "items": [{"product_id": prod_id, "quantity": 2, "unit_price": 500.0}],
        },
        headers=auth,
    )
    assert r.status_code == 201, r.text
    data = r.json()

    assert data["delivery_method"] == "home_delivery"
    assert data["delivery_partner"] is None

    db = next(get_db())
    d_row = db.query(Delivery).filter(Delivery.sales_order_id == data["id"]).first()
    assert d_row is not None
    assert d_row.status == "planned"
    assert d_row.delivery_partner_id is None


def test_06_home_delivery_requires_address():
    auth, org_id = _register_org("HomeDeliveryNoAddress")
    cust_id, wh_id, prod_id = _setup_entities(auth)

    r = client.post(
        "/orders",
        json={
            "customer_id": cust_id,
            "warehouse_id": wh_id,
            "delivery_method": "home_delivery",
            "items": [{"product_id": prod_id, "quantity": 1, "unit_price": 500.0}],
        },
        headers=auth,
    )
    assert r.status_code == 400, r.text
    assert "delivery_address is required" in r.text


def test_07_takeaway_does_not_require_address():
    auth, org_id = _register_org("TakeawayNoAddress")
    cust_id, wh_id, prod_id = _setup_entities(auth)

    r = client.post(
        "/orders",
        json={
            "customer_id": cust_id,
            "warehouse_id": wh_id,
            "delivery_method": "takeaway",
            "items": [{"product_id": prod_id, "quantity": 1, "unit_price": 500.0}],
        },
        headers=auth,
    )
    assert r.status_code == 201, r.text


def test_08_takeaway_does_not_create_delivery():
    auth, org_id = _register_org("TakeawayNoDelivery")
    cust_id, wh_id, prod_id = _setup_entities(auth)

    r = client.post(
        "/orders",
        json={
            "customer_id": cust_id,
            "warehouse_id": wh_id,
            "delivery_method": "takeaway",
            "items": [{"product_id": prod_id, "quantity": 1, "unit_price": 500.0}],
        },
        headers=auth,
    )
    assert r.status_code == 201, r.text

    db = next(get_db())
    del_count = db.query(Delivery).filter(Delivery.sales_order_id == r.json()["id"]).count()
    assert del_count == 0


def test_09_partial_payment_calculation():
    auth, org_id = _register_org("PartialCalc")
    cust_id, wh_id, prod_id = _setup_entities(auth)

    r = client.post(
        "/orders",
        json={
            "customer_id": cust_id,
            "warehouse_id": wh_id,
            "delivery_method": "takeaway",
            "payment_status": "partial",
            "paid_amount": 700.0,
            "items": [{"product_id": prod_id, "quantity": 2, "unit_price": 500.0}],
        },
        headers=auth,
    )
    assert r.status_code == 201, r.text
    data = r.json()
    assert data["paid_amount"] == 700.0
    assert data["remaining_amount"] == 300.0
    assert data["payment_status"] == "partial"


def test_10_full_payment_calculation():
    auth, org_id = _register_org("FullCalc")
    cust_id, wh_id, prod_id = _setup_entities(auth)

    r = client.post(
        "/orders",
        json={
            "customer_id": cust_id,
            "warehouse_id": wh_id,
            "delivery_method": "takeaway",
            "payment_status": "paid",
            "paid_amount": 1000.0,
            "items": [{"product_id": prod_id, "quantity": 2, "unit_price": 500.0}],
        },
        headers=auth,
    )
    assert r.status_code == 201, r.text
    data = r.json()
    assert data["paid_amount"] == 1000.0
    assert data["remaining_amount"] == 0.0
    assert data["payment_status"] == "paid"


def test_11_invalid_overpayment():
    auth, org_id = _register_org("Overpayment")
    cust_id, wh_id, prod_id = _setup_entities(auth)

    r = client.post(
        "/orders",
        json={
            "customer_id": cust_id,
            "warehouse_id": wh_id,
            "delivery_method": "takeaway",
            "payment_status": "paid",
            "paid_amount": 1500.0,
            "items": [{"product_id": prod_id, "quantity": 2, "unit_price": 500.0}],
        },
        headers=auth,
    )
    assert r.status_code == 400, r.text
    assert "cannot exceed order total" in r.text


def test_12_negative_payment():
    auth, org_id = _register_org("NegativePayment")
    cust_id, wh_id, prod_id = _setup_entities(auth)

    r = client.post(
        "/orders",
        json={
            "customer_id": cust_id,
            "warehouse_id": wh_id,
            "delivery_method": "takeaway",
            "payment_status": "paid",
            "paid_amount": -100.0,
            "items": [{"product_id": prod_id, "quantity": 2, "unit_price": 500.0}],
        },
        headers=auth,
    )
    assert r.status_code == 422, r.text


def test_13_invalid_payment_status_combination():
    auth, org_id = _register_org("MismatchedStatus")
    cust_id, wh_id, prod_id = _setup_entities(auth)

    # Paid status with partial amount
    r = client.post(
        "/orders",
        json={
            "customer_id": cust_id,
            "warehouse_id": wh_id,
            "delivery_method": "takeaway",
            "payment_status": "paid",
            "paid_amount": 500.0,
            "items": [{"product_id": prod_id, "quantity": 2, "unit_price": 500.0}],
        },
        headers=auth,
    )
    assert r.status_code == 400, r.text
    assert "must equal order total" in r.text


def test_14_delivery_partner_validation():
    auth, org_id = _register_org("InvalidPartner")
    cust_id, wh_id, prod_id = _setup_entities(auth)

    # Non-existent / non-partner user
    r = client.post(
        "/orders",
        json={
            "customer_id": cust_id,
            "warehouse_id": wh_id,
            "delivery_method": "home_delivery",
            "delivery_address": "789 Park Rd",
            "delivery_partner_id": str(uuid.uuid4()),
            "items": [{"product_id": prod_id, "quantity": 1, "unit_price": 500.0}],
        },
        headers=auth,
    )
    assert r.status_code == 400, r.text


def test_15_vehicle_validation():
    auth, org_id = _register_org("InvalidVehicle")
    cust_id, wh_id, prod_id = _setup_entities(auth)

    r = client.post(
        "/orders",
        json={
            "customer_id": cust_id,
            "warehouse_id": wh_id,
            "delivery_method": "home_delivery",
            "delivery_address": "789 Park Rd",
            "vehicle_id": str(uuid.uuid4()),
            "items": [{"product_id": prod_id, "quantity": 1, "unit_price": 500.0}],
        },
        headers=auth,
    )
    assert r.status_code == 400, r.text


def test_16_backward_compatibility():
    auth, org_id = _register_org("BackwardCompat")
    cust_id, wh_id, prod_id = _setup_entities(auth)

    r = client.post(
        "/orders",
        json={
            "customer_id": cust_id,
            "warehouse_id": wh_id,
            "fulfilment_method": "pickup",
            "payment_type": "cash",
            "items": [{"product_id": prod_id, "quantity": 1, "unit_price": 500.0}],
        },
        headers=auth,
    )
    assert r.status_code == 201, r.text
    data = r.json()
    assert data["delivery_method"] == "takeaway"
    assert data["fulfilment_method"] == "pickup"
    assert data["payment_type"] == "cash"


def test_17_order_detail_response():
    auth, org_id = _register_org("OrderDetail")
    cust_id, wh_id, prod_id = _setup_entities(auth)

    create_res = client.post(
        "/orders",
        json={
            "customer_id": cust_id,
            "warehouse_id": wh_id,
            "delivery_method": "takeaway",
            "payment_status": "partial",
            "paid_amount": 300.0,
            "items": [{"product_id": prod_id, "quantity": 1, "unit_price": 500.0}],
        },
        headers=auth,
    )
    assert create_res.status_code == 201, create_res.text
    order_id = create_res.json()["id"]

    r = client.get(f"/orders/{order_id}", headers=auth)
    assert r.status_code == 200, r.text
    data = r.json()

    assert data["delivery_method"] == "takeaway"
    assert data["payment_status"] == "partial"
    assert data["paid_amount"] == 300.0
    assert data["remaining_amount"] == 200.0
    assert "delivery_partner" in data


def test_18_order_list_response():
    auth, org_id = _register_org("OrderList")
    cust_id, wh_id, prod_id = _setup_entities(auth)

    client.post(
        "/orders",
        json={
            "customer_id": cust_id,
            "warehouse_id": wh_id,
            "delivery_method": "takeaway",
            "items": [{"product_id": prod_id, "quantity": 1, "unit_price": 500.0}],
        },
        headers=auth,
    )

    r = client.get("/orders", headers=auth)
    assert r.status_code == 200, r.text
    orders = r.json()
    assert len(orders) >= 1
    for o in orders:
        assert "delivery_method" in o
        assert "payment_status" in o
        assert "remaining_amount" in o


def test_19_conflicting_delivery_fields():
    auth, org_id = _register_org("ConflictDelivery")
    cust_id, wh_id, prod_id = _setup_entities(auth)

    r = client.post(
        "/orders",
        json={
            "customer_id": cust_id,
            "warehouse_id": wh_id,
            "delivery_method": "takeaway",
            "fulfilment_method": "delivery",
            "items": [{"product_id": prod_id, "quantity": 1, "unit_price": 500.0}],
        },
        headers=auth,
    )
    assert r.status_code == 422, r.text


def test_20_conflicting_payment_fields():
    auth, org_id = _register_org("ConflictPayment")
    cust_id, wh_id, prod_id = _setup_entities(auth)

    r = client.post(
        "/orders",
        json={
            "customer_id": cust_id,
            "warehouse_id": wh_id,
            "payment_method": "upi",
            "payment_type": "cash",
            "items": [{"product_id": prod_id, "quantity": 1, "unit_price": 500.0}],
        },
        headers=auth,
    )
    assert r.status_code == 422, r.text


def test_21_order_subsequent_payment_full():
    auth, org_id = _register_org("OrderPayFull")
    cust_id, wh_id, prod_id = _setup_entities(auth)

    # Order total = 1000, paid upfront = 700
    create_res = client.post(
        "/orders",
        json={
            "customer_id": cust_id,
            "warehouse_id": wh_id,
            "delivery_method": "takeaway",
            "payment_status": "partial",
            "paid_amount": 700.0,
            "items": [{"product_id": prod_id, "quantity": 2, "unit_price": 500.0}],
        },
        headers=auth,
    )
    assert create_res.status_code == 201, create_res.text
    order_id = create_res.json()["id"]

    # Pay remaining 300
    r = client.post(
        f"/orders/{order_id}/payments",
        json={
            "amount": 300.0,
            "payment_method": "cash",
            "reference": "REF123",
            "notes": "Full payment settled",
            "payment_date": "2026-09-10",
        },
        headers=auth,
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["order_id"] == order_id
    assert data["order_amount"] == 1000.0
    assert data["paid_amount"] == 1000.0
    assert data["remaining_amount"] == 0.0
    assert data["payment_status"] == "paid"


def test_22_order_subsequent_payment_partial():
    auth, org_id = _register_org("OrderPayPart")
    cust_id, wh_id, prod_id = _setup_entities(auth)

    # Order total = 1000, paid upfront = 500
    create_res = client.post(
        "/orders",
        json={
            "customer_id": cust_id,
            "warehouse_id": wh_id,
            "delivery_method": "takeaway",
            "payment_status": "partial",
            "paid_amount": 500.0,
            "items": [{"product_id": prod_id, "quantity": 2, "unit_price": 500.0}],
        },
        headers=auth,
    )
    assert create_res.status_code == 201, create_res.text
    order_id = create_res.json()["id"]

    # Pay additional 200
    r = client.post(
        f"/orders/{order_id}/payments",
        json={
            "amount": 200.0,
            "payment_method": "upi",
        },
        headers=auth,
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["paid_amount"] == 700.0
    assert data["remaining_amount"] == 300.0
    assert data["payment_status"] == "partial"


def test_23_order_payment_overpayment():
    auth, org_id = _register_org("OrderPayOver")
    cust_id, wh_id, prod_id = _setup_entities(auth)

    # Order total = 1000, paid upfront = 700 (remaining 300)
    create_res = client.post(
        "/orders",
        json={
            "customer_id": cust_id,
            "warehouse_id": wh_id,
            "delivery_method": "takeaway",
            "payment_status": "partial",
            "paid_amount": 700.0,
            "items": [{"product_id": prod_id, "quantity": 2, "unit_price": 500.0}],
        },
        headers=auth,
    )
    assert create_res.status_code == 201, create_res.text
    order_id = create_res.json()["id"]

    # Pay 500 (exceeds 300) -> 400
    r = client.post(
        f"/orders/{order_id}/payments",
        json={
            "amount": 500.0,
            "payment_method": "cash",
        },
        headers=auth,
    )
    assert r.status_code == 400, r.text
    assert "Amount exceeds outstanding balance" in r.text


def test_24_order_payment_zero_and_negative():
    auth, org_id = _register_org("OrderPayZero")
    cust_id, wh_id, prod_id = _setup_entities(auth)

    create_res = client.post(
        "/orders",
        json={
            "customer_id": cust_id,
            "warehouse_id": wh_id,
            "delivery_method": "takeaway",
            "items": [{"product_id": prod_id, "quantity": 1, "unit_price": 500.0}],
        },
        headers=auth,
    )
    assert create_res.status_code == 201, create_res.text
    order_id = create_res.json()["id"]

    # amount = 0
    r = client.post(
        f"/orders/{order_id}/payments",
        json={"amount": 0.0, "payment_method": "cash"},
        headers=auth,
    )
    assert r.status_code == 422, r.text

    # amount = -100
    r = client.post(
        f"/orders/{order_id}/payments",
        json={"amount": -100.0, "payment_method": "cash"},
        headers=auth,
    )
    assert r.status_code == 422, r.text


def test_25_delivery_collection_valid_and_overpayment():
    auth, org_id = _register_org("DelivCollValidation")
    cust_id, wh_id, prod_id = _setup_entities(auth)

    # Home delivery order total = 1000, paid upfront = 300 (remaining 700)
    create_res = client.post(
        "/orders",
        json={
            "customer_id": cust_id,
            "warehouse_id": wh_id,
            "delivery_method": "home_delivery",
            "delivery_address": "123 Main St",
            "payment_status": "partial",
            "paid_amount": 300.0,
            "items": [{"product_id": prod_id, "quantity": 2, "unit_price": 500.0}],
        },
        headers=auth,
    )
    assert create_res.status_code == 201, create_res.text
    delivery_id = create_res.json()["delivery_id"]
    assert delivery_id is not None

    # Attempt collection of 800 (exceeds remaining 700) -> 400
    r_over = client.post(
        f"/deliveries/{delivery_id}/collections",
        json={"amount": 800.0, "payment_mode": "cash"},
        headers=auth,
    )
    assert r_over.status_code == 400, r_over.text
    assert "Collection amount exceeds remaining balance" in r_over.text

    # Valid collection of 700 -> 201
    r_valid = client.post(
        f"/deliveries/{delivery_id}/collections",
        json={"amount": 700.0, "payment_mode": "cash"},
        headers=auth,
    )
    assert r_valid.status_code == 201, r_valid.text
    coll_data = r_valid.json()
    assert coll_data["amount"] == 700.0
    assert coll_data["reconciliation_status"] == "recorded"

