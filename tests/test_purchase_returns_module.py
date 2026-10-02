"""Automated integration test suite for the Purchase Returns module."""

import os
import sys
import uuid

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi.testclient import TestClient
from app.main import app
from app.core.database import SessionLocal
from app.models import Product, ProductVariant, PurchaseInvoice, PurchaseInvoiceItem, StockMovement, Supplier, User, Warehouse, WarehouseStock
from app.models.purchase_return import PurchaseReturn, PurchaseReturnItem
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


def register_org(name_prefix: str = "PR Test Firm"):
    email = f"pr_admin_{uuid.uuid4().hex[:8]}@example.com"
    r = client.post("/auth/register", json={
        "organization_name": f"{name_prefix} {uuid.uuid4().hex[:6]}",
        "admin_name": "PR Admin",
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
    """Create supplier, warehouse, products, and a confirmed purchase invoice with initial stock."""
    db = SessionLocal()
    try:
        supplier = Supplier(
            id=str(uuid.uuid4()),
            organization_id=org_id,
            name=f"Supplier {uuid.uuid4().hex[:6]}",
            is_active=True,
            total_purchases=5000.0,
        )
        db.add(supplier)

        warehouse = Warehouse(
            id=str(uuid.uuid4()),
            organization_id=org_id,
            name=f"Warehouse {uuid.uuid4().hex[:6]}",
            code=f"WH-{uuid.uuid4().hex[:4]}",
            is_active=True,
        )
        db.add(warehouse)

        product1 = Product(
            id=str(uuid.uuid4()),
            organization_id=org_id,
            name=f"Product 1 {uuid.uuid4().hex[:6]}",
            sku=f"SKU1-{uuid.uuid4().hex[:6]}",
            total_inventory=100,
            is_active=True,
        )
        db.add(product1)

        product2 = Product(
            id=str(uuid.uuid4()),
            organization_id=org_id,
            name=f"Product 2 {uuid.uuid4().hex[:6]}",
            sku=f"SKU2-{uuid.uuid4().hex[:6]}",
            total_inventory=50,
            is_active=True,
        )
        db.add(product2)

        variant2 = ProductVariant(
            id=str(uuid.uuid4()),
            product_id=product2.id,
            name="Variant Red",
            inventory=50,
        )
        db.add(variant2)
        db.commit()

        # Create confirmed purchase invoice
        r_purch = client.post("/purchases", json={
            "supplier_id": supplier.id,
            "warehouse_id": warehouse.id,
            "purchase_status": "confirmed",
            "invoice_number": f"PI-{uuid.uuid4().hex[:6]}",
            "items": [
                {
                    "product_id": product1.id,
                    "product_name": product1.name,
                    "quantity": 20,
                    "purchase_price": 100.0,
                    "tax_rate": 18.0,
                },
                {
                    "product_id": product2.id,
                    "variant_id": variant2.id,
                    "product_name": product2.name,
                    "quantity": 10,
                    "purchase_price": 200.0,
                    "tax_rate": 18.0,
                },
            ],
        }, headers=auth)
        assert r_purch.status_code == 201, r_purch.text
        purch_data = r_purch.json()
        purch_id = purch_data["id"]

        return {
            "supplier": supplier,
            "warehouse": warehouse,
            "product1": product1,
            "product2": product2,
            "variant2": variant2,
            "purchase_id": purch_id,
            "purchase_item1_id": purch_data["items"][0]["id"],
            "purchase_item2_id": purch_data["items"][1]["id"],
        }
    finally:
        db.close()


def main():
    print("\n--- RUNNING PURCHASE RETURNS INTEGRATION TESTS ---")
    auth, org_id = register_org("Main PR Firm")
    base = setup_base_data(auth, org_id)

    # 1. Create draft purchase return
    payload = {
        "purchase_id": base["purchase_id"],
        "reason": "Damaged in transit",
        "items": [
            {
                "purchase_item_id": base["purchase_item1_id"],
                "quantity": 5,
                "reason": "Broken seal",
            },
            {
                "purchase_item_id": base["purchase_item2_id"],
                "quantity": 3,
                "reason": "Wrong variant",
            },
        ],
    }
    r = client.post("/purchase-returns", json=payload, headers=auth)
    check("1. Create draft purchase return status 201", r.status_code == 201)
    ret_data = r.json()
    ret_id = ret_data["id"]
    check("1b. Return status is draft", ret_data["status"] == "draft")
    check("1c. Return number generated (PR-)", ret_data["return_number"].startswith("PR-"))
    check("1d. Items length is 2", len(ret_data["items"]) == 2)
    check("1e. Total return qty is 8", ret_data["totalReturnQty"] == 8)
    check("1f. CamelCase alias returnNumber matches return_number", ret_data["returnNumber"] == ret_data["return_number"])
    check("1g. Supplier name populated in brief", ret_data["supplierName"] is not None)

    # Stock should NOT be deducted while in draft
    db = SessionLocal()
    try:
        p1 = db.get(Product, base["product1"].id)
        check("1h. Draft return does NOT deduct stock", p1.total_inventory == 100)
    finally:
        db.close()

    # 2. Detail endpoint
    r_detail = client.get(f"/purchase-returns/{ret_id}", headers=auth)
    check("2. GET /purchase-returns/{id} status 200", r_detail.status_code == 200)
    check("2b. Detail return number matches", r_detail.json()["id"] == ret_id)

    # 3. List endpoint with search and status filter
    r_list = client.get("/purchase-returns?status=draft", headers=auth)
    check("3. GET /purchase-returns?status=draft status 200", r_list.status_code == 200)
    check("3b. List items non-empty", len(r_list.json()["items"]) >= 1)

    r_search = client.get(f"/purchase-returns?search={ret_data['return_number']}", headers=auth)
    check("3c. List search returns matching record", len(r_search.json()["items"]) == 1)

    # 4. Draft update (PATCH)
    r_patch = client.patch(f"/purchase-returns/{ret_id}", json={
        "notes": "Updated return note for QC team",
        "items": [
            {
                "purchase_item_id": base["purchase_item1_id"],
                "quantity": 4,
            }
        ],
    }, headers=auth)
    check("4. PATCH /purchase-returns/{id} status 200", r_patch.status_code == 200)
    check("4b. Updated totalReturnQty is 4", r_patch.json()["totalReturnQty"] == 4)
    check("4c. Updated notes saved", r_patch.json()["notes"] == "Updated return note for QC team")

    # 5. Invalid purchase ID rejected (404)
    r_invalid_pid = client.post("/purchase-returns", json={
        "purchase_id": str(uuid.uuid4()),
        "items": [{"product_id": base["product1"].id, "quantity": 1}],
    }, headers=auth)
    check("5. Invalid purchase_id rejected 404", r_invalid_pid.status_code == 404)

    # 6. Invalid quantity (<= 0) rejected (422/400)
    r_zero_qty = client.post("/purchase-returns", json={
        "purchase_id": base["purchase_id"],
        "items": [{"purchase_item_id": base["purchase_item1_id"], "quantity": 0}],
    }, headers=auth)
    check("6. Zero/negative quantity rejected", r_zero_qty.status_code in (400, 422))

    # 7. Cumulative over-return rejected (ordered: 20, active draft: 4, requesting 17 -> exceeds 16 remaining)
    r_over = client.post("/purchase-returns", json={
        "purchase_id": base["purchase_id"],
        "items": [{"purchase_item_id": base["purchase_item1_id"], "quantity": 17}],
    }, headers=auth)
    check("7. Cumulative over-return rejected 400", r_over.status_code == 400)
    check("7b. Detailed error message indicates eligible quantity", "exceeds eligible return quantity" in r_over.json()["detail"])

    # 8. Confirm transition (deducts stock exactly once)
    r_conf = client.post(f"/purchase-returns/{ret_id}/confirm", headers=auth)
    check("8. POST /purchase-returns/{id}/confirm status 200", r_conf.status_code == 200)
    conf_data = r_conf.json()
    check("8b. Status changed to confirmed", conf_data["status"] == "confirmed")
    check("8c. confirmedAt timestamp set", conf_data["confirmedAt"] is not None)

    db = SessionLocal()
    try:
        p1 = db.get(Product, base["product1"].id)
        check("8d. Stock deducted upon confirmation (100 -> 96)", p1.total_inventory == 96)
        sm = db.query(StockMovement).filter(
            StockMovement.organization_id == org_id,
            StockMovement.movement_type == "purchase_return",
            StockMovement.product_id == base["product1"].id,
        ).first()
        check("8e. StockMovement ledger entry created", sm is not None and sm.quantity == -4)
    finally:
        db.close()

    # 9. Duplicate confirm / invalid edit on confirmed return rejected
    r_edit_conf = client.patch(f"/purchase-returns/{ret_id}", json={"notes": "Should fail"}, headers=auth)
    check("9. Edit on confirmed return rejected 400", r_edit_conf.status_code == 400)

    # 10. Dispatch transition
    r_disp = client.post(f"/purchase-returns/{ret_id}/dispatch", headers=auth)
    check("10. POST /purchase-returns/{id}/dispatch status 200", r_disp.status_code == 200)
    check("10b. Status changed to dispatched", r_disp.json()["status"] == "dispatched")
    check("10c. dispatchedAt timestamp set", r_disp.json()["dispatchedAt"] is not None)

    # 11. Complete transition (terminal)
    r_comp = client.post(f"/purchase-returns/{ret_id}/complete", headers=auth)
    check("11. POST /purchase-returns/{id}/complete status 200", r_comp.status_code == 200)
    check("11b. Status changed to completed", r_comp.json()["status"] == "completed")
    check("11c. completedAt timestamp set", r_comp.json()["completedAt"] is not None)

    # 12. Invalid transition: cannot cancel completed return
    r_cancel_comp = client.post(f"/purchase-returns/{ret_id}/cancel", json={"reason": "Cannot cancel"}, headers=auth)
    check("12. Cancel completed return rejected 400", r_cancel_comp.status_code == 400)

    # 13. Cancellation of confirmed return restores inventory
    r_new_ret = client.post("/purchase-returns", json={
        "purchase_id": base["purchase_id"],
        "items": [{"purchase_item_id": base["purchase_item1_id"], "quantity": 6}],
    }, headers=auth)
    assert r_new_ret.status_code == 201
    ret2_id = r_new_ret.json()["id"]
    client.post(f"/purchase-returns/{ret2_id}/confirm", headers=auth)

    db = SessionLocal()
    try:
        p1 = db.get(Product, base["product1"].id)
        check("13a. Stock before cancel is 90 (96 - 6)", p1.total_inventory == 90)
    finally:
        db.close()

    r_cancel = client.post(f"/purchase-returns/{ret2_id}/cancel", json={"cancel_reason": "Supplier refused RMA"}, headers=auth)
    check("13b. Cancel confirmed return status 200", r_cancel.status_code == 200)
    check("13c. Status is cancelled", r_cancel.json()["status"] == "cancelled")
    check("13d. cancelReason persisted", r_cancel.json()["cancelReason"] == "Supplier refused RMA")

    db = SessionLocal()
    try:
        p1 = db.get(Product, base["product1"].id)
        check("13e. Stock restored back to 96 after cancellation", p1.total_inventory == 96)
    finally:
        db.close()

    # 14. Multi-tenant isolation: Org B cannot access Org A's purchase return
    auth_b, org_b = register_org("Org B Firm")
    r_cross_get = client.get(f"/purchase-returns/{ret_id}", headers=auth_b)
    check("14. Cross-organization access rejected 404", r_cross_get.status_code == 404)

    r_cross_cancel = client.post(f"/purchase-returns/{ret_id}/cancel", json={"reason": "Hacking"}, headers=auth_b)
    check("14b. Cross-organization action rejected 404", r_cross_cancel.status_code == 404)

    # 15. Report compatibility: GET /reports/purchase-return
    r_rep = client.get("/reports/purchase-return", headers=auth)
    check("15. GET /reports/purchase-return status 200", r_rep.status_code == 200)
    rep_data = r_rep.json()
    check("15b. Report contains completed purchase return rows", len(rep_data["rows"]) >= 1)
    check("15c. Report summary total_returns is positive", rep_data["summary"]["total_returns"] > 0)

    # 16. Legacy endpoint compatibility: POST /purchases/{id}/returns
    r_legacy = client.post(f"/purchases/{base['purchase_id']}/returns", json={
        "items": [{"product_id": base["product1"].id, "quantity": 2}],
        "reason": "Legacy RMA return",
    }, headers=auth)
    if r_legacy.status_code != 200:
        print("LEGACY ERROR:", r_legacy.status_code, r_legacy.text)
    check("16. Legacy POST /purchases/{id}/returns status 200", r_legacy.status_code == 200)

    db = SessionLocal()
    try:
        # Verify a PurchaseReturn record was created behind the scenes
        legacy_pr = db.query(PurchaseReturn).filter(
            PurchaseReturn.organization_id == org_id,
            PurchaseReturn.purchase_id == base["purchase_id"],
            PurchaseReturn.reason == "Legacy RMA return",
        ).first()
        check("16b. Legacy call created canonical PurchaseReturn record", legacy_pr is not None)
        p1 = db.get(Product, base["product1"].id)
        check("16c. Stock decremented to 94 (96 - 2)", p1.total_inventory == 94)
    finally:
        db.close()

    print(f"\nRESULTS: {passed_count} PASSED, {failed_count} FAILED\n")
    assert failed_count == 0, f"{failed_count} tests failed!"


if __name__ == "__main__":
    main()
