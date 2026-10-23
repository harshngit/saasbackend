"""GET/PUT /organizations/settings/fields: the canonical field catalog of the nine modules,
effective visibility, partial-merge updates, legacy settings, and that visibility never
changes what the module APIs accept or store."""

import copy
import typing
import uuid

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.database import Base, engine
from app.core.security import create_access_token
from app.main import app
from app.models import Customer, Lead, Organization, User
from app.schemas.company import CompanySettingsUpdate
from app.schemas.customer_profile import CustomerProfileIn
from app.schemas.expense import ExpenseCreate, ExpenseUpdate
from app.schemas.lead import LeadCreate, LeadUpdate
from app.schemas.product import ProductCreate, ProductUpdate
from app.schemas.purchase import PurchaseCreate, PurchaseUpdate
from app.schemas.quotation import QuotationCreate, QuotationUpdate
from app.schemas.sales_order import OrderCreate, OrderUpdate
from app.schemas.supplier import SupplierCreate, SupplierUpdate
from app.services import entitlement_service, field_settings_service as fs

MODULES = ["company", "lead", "customer", "quotation", "product", "sales_order", "supplier", "purchase", "expense"]
SCHEMAS = {
    "company": [CompanySettingsUpdate],
    "lead": [LeadCreate, LeadUpdate],
    "customer": [CustomerProfileIn],
    "quotation": [QuotationCreate, QuotationUpdate],
    "product": [ProductCreate, ProductUpdate],
    "sales_order": [OrderCreate, OrderUpdate],
    "supplier": [SupplierCreate, SupplierUpdate],
    "purchase": [PurchaseCreate, PurchaseUpdate],
    "expense": [ExpenseCreate, ExpenseUpdate],
}
URL = "/organizations/settings/fields"


@pytest.fixture
def db_session():
    Base.metadata.create_all(bind=engine)
    session = Session(bind=engine)
    try:
        yield session
    finally:
        session.close()


def _org(db: Session, saved=None, system_role: str = "admin") -> tuple[Organization, TestClient]:
    org = Organization(id=str(uuid.uuid4()), name=f"Fields {uuid.uuid4().hex[:6]}", company_code="CMP-10001",
                       field_settings=saved)
    db.add(org)
    db.flush()
    user = User(id=str(uuid.uuid4()), organization_id=org.id, email=f"f_{uuid.uuid4().hex[:8]}@x.com", name="Admin",
                password_hash="h", role="admin" if system_role == "admin" else "sales_officer",
                system_role=system_role, is_active=True)
    db.add(user)
    db.commit()
    client = TestClient(app)
    token = create_access_token(user.id, role=user.role, organization_id=org.id)
    client.headers = {"Authorization": f"Bearer {token}"}
    return org, client


def _stored(db: Session, org: Organization):
    db.expire_all()
    return db.get(Organization, org.id).field_settings


def _meta(module: str, key: str) -> dict:
    return next(f for f in fs.available_fields()[module]["fields"] if f["key"] == key)


def _resolve(models: list[type[BaseModel]], path: str) -> bool:
    """Is `path` (a.b for nested sections) an input field of any of these request schemas?"""
    for model in models:
        current, found = model, True
        for part in path.split("."):
            field = current.model_fields.get(part) if current is not None else None
            if field is None:
                found = False
                break
            nested = [a for a in (typing.get_args(field.annotation) or (field.annotation,))
                      if isinstance(a, type) and issubclass(a, BaseModel)]
            current = nested[0] if nested else None
        if found:
            return True
    return False


# ------------------------------------------------------------------ catalog


def test_catalog_has_the_nine_modules_and_real_schema_keys():
    catalog = fs.available_fields()
    assert list(catalog) == MODULES
    for module, spec in catalog.items():
        keys = [f["key"] for f in spec["fields"]]
        assert len(keys) == len(set(keys)), module
        assert keys == spec["mandatory"] + spec["conditional"] + spec["non_configurable"] + spec["optional"]
        for key in keys:
            assert _resolve(SCHEMAS[module], key), f"{module}.{key} is not an API input"


@pytest.mark.parametrize("module, mandatory, conditional, non_configurable, optional", [
    ("company", ["name"], [], [], ["legal_name", "primary_mobile", "registered_address", "branch_addresses", "logo_url"]),
    ("lead", ["name", "mobile_number", "lead_source"], [], [], ["email", "assigned_salesperson_id", "notes"]),
    ("customer", ["basic_information.customer_name"], [], [],
     ["contact_information.mobile_number", "payment_information.opening_balance", "documents.pan_card_id"]),
    ("quotation", ["items"], ["customer_id", "lead_id"], [], ["valid_until", "terms_conditions"]),
    ("product", ["name"], [], ["has_variants", "variant_attributes", "variations"],
     ["pricing.selling_price", "minimum_stock_level", "category_id", "sub_category_id", "brand_id",
      "preferred_supplier_id", "vendor", "hsn_code"]),
    ("sales_order", ["customer_id", "items"], ["delivery_address", "paid_amount"], [], ["discount", "tax", "shipping_address"]),
    ("supplier", ["name"], [], [], ["credit_limit", "opening_balance", "gst_number"]),
    ("purchase", ["invoice_number", "supplier_id", "items"], [], [], ["freight_charges", "round_off", "amount_paid"]),
    ("expense", ["category"], ["amount"], [], ["items", "tax_amount", "tds_amount"]),
])
def test_classifications(module, mandatory, conditional, non_configurable, optional):
    spec = fs.available_fields()[module]
    assert spec["mandatory"] == mandatory
    assert spec["conditional"] == conditional
    assert spec["non_configurable"] == non_configurable
    for key in optional:
        assert key in spec["optional"], key
    for key in non_configurable:
        meta = _meta(module, key)
        assert meta["classification"] == "non_configurable" and meta["locked"] and not meta["configurable"]
    for key in mandatory + conditional:
        meta = _meta(module, key)
        assert meta["locked"] and not meta["configurable"] and meta["default_visible"]
    for key in conditional:
        assert _meta(module, key)["condition"]
    for key in optional:
        meta = _meta(module, key)
        assert meta["configurable"] and not meta["locked"] and meta["condition"] is None


@pytest.mark.parametrize("module, excluded", [
    ("company", ["company_status", "status", "tax_configuration", "invoice_settings", "number_prefixes",
                 "employee_id_prefix", "company_code", "doc_other_files", "address", "branch_address"]),
    ("lead", ["lead_id", "lead_status", "status", "mobile", "source", "assigned_sales_officer_id",
              "converted_customer_id", "converted_at"]),
    ("customer", ["basic_information.status", "sales_crm_information.sales_representative", "customer_id",
                  "name", "phone", "gst_number"]),
    ("quotation", ["quotation_number", "status"]),
    ("product", ["product_id", "status", "is_active", "total_inventory", "price", "min_stock",
                 "sub_category", "brand"]),
    ("sales_order", ["order_status", "payment_status"]),
    ("supplier", ["supplier_code"]),
    ("purchase", ["grn_number", "purchase_status", "receiving_status", "approval_status", "requested_by",
                  "received_by", "payment_status"]),
    ("expense", ["expense_status", "payment_status", "approval_status", "status"]),
])
def test_system_and_workflow_fields_are_excluded(module, excluded):
    keys = {f["key"] for f in fs.available_fields()[module]["fields"]}
    assert not keys & set(excluded)


def test_company_completeness_checklist_is_not_mandatory():
    from app.routers.organizations import REQUIRED_COMPANY_FIELDS

    company = fs.available_fields()["company"]
    assert company["mandatory"] == ["name"]
    for key in REQUIRED_COMPANY_FIELDS:
        if key not in ("name", "tax_configuration", "invoice_settings", "company_status"):
            assert key in company["optional"], key


def test_every_field_has_an_explicit_label_and_defaults_on():
    for module, spec in fs.available_fields().items():
        for field in spec["fields"]:
            label = field["label"]
            assert isinstance(label, str) and label and label == label.strip(), (module, field["key"])
            # Written by hand in the catalog, not derived from the key
            kind = field["classification"]
            explicit = fs.CATALOG[module][kind][field["key"]]
            assert label == (explicit[0] if kind == "conditional" else explicit)
        labels = [f["label"] for f in spec["fields"]]
        assert len(labels) == len(set(labels)), module
        assert all(f["default_visible"] for f in spec["fields"]), module
    for module, key, label in (("product", "preferred_supplier_id", "Preferred Supplier"),
                               ("product", "sub_category_id", "Subcategory"), ("product", "brand_id", "Brand"),
                               ("quotation", "customer_id", "Customer"), ("sales_order", "customer_id", "Customer"),
                               ("customer", "basic_information.customer_name", "Customer Name")):
        assert _meta(module, key)["label"] == label
    assert _meta("product", "sub_category_id")["depends_on"] == "category_id"
    assert _meta("product", "category_id")["depends_on"] is None


# ------------------------------------------------------------------ GET


def test_new_org_sees_every_field(db_session):
    org, client = _org(db_session)
    res = client.get(URL)
    assert res.status_code == 200
    body = res.json()
    assert list(body["available_fields"]) == MODULES
    for module, spec in body["available_fields"].items():
        assert set(body["field_settings"][module]) == {f["key"] for f in spec["fields"]}
        assert all(body["field_settings"][module].values()), module
    assert _stored(db_session, org) is None  # GET writes nothing


def test_saved_values_legacy_keys_and_new_fields(db_session):
    saved = {
        "customer": {"email": False, "phone": True, "name": False, "made_up": False},
        "product": {"price": False, "min_stock": False, "brand": True, "variations": False},
        "sales": {"discount": False, "assigned_delivery_partner_id": False},
        "expenses": {"receipt_url": False},
        "company": {"employee_id_prefix": False, "doc_other_files": False, "website": False},
        "retired_module": {"x": False},
        "lead": "not-a-dict",
        "supplier": {"notes": "no"},
    }
    org, client = _org(db_session, copy.deepcopy(saved))
    eff = client.get(URL).json()["field_settings"]
    assert eff["customer"]["contact_information.email_address"] is False
    assert eff["customer"]["contact_information.mobile_number"] is True
    assert eff["customer"]["basic_information.customer_name"] is True  # mandatory stays visible
    assert eff["product"]["pricing.selling_price"] is False and eff["product"]["minimum_stock_level"] is False
    assert eff["product"]["brand_id"] is True  # legacy `brand`
    assert eff["product"]["variations"] is True  # variants are not controlled by settings
    assert eff["sales_order"]["discount"] is False and eff["sales_order"]["delivery_partner_id"] is False
    assert eff["expense"]["receipt_url"] is False
    assert eff["company"]["website"] is False
    # Fields the old catalog never had default on; nothing else picked up an unrelated value
    assert eff["customer"]["documents.pan_card_id"] is True and eff["product"]["hsn_code"] is True
    assert eff["supplier"]["notes"] is True
    off = {(m, k) for m, v in eff.items() for k, on in v.items() if not on}
    assert off == {("customer", "contact_information.email_address"), ("product", "pricing.selling_price"),
                   ("product", "minimum_stock_level"), ("sales_order", "discount"),
                   ("sales_order", "delivery_partner_id"), ("expense", "receipt_url"), ("company", "website")}
    assert _stored(db_session, org) == saved  # GET writes nothing


def test_canonical_keys_win_over_legacy_ones():
    raw = {
        "sales": {"discount": False, "tax": True},
        "sales_order": {"discount": True},
        "customer": {"email": True, "contact_information.email_address": False},
        "product": {"pricing.selling_price": True, "price": False},
    }
    eff = fs.effective_settings(raw)
    assert eff["sales_order"]["discount"] is True and eff["sales_order"]["tax"] is True
    assert eff["customer"]["contact_information.email_address"] is False
    assert eff["product"]["pricing.selling_price"] is True


# ------------------------------------------------------------------ PUT


def test_partial_put_merges(db_session):
    org, client = _org(db_session, {"customer": {"email": False}, "sales": {"tax": False}, "retired": {"x": True}})
    res = client.put(URL, json={"field_settings": {"customer": {"contact_information.website": False}}})
    assert res.status_code == 200
    eff = res.json()["field_settings"]
    assert eff["customer"]["contact_information.website"] is False
    assert eff["customer"]["contact_information.email_address"] is False  # same module kept
    assert eff["sales_order"]["tax"] is False  # other module kept
    assert eff["customer"]["contact_information.mobile_number"] is True

    res = client.put(URL, json={"field_settings": {"customer": {"contact_information.website": True},
                                                   "expense": {"tags": False}}})
    eff = res.json()["field_settings"]
    assert eff["customer"]["contact_information.website"] is True and eff["expense"]["tags"] is False
    assert eff["customer"]["contact_information.email_address"] is False
    assert client.get(URL).json()["field_settings"] == eff

    stored = _stored(db_session, org)
    assert stored["retired"] == {"x": True} and stored["sales"] == {"tax": False}  # nothing deleted
    assert stored["customer"]["contact_information.email_address"] is False
    assert "contact_information.mobile_number" not in stored["customer"]  # defaults are not frozen


def test_put_accepts_legacy_names(db_session):
    _, client = _org(db_session)
    res = client.put(URL, json={"field_settings": {
        "sales": {"discount": False},
        "expenses": {"receipt_url": False},
        "product": {"price": False, "min_stock": True},
        "customer": {"email": False, "contact_information.email_address": True},
    }})
    assert res.status_code == 200, res.text
    eff = res.json()["field_settings"]
    assert eff["sales_order"]["discount"] is False and eff["expense"]["receipt_url"] is False
    assert eff["product"]["pricing.selling_price"] is False and eff["product"]["minimum_stock_level"] is True
    assert eff["customer"]["contact_information.email_address"] is True  # canonical key wins


@pytest.mark.parametrize("body, bad_loc", [
    ({"field_settings": {"warehouse": {"x": True}}}, ["warehouse"]),
    ({"field_settings": {"lead": {"favourite_colour": False}}}, ["lead", "favourite_colour"]),
    ({"field_settings": {"lead": {"lead_status": False}}}, ["lead", "lead_status"]),
    ({"field_settings": {"lead": {"name": False}}}, ["lead", "name"]),
    ({"field_settings": {"customer": {"name": False}}}, ["customer", "name"]),
    ({"field_settings": {"quotation": {"customer_id": False}}}, ["quotation", "customer_id"]),
    ({"field_settings": {"expense": {"amount": False}}}, ["expense", "amount"]),
    ({"field_settings": {"sales_order": {"delivery_address": False}}}, ["sales_order", "delivery_address"]),
    ({"field_settings": {"lead": {"email": "yes"}}}, ["lead", "email"]),
    ({"field_settings": {"lead": {"email": "false"}}}, ["lead", "email"]),
    ({"field_settings": {"lead": {"email": 1}}}, ["lead", "email"]),
    ({"field_settings": {"lead": {"email": 0}}}, ["lead", "email"]),
    ({"field_settings": {"lead": {"email": None}}}, ["lead", "email"]),
])
def test_put_rejects_invalid_input_and_saves_nothing(db_session, body, bad_loc):
    org, client = _org(db_session, {"lead": {"notes": False}})
    body["field_settings"].setdefault("supplier", {})["notes"] = False  # a valid change alongside
    res = client.put(URL, json=body)
    assert res.status_code == 422
    assert any(err["loc"][2:] == bad_loc for err in res.json()["detail"]), res.json()
    assert _stored(db_session, org) == {"lead": {"notes": False}}


@pytest.mark.parametrize("body", [
    {}, {"field_settings": None}, {"field_settings": []}, {"field_settings": {"lead": ["email"]}},
    {"field_settings": {"lead": True}},
])
def test_put_rejects_malformed_payloads(db_session, body):
    _, client = _org(db_session)
    assert client.put(URL, json=body).status_code == 422


def test_locked_fields_sent_as_true_are_accepted_and_not_stored(db_session):
    org, client = _org(db_session)
    res = client.put(URL, json={"field_settings": {"lead": {"name": True}, "quotation": {"lead_id": True}}})
    assert res.status_code == 200
    assert _stored(db_session, org) == {}


# ------------------------------------------------------------------ isolation and access


def test_organizations_are_isolated(db_session):
    org_a, client_a = _org(db_session)
    org_b, client_b = _org(db_session, {"lead": {"email": False}})
    res = client_a.put(URL, json={"field_settings": {"lead": {"notes": False}},
                                  "organization_id": org_b.id})
    assert res.status_code == 200
    assert _stored(db_session, org_b) == {"lead": {"email": False}}
    assert _stored(db_session, org_a) == {"lead": {"notes": False}}
    assert client_a.get(URL).json()["field_settings"]["lead"]["email"] is True
    assert client_b.get(URL).json()["field_settings"]["lead"]["notes"] is True


def _staff(db: Session, org: Organization) -> TestClient:
    staff = User(id=str(uuid.uuid4()), organization_id=org.id, email=f"s_{uuid.uuid4().hex[:8]}@x.com", name="Staff",
                 password_hash="h", role="sales_officer", system_role="staff", is_active=True)
    db.add(staff)
    db.commit()
    client = TestClient(app)
    client.headers = {"Authorization": f"Bearer {create_access_token(staff.id, role='sales_officer', organization_id=org.id)}"}
    return client


def test_staff_read_their_own_firm_and_cannot_write(db_session):
    org, admin = _org(db_session, {"lead": {"email": False}})
    other, _ = _org(db_session, {"lead": {"notes": False}})
    staff = _staff(db_session, org)

    res = staff.get(URL, params={"organization_id": other.id})  # a caller-supplied id is ignored
    assert res.status_code == 200
    assert res.json() == admin.get(URL).json()
    assert res.json()["field_settings"]["lead"]["email"] is False and res.json()["field_settings"]["lead"]["notes"] is True

    assert staff.put(URL, json={"field_settings": {"lead": {"email": True}}}).status_code == 403
    assert staff.put(URL, json={"field_settings": {"lead": {"notes": False}}, "organization_id": other.id}).status_code == 403
    anon = TestClient(app)
    assert anon.get(URL).status_code in (401, 403)
    assert anon.put(URL, json={"field_settings": {"lead": {"email": True}}}).status_code in (401, 403)
    assert _stored(db_session, org) == {"lead": {"email": False}}
    assert _stored(db_session, other) == {"lead": {"notes": False}}


def test_admin_reads_and_writes(db_session):
    org, admin = _org(db_session)
    assert admin.get(URL).status_code == 200
    assert admin.put(URL, json={"field_settings": {"lead": {"email": False}}}).status_code == 200
    assert _stored(db_session, org) == {"lead": {"email": False}}


# ------------------------------------------------------------------ category -> subcategory


def _product(eff):
    return eff["product"]["category_id"], eff["product"]["sub_category_id"]


def test_hiding_category_hides_subcategory_and_restores_its_own_preference(db_session):
    org, client = _org(db_session)
    assert _product(client.get(URL).json()["field_settings"]) == (True, True)

    # Category OFF: Subcategory follows, without anything saved for it
    eff = client.put(URL, json={"field_settings": {"product": {"category_id": False}}}).json()["field_settings"]
    assert _product(eff) == (False, False)
    assert _stored(db_session, org) == {"product": {"category_id": False}}

    # Category back ON: Subcategory had no preference, so it is shown again
    eff = client.put(URL, json={"field_settings": {"product": {"category_id": True}}}).json()["field_settings"]
    assert _product(eff) == (True, True)

    # A saved Subcategory OFF survives Category going OFF and ON again
    client.put(URL, json={"field_settings": {"product": {"sub_category_id": False}}})
    client.put(URL, json={"field_settings": {"product": {"category_id": False}}})
    eff = client.put(URL, json={"field_settings": {"product": {"category_id": True}}}).json()["field_settings"]
    assert _product(eff) == (True, False)


def test_subcategory_cannot_be_shown_without_category(db_session):
    org, client = _org(db_session, {"product": {"category_id": False}, "lead": {"notes": False}})
    for body in ({"product": {"sub_category_id": True}},
                 {"product": {"category_id": False, "sub_category_id": True}},
                 {"product": {"sub_category": True}}):  # legacy key, same field
        res = client.put(URL, json={"field_settings": body})
        assert res.status_code == 422, body
        assert res.json()["detail"][0]["loc"] == ["body", "field_settings", "product", "sub_category_id"]
    assert _stored(db_session, org) == {"product": {"category_id": False}, "lead": {"notes": False}}

    # Showing both together is fine, and unrelated preferences stay put
    res = client.put(URL, json={"field_settings": {"product": {"category_id": True, "sub_category_id": True}}})
    assert res.status_code == 200 and _product(res.json()["field_settings"]) == (True, True)
    assert res.json()["field_settings"]["lead"]["notes"] is False


def test_impossible_saved_combinations_resolve_safely():
    for raw in ({"product": {"category_id": False, "sub_category_id": True}},
                {"product": {"category_id": False, "sub_category": True}},
                {"product": {"category_id": False}}):
        assert _product(fs.effective_settings(raw)) == (False, False)
    assert _product(fs.effective_settings({"product": {"sub_category_id": False}})) == (True, False)


# ------------------------------------------------------------------ variants


def test_variant_fields_cannot_be_configured(db_session):
    org, client = _org(db_session, {"product": {"has_variants": False}})
    for key in ("has_variants", "variant_attributes", "variations"):
        res = client.put(URL, json={"field_settings": {"product": {key: False}}})
        assert res.status_code == 422 and "non_configurable" in res.json()["detail"][0]["msg"], key
        assert client.put(URL, json={"field_settings": {"product": {key: True}}}).status_code == 200
    eff = client.get(URL).json()["field_settings"]["product"]
    assert eff["has_variants"] and eff["variant_attributes"] and eff["variations"]
    assert _stored(db_session, org) == {"product": {"has_variants": False}}  # kept, but has no effect


def test_settings_changes_leave_variants_and_stock_alone(db_session, monkeypatch):
    from app.models import Product, ProductVariant, StockMovement

    monkeypatch.setattr(entitlement_service, "check_entitlement", lambda *a, **k: None)
    org, client = _org(db_session)
    res = client.post("/products", json={"name": "Batten", "total_inventory": 7,
                                         "variations": [{"name": "Small"}, {"name": "Large"}]})
    assert res.status_code == 201, res.text
    product_id = res.json()["id"]

    def snapshot():
        db_session.expire_all()
        product = db_session.get(Product, product_id)
        variants = db_session.query(ProductVariant).filter(ProductVariant.product_id == product_id).all()
        moves = db_session.query(StockMovement).filter(StockMovement.product_id == product_id).count()
        return product.total_inventory, product.has_variants, sorted((v.id, v.name, v.inventory) for v in variants), moves

    before = snapshot()
    assert client.put(URL, json={"field_settings": {"product": {"category_id": False, "brand_id": False,
                                                                "opening_stock": False}}}).status_code == 200
    assert snapshot() == before

    # An edit that leaves variants out (as a form with hidden fields would) keeps them
    assert client.patch(f"/products/{product_id}", json={"description": "Edited"}).status_code == 200
    assert snapshot() == before
    assert len(before[2]) == 2


# ------------------------------------------------------------------ canonical fields and aliases


def test_consolidated_legacy_keys(db_session):
    saved = {"company": {"address": False, "branch_address": False, "gstin_pan": False},
             "product": {"brand": False, "sub_category": False, "vendor": False}}
    org, client = _org(db_session, copy.deepcopy(saved))
    eff = client.get(URL).json()["field_settings"]
    assert eff["company"]["registered_address"] is False and eff["company"]["branch_addresses"] is False
    assert eff["company"]["gstin_pan"] is False and eff["company"]["gst_number"] is True  # not merged
    assert eff["product"]["brand_id"] is False and eff["product"]["sub_category_id"] is False
    assert eff["product"]["vendor"] is False and eff["product"]["preferred_supplier_id"] is True  # not merged

    # Canonical keys win over the legacy ones, and legacy keys still work on PUT
    assert fs.effective_settings({"product": {"brand": False, "brand_id": True}})["product"]["brand_id"] is True
    res = client.put(URL, json={"field_settings": {"company": {"address": True}, "product": {"brand": True}}})
    assert res.status_code == 200
    assert res.json()["field_settings"]["company"]["registered_address"] is True
    assert res.json()["field_settings"]["product"]["brand_id"] is True
    stored = _stored(db_session, org)
    assert stored["company"]["address"] is False and stored["company"]["registered_address"] is True  # old key kept


# ------------------------------------------------------------------ business behaviour unchanged


def test_hiding_fields_keeps_record_data(db_session):
    org, client = _org(db_session)
    customer = Customer(organization_id=org.id, name="Kept", email="kept@x.com", phone="9000000000",
                        customer_id=f"CS-F-{uuid.uuid4().hex[:6]}", opening_balance=500.0)
    lead = Lead(organization_id=org.id, name="Lead", mobile_number="9111111111", lead_source="walk_in",
                email="lead@x.com", lead_id=f"L-F-{uuid.uuid4().hex[:6]}")
    db_session.add_all([customer, lead])
    db_session.commit()
    res = client.put(URL, json={"field_settings": {
        "customer": {"contact_information.email_address": False, "payment_information.opening_balance": False},
        "lead": {"email": False},
    }})
    assert res.status_code == 200
    db_session.expire_all()
    customer, lead = db_session.get(Customer, customer.id), db_session.get(Lead, lead.id)
    assert (customer.email, customer.phone, customer.opening_balance) == ("kept@x.com", "9000000000", 500.0)
    assert lead.email == "lead@x.com"


def test_business_rules_still_apply_when_fields_are_hidden(db_session, monkeypatch):
    from fastapi import HTTPException

    from app.models import Product
    from app.services import expense_service, quotation_service

    monkeypatch.setattr(entitlement_service, "check_entitlement", lambda *a, **k: None)
    org, client = _org(db_session)
    assert client.put(URL, json={"field_settings": {
        "sales_order": {"shipping_address": False, "discount": False, "tax": False},
        "expense": {"items": False, "tax_amount": False},
        "quotation": {"billing_address": False},
    }}).status_code == 200

    # Quotation: exactly one party
    for customer_id, lead_id in ((None, None), ("c", "l")):
        with pytest.raises(HTTPException):
            quotation_service._validate_party_for_create(customer_id, lead_id)

    # Expense: an amount is needed without items; with items it is calculated as before
    with pytest.raises(HTTPException):
        expense_service.calculate_totals(None, header_amount=0)
    assert expense_service.calculate_totals(None, header_tax_rate=18, header_amount=118.0)[:3] == (100.0, 18.0, 118.0)

    customer = Customer(organization_id=org.id, name="No Address", customer_id=f"CS-F-{uuid.uuid4().hex[:6]}")
    product = Product(organization_id=org.id, name="Widget", price=100.0, tax_rate=0.0)
    db_session.add_all([customer, product])
    db_session.flush()
    from app.services import stock_service

    warehouse = stock_service.default_warehouse(db_session, org.id)
    stock_service.adjust_on_hand(db_session, org.id, warehouse.id, product.id, None, 10, "adjustment")
    db_session.commit()
    line = [{"product_id": product.id, "quantity": 1}]

    # Sales order: home delivery still needs an address
    res = client.post("/orders", json={"customer_id": customer.id, "items": line, "delivery_method": "home_delivery"})
    assert res.status_code == 400 and "delivery_address" in res.json()["detail"]

    # Sales order: a partial payment still needs 0 < paid_amount < total (schema, then router)
    res = client.post("/orders", json={"customer_id": customer.id, "items": line,
                                       "payment_status": "partial", "paid_amount": 0})
    assert res.status_code == 422 and "partial" in res.text
    res = client.post("/orders", json={"customer_id": customer.id, "items": line,
                                       "payment_status": "partial", "paid_amount": 100.0})
    assert res.status_code == 400 and "partial" in res.json()["detail"], res.text
