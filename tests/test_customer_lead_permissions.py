"""Who may create a customer and convert a lead, proven end to end.

For every role and permission state this sends a VALID request and then checks the
database, so a 403 is known to have created nothing. It also checks that an
unauthorized role gets 403 before any body validation or record lookup (an empty
body, or a lead id that does not exist).

    python -m pytest tests/test_customer_lead_permissions.py -s   # -s prints the table
"""

import os
import sys
import uuid

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi.testclient import TestClient  # noqa: E402

from app.core.database import Base, SessionLocal, engine  # noqa: E402
from app.core.security import create_access_token, hash_password  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Customer, Lead, Organization, Role, User  # noqa: E402
from app.models.enums import UserRole  # noqa: E402
from app.services import entitlement_service, role_service  # noqa: E402

client = TestClient(app)
DENY_CUSTOMER = "You don't have permission to create customers"
TABLE: list[tuple] = []


@pytest.fixture(scope="module", autouse=True)
def _schema():
    Base.metadata.create_all(bind=engine)


@pytest.fixture(autouse=True)
def _no_plan_gates(monkeypatch):
    # Plan entitlements are not what is under test here.
    monkeypatch.setattr(entitlement_service, "check_entitlement", lambda *a, **k: None)
    monkeypatch.setattr(entitlement_service, "check_limit", lambda *a, **k: None)
    monkeypatch.setattr(entitlement_service, "has_entitlement", lambda *a, **k: True)


def _auth(user: User) -> dict:
    return {"Authorization": f"Bearer {create_access_token(user.id, user.system_role, user.organization_id)}"}


@pytest.fixture
def org():
    """An org with its seeded default roles, plus a custom no-permission "Staff" role."""
    db = SessionLocal()
    try:
        o = Organization(name=f"Perm {uuid.uuid4().hex[:6]}", company_code=f"CMP-{uuid.uuid4().int % 90000 + 10000}")
        db.add(o)
        db.flush()
        role_service.seed_default_roles(db, o.id)
        staff_role = Role(organization_id=o.id, name="Staff", permissions={"dashboard": {"view": True}})
        db.add(staff_role)
        db.flush()
        roles = {r.name: r for r in db.query(Role).filter(Role.organization_id == o.id)}
        users = {}
        for key, system_role, legacy, role_name in (
            ("admin", "admin", UserRole.ADMIN, None),
            ("sales_officer", "staff", UserRole.SALES_OFFICER, "Sales Officer"),
            ("accountant", "staff", UserRole.ACCOUNTANT, "Accountant"),
            ("delivery_partner", "staff", UserRole.DELIVERY_PARTNER, "Delivery Partner"),
            ("staff", "staff", None, "Staff"),
        ):
            u = User(email=f"{key}.{uuid.uuid4().hex[:8]}@perm.test", name=f"{key} user", organization_id=o.id,
                     system_role=system_role, role=legacy, role_id=roles[role_name].id if role_name else None,
                     password_hash=hash_password("Password123!"), is_active=True)
            db.add(u)
            db.flush()
            users[key] = u
        db.commit()
        yield {"id": o.id, "roles": {k: v.id for k, v in roles.items()},
               "users": {k: (u.id, _auth(u)) for k, u in users.items()}}
    finally:
        db.close()


def _grant(role_id: str, *, create_customer: bool, edit_leads: bool) -> None:
    db = SessionLocal()
    try:
        role = db.get(Role, role_id)
        perms = {k: dict(v) for k, v in (role.permissions or {}).items()}
        perms.setdefault("customers", {})["view"] = True
        perms["customers"]["create"] = create_customer
        perms.setdefault("leads", {}).update(view=edit_leads, edit=edit_leads)
        role.permissions = perms
        db.commit()
    finally:
        db.close()


def _lead_for(org_id: str, user_id: str) -> str:
    db = SessionLocal()
    try:
        lead = Lead(organization_id=org_id, lead_id=f"L-{uuid.uuid4().hex[:10]}", name="Prospect",
                    mobile_number="9" + str(uuid.uuid4().int)[:9], lead_source="Website Enquiry",
                    lead_status="new", assigned_salesperson_id=user_id)
        db.add(lead)
        db.commit()
        return lead.id
    finally:
        db.close()


def _customers_named(name: str) -> int:
    db = SessionLocal()
    try:
        return db.query(Customer).filter(Customer.name == name).count()
    finally:
        db.close()


def _lead_customer(lead_id: str) -> str | None:
    db = SessionLocal()
    try:
        return db.get(Lead, lead_id).customer_id
    finally:
        db.close()


def _try(org, role_key: str, state: str) -> dict:
    user_id, headers = org["users"][role_key]
    name = f"Cust {uuid.uuid4().hex[:8]}"
    c = client.post("/customers", json={"name": name}, headers=headers)
    lead_id = _lead_for(org["id"], user_id)
    lc = client.post(f"/leads/{lead_id}/convert-to-customer", json={"business_name": name + " Co"}, headers=headers)
    out = {
        "create": (c.status_code, c.json().get("detail") if c.status_code >= 400 else "created"),
        "convert": (lc.status_code, lc.json().get("detail") if lc.status_code >= 400 else "converted"),
        "customer_rows": _customers_named(name),
        "lead_linked": _lead_customer(lead_id) is not None,
    }
    TABLE.append((role_key, state, *out["create"], *out["convert"]))
    return out


def _assert_denied(out):
    assert out["create"][0] == 403 and out["customer_rows"] == 0, out
    assert out["convert"][0] == 403 and not out["lead_linked"], out


def _assert_allowed(out):
    assert out["create"][0] == 201 and out["customer_rows"] == 1, out
    assert out["convert"][0] == 200 and out["lead_linked"], out


def test_role_matrix_default_permissions(org):
    _assert_allowed(_try(org, "admin", "always (full access)"))
    _assert_allowed(_try(org, "sales_officer", "default"))
    _assert_denied(_try(org, "accountant", "default"))
    _assert_denied(_try(org, "delivery_partner", "default"))
    _assert_denied(_try(org, "staff", "default (no permissions)"))


def test_role_matrix_granted_then_revoked(org):
    for role_key, role_name in (("delivery_partner", "Delivery Partner"), ("accountant", "Accountant"),
                                ("staff", "Staff")):
        _grant(org["roles"][role_name], create_customer=True, edit_leads=True)
        _assert_allowed(_try(org, role_key, "customers.create + leads.edit granted"))
        _grant(org["roles"][role_name], create_customer=False, edit_leads=False)
        _assert_denied(_try(org, role_key, "both revoked"))
    _grant(org["roles"]["Sales Officer"], create_customer=False, edit_leads=False)
    _assert_denied(_try(org, "sales_officer", "both revoked"))


def test_delivery_partner_with_leads_edit_but_no_customer_create_cannot_convert(org):
    """Converting creates a customer, so a Delivery Partner also needs customers.create."""
    _grant(org["roles"]["Delivery Partner"], create_customer=False, edit_leads=True)
    out = _try(org, "delivery_partner", "leads.edit only")
    assert out["create"][0] == 403 and out["customer_rows"] == 0
    assert out["convert"] == (403, DENY_CUSTOMER) and not out["lead_linked"]
    # The 403 comes before the lead lookup: an unknown lead id is 403, not 404.
    _, headers = org["users"]["delivery_partner"]
    r = client.post(f"/leads/{uuid.uuid4()}/convert-to-customer", json={}, headers=headers)
    assert (r.status_code, r.json()["detail"]) == (403, DENY_CUSTOMER)


@pytest.mark.parametrize("role_key", ["accountant", "delivery_partner", "staff"])
def test_unauthorized_role_gets_403_before_validation_or_lookup(org, role_key):
    _, headers = org["users"][role_key]
    for body in (None, {}):  # no body at all, and an empty JSON object
        r = client.post("/customers", json=body, headers=headers)
        assert r.status_code == 403, (body, r.text)
    r = client.post(f"/leads/{uuid.uuid4()}/convert-to-customer", headers=headers)
    assert r.status_code == 403, r.text
    # An authorized role with the same empty body gets the validation error instead.
    _, admin = org["users"]["admin"]
    assert client.post("/customers", json={}, headers=admin).status_code == 422
    assert client.post(f"/leads/{uuid.uuid4()}/convert-to-customer", headers=admin).status_code == 404


def test_print_table():
    print("\nrole | permission state | create customer | convert lead")
    for role, state, cs, cm, ls, lm in TABLE:
        print(f"{role} | {state} | {cs} {cm} | {ls} {lm}")


def test_malformed_json_is_rejected_before_auth_and_creates_nothing(org):
    """FastAPI parses the JSON body before any dependency runs, so a body that is not
    valid JSON is a 422 even for a role without permission. Nothing is created and
    nothing about the firm is revealed; a well-formed body (even empty) gets the 403."""
    _, headers = org["users"]["delivery_partner"]
    before = _customers_named("{broken")
    r = client.post("/customers", content=b"{broken", headers={**headers, "Content-Type": "application/json"})
    assert r.status_code == 422 and _customers_named("{broken") == before
