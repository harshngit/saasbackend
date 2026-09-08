"""Comprehensive automated test suite for the Goods Receipt Note (GRN) Module."""

import os
import sys
import uuid

# Add project root to sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi.testclient import TestClient
from app.main import app
from app.core.database import SessionLocal
from app.models import WarehouseStock, StockMovement
from app.models.product import Product
from app.models import PurchaseInvoice, PurchaseInvoiceItem
from app.models.supplier import Supplier
from app.models.user import User
from app.models.warehouse import Warehouse
from app.seed import main as seed_main

seed_main()
client = TestClient(app)

passed_count = 0
failed_count = 0


def check(description: str, condition: bool):
    global passed_count, failed_count
    if condition:
        print(f"  PASS  {description}")
        passed_count += 1
    else:
        print(f"  FAIL  {description}")
        failed_count += 1


def register_org(name_prefix: str = "GRN Test Firm"):
    email = f"grn_admin_{uuid.uuid4().hex[:8]}@example.com"
    r = client.post("/auth/register", json={
        "organization_name": f"{name_prefix} {uuid.uuid4().hex[:6]}",
        "admin_name": "GRN Admin",
        "email": email,
        "password": "Password123!",
        "role": "admin",
    })
    assert r.status_code == 201, r.text
    token = r.json()["tokens"]["access_token"]
    auth = {"Authorization": f"Bearer {token}"}

    db = SessionLocal()
    try:
        user = db.query(User).filter(User.email == email).first()
        org_id = user.organization_id
    finally:
        db.close()

    return auth, org_id


def setup_base_data(auth, org_id):
    """Create a supplier, warehouse, product, and confirmed purchase invoice."""
    db = SessionLocal()
    try:
        # Supplier
        supplier = Supplier(
            id=str(uuid.uuid4()),
            organization_id=org_id,
            name=f"Supplier {uuid.uuid4().hex[:6]}",
            is_active=True,
        )
        db.add(supplier)

        # Warehouse
        warehouse = Warehouse(
            id=str(uuid.uuid4()),
            organization_id=org_id,
            name=f"Warehouse {uuid.uuid4().hex[:6]}",
            code=f"WH-{uuid.uuid4().hex[:4]}",
            is_active=True,
        )
        db.add(warehouse)

        # Product
        product = Product(
            id=str(uuid.uuid4()),
            organization_id=org_id,
            name=f"Product {uuid.uuid4().hex[:6]}",
            sku=f"SKU-{uuid.uuid4().hex[:6]}",
            is_active=True,
        )
        db.add(product)
        db.commit()

        # Purchase Invoice
        r_purch = client.post("/purchases", json={
            "supplier_id": supplier.id,
            "warehouse_id": warehouse.id,
            "invoice_number": f"INV-{uuid.uuid4().hex[:6]}",
            "items": [
                {
                    "product_id": product.id,
                    "quantity": 100,
                    "purchase_price": 10.0,
                }
            ]
        }, headers=auth)
        assert r_purch.status_code == 201, r_purch.text
        purch_data = r_purch.json()
        purchase_id = purch_data["id"]
        purchase_item_id = purch_data["items"][0]["id"]

        # Confirm Purchase
        r_conf = client.post(f"/purchases/{purchase_id}/confirm", headers=auth)
        assert r_conf.status_code == 200, r_conf.text

        return {
            "supplier_id": supplier.id,
            "warehouse_id": warehouse.id,
            "product_id": product.id,
            "purchase_id": purchase_id,
            "purchase_item_id": purchase_item_id,
        }
    finally:
        db.close()


def run_all_tests():
    print("\n--- RUNNING GRN MODULE TEST SUITE ---")
    auth, org_id = register_org()
    base = setup_base_data(auth, org_id)

    # -------------------------------------------------------------
    # Test 1 — Draft GRN creation
    # -------------------------------------------------------------
    print("\nTest 1 — Draft GRN Creation")
    r_grn = client.post("/grns", json={
        "purchase_id": base["purchase_id"],
        "supplier_id": base["supplier_id"],
        "warehouse_id": base["warehouse_id"],
        "notes": "Draft receipt test",
        "items": [
            {
                "purchase_item_id": base["purchase_item_id"],
                "product_id": base["product_id"],
                "received_qty": 10,
                "damaged_qty": 0,
                "rejected_qty": 0,
            }
        ]
    }, headers=auth)
    check("Create Draft GRN status 201", r_grn.status_code == 201)
    grn_1_data = r_grn.json()
    grn_1_id = grn_1_data["id"]
    check("Draft GRN has status draft", grn_1_data["status"] == "draft")
    check("Draft GRN accepted_qty is 10", grn_1_data["items"][0]["accepted_qty"] == 10)

    # Verify stock unchanged in DB
    db = SessionLocal()
    try:
        wh_stock = db.query(WarehouseStock).filter(
            WarehouseStock.organization_id == org_id,
            WarehouseStock.warehouse_id == base["warehouse_id"],
            WarehouseStock.product_id == base["product_id"],
        ).first()
        stock_on_hand = wh_stock.on_hand_quantity if wh_stock else 0
        check("Stock on hand remains 0 for Draft GRN", stock_on_hand == 0)

        purch_item = db.query(PurchaseInvoiceItem).filter(PurchaseInvoiceItem.id == base["purchase_item_id"]).first()
        check("Purchase item received_qty remains 0 for Draft GRN", purch_item.received_qty == 0)
    finally:
        db.close()

    # -------------------------------------------------------------
    # Test 2 — Confirm GRN
    # -------------------------------------------------------------
    print("\nTest 2 — Confirm GRN (Stock Inward + Purchase Rollup)")
    r_conf = client.post(f"/grns/{grn_1_id}/confirm", headers=auth)
    check("Confirm GRN 1 returns 200", r_conf.status_code == 200)
    conf_1_data = r_conf.json()
    check("GRN 1 status is confirmed", conf_1_data["status"] == "confirmed")

    db = SessionLocal()
    try:
        wh_stock = db.query(WarehouseStock).filter(
            WarehouseStock.organization_id == org_id,
            WarehouseStock.warehouse_id == base["warehouse_id"],
            WarehouseStock.product_id == base["product_id"],
        ).first()
        check("Warehouse stock increased to 10", wh_stock and wh_stock.on_hand_quantity == 10)

        movements = db.query(StockMovement).filter(
            StockMovement.organization_id == org_id,
            StockMovement.warehouse_id == base["warehouse_id"],
            StockMovement.product_id == base["product_id"],
        ).all()
        check("StockMovement row created", len(movements) == 1)
        if len(movements) == 1:
            check("StockMovement movement_type is purchase_in", movements[0].movement_type == "purchase_in")
            check("StockMovement quantity is +10", movements[0].quantity == 10)

        purch_item = db.query(PurchaseInvoiceItem).filter(PurchaseInvoiceItem.id == base["purchase_item_id"]).first()
        check("Purchase item received_qty updated to 10", purch_item.received_qty == 10)

        purch = db.query(PurchaseInvoice).filter(PurchaseInvoice.id == base["purchase_id"]).first()
        check("Purchase receiving_status is partially_received", purch.receiving_status == "partially_received")
    finally:
        db.close()

    # -------------------------------------------------------------
    # Test 3 — Damaged / Rejected Calculation
    # -------------------------------------------------------------
    print("\nTest 3 — Damaged/Rejected Calculation (Received=10, Damaged=2, Rejected=1 -> Accepted=7)")
    r_grn_2 = client.post("/grns", json={
        "purchase_id": base["purchase_id"],
        "supplier_id": base["supplier_id"],
        "warehouse_id": base["warehouse_id"],
        "items": [
            {
                "purchase_item_id": base["purchase_item_id"],
                "product_id": base["product_id"],
                "received_qty": 10,
                "damaged_qty": 2,
                "rejected_qty": 1,
            }
        ]
    }, headers=auth)
    check("Create GRN 2 status 201", r_grn_2.status_code == 201)
    grn_2_data = r_grn_2.json()
    check("Accepted qty is computed as 7", grn_2_data["items"][0]["accepted_qty"] == 7)

    r_conf_2 = client.post(f"/grns/{grn_2_data['id']}/confirm", headers=auth)
    check("Confirm GRN 2 status 200", r_conf_2.status_code == 200)

    db = SessionLocal()
    try:
        wh_stock = db.query(WarehouseStock).filter(
            WarehouseStock.organization_id == org_id,
            WarehouseStock.warehouse_id == base["warehouse_id"],
            WarehouseStock.product_id == base["product_id"],
        ).first()
        # Previous stock was 10, now +7 = 17
        check("Warehouse stock updated to 17 (10 + 7)", wh_stock.on_hand_quantity == 17)

        purch_item = db.query(PurchaseInvoiceItem).filter(PurchaseInvoiceItem.id == base["purchase_item_id"]).first()
        check("Purchase item received_qty updated to 17", purch_item.received_qty == 17)
    finally:
        db.close()

    # -------------------------------------------------------------
    # Test 4 — Invalid Quantities (Damaged + Rejected > Received)
    # -------------------------------------------------------------
    print("\nTest 4 — Invalid Quantities (Damaged + Rejected > Received)")
    r_bad_qty = client.post("/grns", json={
        "purchase_id": base["purchase_id"],
        "supplier_id": base["supplier_id"],
        "warehouse_id": base["warehouse_id"],
        "items": [
            {
                "purchase_item_id": base["purchase_item_id"],
                "product_id": base["product_id"],
                "received_qty": 10,
                "damaged_qty": 8,
                "rejected_qty": 5, # Total 13 > 10
            }
        ]
    }, headers=auth)
    check("Invalid damaged+rejected blocked with 400", r_bad_qty.status_code == 400)

    # -------------------------------------------------------------
    # Test 5 — Negative Quantities
    # -------------------------------------------------------------
    print("\nTest 5 — Negative Quantities")
    r_neg_qty = client.post("/grns", json={
        "purchase_id": base["purchase_id"],
        "supplier_id": base["supplier_id"],
        "warehouse_id": base["warehouse_id"],
        "items": [
            {
                "purchase_item_id": base["purchase_item_id"],
                "product_id": base["product_id"],
                "received_qty": -5,
                "damaged_qty": 0,
                "rejected_qty": 0,
            }
        ]
    }, headers=auth)
    check("Negative received_qty blocked with 400 or 422", r_neg_qty.status_code in (400, 422))

    # -------------------------------------------------------------
    # Test 6 & 7 — Partial receiving & Multiple GRNs to Full receiving
    # -------------------------------------------------------------
    print("\nTest 6 & 7 — Multiple GRNs to Full Receiving (Ordered = 100)")
    # Currently accepted: 17 (from GRN 1 & 2).
    # Create GRN 3 with accepted = 83 to reach exactly 100.
    r_grn_3 = client.post("/grns", json={
        "purchase_id": base["purchase_id"],
        "supplier_id": base["supplier_id"],
        "warehouse_id": base["warehouse_id"],
        "items": [
            {
                "purchase_item_id": base["purchase_item_id"],
                "product_id": base["product_id"],
                "received_qty": 83,
                "damaged_qty": 0,
                "rejected_qty": 0,
            }
        ]
    }, headers=auth)
    check("Create GRN 3 status 201", r_grn_3.status_code == 201)
    grn_3_id = r_grn_3.json()["id"]

    r_conf_3 = client.post(f"/grns/{grn_3_id}/confirm", headers=auth)
    check("Confirm GRN 3 status 200", r_conf_3.status_code == 200)

    db = SessionLocal()
    try:
        purch_item = db.query(PurchaseInvoiceItem).filter(PurchaseInvoiceItem.id == base["purchase_item_id"]).first()
        check("Purchase item received_qty reached 100", purch_item.received_qty == 100)

        purch = db.query(PurchaseInvoice).filter(PurchaseInvoice.id == base["purchase_id"]).first()
        check("Purchase receiving_status is fully_received", purch.receiving_status == "fully_received")
    finally:
        db.close()

    # -------------------------------------------------------------
    # Test 8 — Over Receipt Protection
    # -------------------------------------------------------------
    print("\nTest 8 — Over Receipt Protection")
    # Total received is 100 / ordered 100. Try adding 1 more unit.
    r_over = client.post("/grns", json={
        "purchase_id": base["purchase_id"],
        "supplier_id": base["supplier_id"],
        "warehouse_id": base["warehouse_id"],
        "items": [
            {
                "purchase_item_id": base["purchase_item_id"],
                "product_id": base["product_id"],
                "received_qty": 1,
                "damaged_qty": 0,
                "rejected_qty": 0,
            }
        ]
    }, headers=auth)
    check("Create GRN exceeding ordered_qty succeeds as Draft", r_over.status_code == 201)
    over_grn_id = r_over.json()["id"]

    r_over_conf = client.post(f"/grns/{over_grn_id}/confirm", headers=auth)
    check("Confirming over-receipt GRN blocked with 400", r_over_conf.status_code == 400)

    # -------------------------------------------------------------
    # Test 9 — Duplicate Confirmation Protection
    # -------------------------------------------------------------
    print("\nTest 9 — Duplicate Confirmation Protection")
    r_dup = client.post(f"/grns/{grn_1_id}/confirm", headers=auth)
    check("Confirming an already confirmed GRN blocked with 400", r_dup.status_code == 400)

    # -------------------------------------------------------------
    # Test 10 — Cancel Draft GRN
    # -------------------------------------------------------------
    print("\nTest 10 — Cancel Draft GRN")
    r_cancel_draft = client.post(f"/grns/{over_grn_id}/cancel", headers=auth)
    check("Cancel draft GRN returns 200", r_cancel_draft.status_code == 200)
    check("Status is cancelled", r_cancel_draft.json()["status"] == "cancelled")

    # -------------------------------------------------------------
    # Test 11 — Cancel Confirmed GRN Blocked
    # -------------------------------------------------------------
    print("\nTest 11 — Cancel Confirmed GRN Blocked")
    r_cancel_conf = client.post(f"/grns/{grn_1_id}/cancel", headers=auth)
    check("Cancelling confirmed GRN blocked with 400", r_cancel_conf.status_code == 400)

    # -------------------------------------------------------------
    # Test 12 — Edit Confirmed GRN Blocked
    # -------------------------------------------------------------
    print("\nTest 12 — Edit Confirmed GRN Blocked")
    r_edit_conf = client.patch(f"/grns/{grn_1_id}", json={
        "notes": "Hacked notes",
    }, headers=auth)
    check("Editing confirmed GRN blocked with 400", r_edit_conf.status_code == 400)

    # -------------------------------------------------------------
    # Test 13 — Cross-Tenant References Blocked
    # -------------------------------------------------------------
    print("\nTest 13 — Cross-Tenant References Blocked")
    auth_b, org_id_b = register_org("Tenant B Firm")
    base_b = setup_base_data(auth_b, org_id_b)

    # Try creating GRN in Tenant A using Tenant B's warehouse
    r_cross = client.post("/grns", json={
        "purchase_id": base["purchase_id"],
        "supplier_id": base["supplier_id"],
        "warehouse_id": base_b["warehouse_id"], # Org B warehouse!
        "items": [
            {
                "purchase_item_id": base["purchase_item_id"],
                "product_id": base["product_id"],
                "received_qty": 5,
            }
        ]
    }, headers=auth)
    check("Cross-tenant warehouse blocked with 400", r_cross.status_code == 400)

    # -------------------------------------------------------------
    # Test 14 — Inactive Warehouse Blocked
    # -------------------------------------------------------------
    print("\nTest 14 — Inactive Warehouse Blocked")
    db = SessionLocal()
    try:
        wh = db.query(Warehouse).filter(Warehouse.id == base["warehouse_id"]).first()
        wh.is_active = False
        db.commit()
    finally:
        db.close()

    # Create new purchase with 20 items for inactive WH test
    db = SessionLocal()
    try:
        wh_active = Warehouse(
            id=str(uuid.uuid4()),
            organization_id=org_id,
            name="Temp WH Active",
            code=f"WH-TMP-{uuid.uuid4().hex[:4]}",
            is_active=True
        )
        db.add(wh_active)
        db.commit()
        temp_wh_id = wh_active.id
    finally:
        db.close()

    r_purch_inact = client.post("/purchases", json={
        "supplier_id": base["supplier_id"],
        "warehouse_id": temp_wh_id,
        "invoice_number": f"INV-INACT-{uuid.uuid4().hex[:6]}",
        "items": [{"product_id": base["product_id"], "quantity": 20, "purchase_price": 5.0}]
    }, headers=auth)
    p_inact_data = r_purch_inact.json()
    client.post(f"/purchases/{p_inact_data['id']}/confirm", headers=auth)

    # Create GRN pointing to the now INACTIVE warehouse base["warehouse_id"]
    r_grn_inact = client.post("/grns", json={
        "purchase_id": p_inact_data["id"],
        "supplier_id": base["supplier_id"],
        "warehouse_id": base["warehouse_id"], # Inactive!
        "items": [{"purchase_item_id": p_inact_data["items"][0]["id"], "product_id": base["product_id"], "received_qty": 5}]
    }, headers=auth)
    check("Creation or Confirmation on inactive warehouse blocked with 400", r_grn_inact.status_code == 400)

    # Restore warehouse active state
    db = SessionLocal()
    try:
        wh = db.query(Warehouse).filter(Warehouse.id == base["warehouse_id"]).first()
        wh.is_active = True
        db.commit()
    finally:
        db.close()

    # -------------------------------------------------------------
    # Test 15 — Missing Warehouse Stock Row Creation
    # -------------------------------------------------------------
    print("\nTest 15 — Missing Warehouse Stock Row Safely Created")
    # Setup new product with zero existing stock row
    db = SessionLocal()
    try:
        new_prod = Product(
            id=str(uuid.uuid4()),
            organization_id=org_id,
            name="New Product No Stock Row",
            sku=f"SKU-NEW-{uuid.uuid4().hex[:6]}",
            is_active=True,
        )
        db.add(new_prod)
        db.commit()
        new_prod_id = new_prod.id
    finally:
        db.close()

    r_p_new = client.post("/purchases", json={
        "supplier_id": base["supplier_id"],
        "warehouse_id": base["warehouse_id"],
        "invoice_number": f"INV-NEW-{uuid.uuid4().hex[:6]}",
        "items": [{"product_id": new_prod_id, "quantity": 50, "purchase_price": 12.0}]
    }, headers=auth)
    p_new_data = r_p_new.json()
    client.post(f"/purchases/{p_new_data['id']}/confirm", headers=auth)

    r_grn_new = client.post("/grns", json={
        "purchase_id": p_new_data["id"],
        "supplier_id": base["supplier_id"],
        "warehouse_id": base["warehouse_id"],
        "items": [{"purchase_item_id": p_new_data["items"][0]["id"], "product_id": new_prod_id, "received_qty": 15}]
    }, headers=auth)
    grn_new_id = r_grn_new.json()["id"]

    r_conf_new = client.post(f"/grns/{grn_new_id}/confirm", headers=auth)
    check("Confirmation of GRN for new stock row succeeds", r_conf_new.status_code == 200)

    db = SessionLocal()
    try:
        wh_stock_new = db.query(WarehouseStock).filter(
            WarehouseStock.organization_id == org_id,
            WarehouseStock.warehouse_id == base["warehouse_id"],
            WarehouseStock.product_id == new_prod_id,
        ).first()
        check("WarehouseStock row created automatically with quantity 15", wh_stock_new and wh_stock_new.on_hand_quantity == 15)
    finally:
        db.close()

    # -------------------------------------------------------------
    # Test 17 — Contract Cleanup: Close Guards, GRN Delete Guards, & Supplier PUT Route
    # -------------------------------------------------------------
    print("\nTest 17 — Contract Cleanup: Close Guards, GRN Delete Guards, & Supplier PUT Route")

    # 1. Purchase Close Guards
    # Create PO A (not_received)
    r_po_nr = client.post("/purchases", json={
        "supplier_id": base["supplier_id"],
        "warehouse_id": base["warehouse_id"],
        "invoice_number": f"INV-NR-{uuid.uuid4().hex[:6]}",
        "items": [{"product_id": base["product_id"], "quantity": 10, "purchase_price": 5.0}]
    }, headers=auth)
    po_nr_id = r_po_nr.json()["id"]
    client.post(f"/purchases/{po_nr_id}/confirm", headers=auth)

    r_close_nr = client.post(f"/purchases/{po_nr_id}/close", headers=auth)
    check("Close on not_received purchase blocked with 400", r_close_nr.status_code == 400)

    # Create GRN partial on PO A (partially_received)
    r_grn_pr = client.post("/grns", json={
        "purchase_id": po_nr_id,
        "supplier_id": base["supplier_id"],
        "warehouse_id": base["warehouse_id"],
        "items": [{"purchase_item_id": r_po_nr.json()["items"][0]["id"], "product_id": base["product_id"], "received_qty": 4}]
    }, headers=auth)
    client.post(f"/grns/{r_grn_pr.json()['id']}/confirm", headers=auth)

    r_close_pr = client.post(f"/purchases/{po_nr_id}/close", headers=auth)
    check("Close on partially_received purchase blocked with 400", r_close_pr.status_code == 400)

    # Receive remaining 6 units on PO A (fully_received)
    r_grn_fr = client.post("/grns", json={
        "purchase_id": po_nr_id,
        "supplier_id": base["supplier_id"],
        "warehouse_id": base["warehouse_id"],
        "items": [{"purchase_item_id": r_po_nr.json()["items"][0]["id"], "product_id": base["product_id"], "received_qty": 6}]
    }, headers=auth)
    client.post(f"/grns/{r_grn_fr.json()['id']}/confirm", headers=auth)

    r_close_fr = client.post(f"/purchases/{po_nr_id}/close", headers=auth)
    check("Close on fully_received purchase returns 200", r_close_fr.status_code == 200)
    check("Status updated to closed", r_close_fr.json()["status"] == "closed")

    # 2. GRN Delete Contract
    # Create Draft GRN
    r_d_draft = client.post("/grns", json={
        "purchase_id": p_new_data["id"],
        "supplier_id": base["supplier_id"],
        "warehouse_id": base["warehouse_id"],
        "items": [{"purchase_item_id": p_new_data["items"][0]["id"], "product_id": new_prod_id, "received_qty": 5}]
    }, headers=auth)
    d_draft_id = r_d_draft.json()["id"]

    r_del_draft = client.delete(f"/grns/{d_draft_id}", headers=auth)
    check("Delete Draft GRN returns 204", r_del_draft.status_code == 204)

    # Create Cancelled GRN
    r_d_canc = client.post("/grns", json={
        "purchase_id": p_new_data["id"],
        "supplier_id": base["supplier_id"],
        "warehouse_id": base["warehouse_id"],
        "items": [{"purchase_item_id": p_new_data["items"][0]["id"], "product_id": new_prod_id, "received_qty": 5}]
    }, headers=auth)
    d_canc_id = r_d_canc.json()["id"]
    client.post(f"/grns/{d_canc_id}/cancel", headers=auth)

    r_del_canc = client.delete(f"/grns/{d_canc_id}", headers=auth)
    check("Delete Cancelled GRN blocked with 400", r_del_canc.status_code == 400)

    # Delete Confirmed GRN
    r_del_conf = client.delete(f"/grns/{grn_1_id}", headers=auth)
    check("Delete Confirmed GRN blocked with 400", r_del_conf.status_code == 400)

    # 3. Supplier Canonical PUT Update Route
    r_supp_put = client.put(f"/suppliers/{base['supplier_id']}", json={
        "notes": "Updated supplier master notes via PUT",
    }, headers=auth)
    check("Supplier canonical PUT update returns 200", r_supp_put.status_code == 200)

    print(f"\nGRN MODULE TEST RESULTS: Passed={passed_count}, Failed={failed_count}")
    assert failed_count == 0, f"GRN module test suite had {failed_count} failures!"


if __name__ == "__main__":
    run_all_tests()
