"""What the DEFAULT Accountant role can read, endpoint by endpoint (report only: these
tests pin today's behaviour so a later permission decision is a visible change)."""

import os
import sys
import uuid

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi.testclient import TestClient  # noqa: E402

from app.core.database import Base, SessionLocal, engine  # noqa: E402
from app.core.permissions import default_role_matrices  # noqa: E402
from app.core.security import create_access_token, hash_password  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Organization, Role, User  # noqa: E402
from app.models.enums import UserRole  # noqa: E402
from app.services import entitlement_service, role_service  # noqa: E402

client = TestClient(app)

# endpoint -> (module, action) it requires; None = not a permission: Admin system role only
REQUIRED = {
    "/supplier-invoices": ("supplier_invoices", "view"),
    "/accounts-payable": ("accounts_payable", "view"),
    "/supplier-payments": ("supplier_payments", "view"),
    "/delivery-day-ends": ("deliveries", "view"),
    "/brands": ("products", "view"),
    "/categories": ("products", "view"),
    "/users": None,
}


@pytest.fixture(scope="module", autouse=True)
def _schema():
    Base.metadata.create_all(bind=engine)


@pytest.fixture(autouse=True)
def _no_plan_gates(monkeypatch):
    monkeypatch.setattr(entitlement_service, "check_entitlement", lambda *a, **k: None)
    monkeypatch.setattr(entitlement_service, "check_limit", lambda *a, **k: None)
    monkeypatch.setattr(entitlement_service, "has_entitlement", lambda *a, **k: True)


@pytest.fixture(scope="module")
def tokens():
    db = SessionLocal()
    try:
        org = Organization(name=f"Acct {uuid.uuid4().hex[:6]}", company_code=f"CMP-{uuid.uuid4().int % 90000 + 10000}")
        db.add(org)
        db.flush()
        role_service.seed_default_roles(db, org.id)
        acct_role = db.query(Role).filter(Role.organization_id == org.id, Role.name == "Accountant").one()
        out = {}
        for key, system_role, legacy, role_id in (("admin", "admin", UserRole.ADMIN, None),
                                                  ("accountant", "staff", UserRole.ACCOUNTANT, acct_role.id)):
            u = User(email=f"{key}.{uuid.uuid4().hex[:8]}@example.com", name=key, organization_id=org.id,
                     system_role=system_role, role=legacy, role_id=role_id,
                     password_hash=hash_password("Password123!"), is_active=True)
            db.add(u)
            db.flush()
            out[key] = {"Authorization": f"Bearer {create_access_token(u.id, u.system_role, u.organization_id)}"}
        db.commit()
        return out
    finally:
        db.close()


def _default_has(required) -> bool:
    if required is None:
        return False
    module, action = required
    return bool(default_role_matrices()["Accountant"].get(module, {}).get(action, False))


@pytest.mark.parametrize("path", list(REQUIRED))
def test_default_accountant_access_matches_the_permission_table(tokens, path):
    assert client.get(path, headers=tokens["admin"]).status_code == 200
    r = client.get(path, headers=tokens["accountant"])
    expected = 200 if _default_has(REQUIRED[path]) else 403
    assert r.status_code == expected, (path, r.status_code, r.text)
    print(f"{path} | {REQUIRED[path]} | default accountant: {'yes' if expected == 200 else 'no'} -> {r.status_code} "
          f"{r.json().get('detail', '') if r.status_code != 200 else ''}")


def test_none_of_the_seven_is_granted_by_default_today():
    assert not any(_default_has(req) for req in REQUIRED.values())
    # what the default Accountant does have, for comparison
    granted = default_role_matrices()["Accountant"]
    assert granted["purchases"]["view"] and granted["grn"]["view"] and granted["suppliers"]["view"]
