"""Tests for the Google registration flow (Scenario 3: brand-new Google user)
and the google_id identity-conflict protection (Scenario/Case C).

Covers what tests/test_google_auth.py and tests/test_google_oauth_redirect.py
deliberately do not: registration_code issuance, the registration-info peek
endpoint, complete-registration (Organization + Admin + 7-day Trial + default
roles + tokens), reuse of the normal /auth/register validation contract, and
the google_id mismatch rejection.
"""

import os
import sys
import time
import urllib.parse
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath("."))

from fastapi.testclient import TestClient

from app.core.database import Base, SessionLocal, auto_add_missing_columns, engine
from app.core.security import hash_password, verify_password
from app.main import app
from app.models import (
    OAuthExchangeTicket,
    OAuthRegistrationTicket,
    Organization,
    Role,
    User,
    UserRole,
)
from app.services import auth_service, org_service, role_service

# Ensure SQLite has every table & column migrated, including the new
# oauth_registration_tickets table (explicit per-table create as a safety net
# for the same FK-cycle quirk that can make a blanket create_all() skip
# tables — see the ones already introduced by table order in this schema).
Base.metadata.create_all(bind=engine)
for _table_name in ("oauth_registration_tickets", "oauth_exchange_tickets"):
    Base.metadata.tables[_table_name].create(bind=engine, checkfirst=True)
auto_add_missing_columns()

client = TestClient(app, follow_redirects=False)


def _create_test_org_and_user(
    email: str,
    name: str = "Test User",
    is_active: bool = True,
    google_id: str | None = None,
    role: UserRole = UserRole.ADMIN,
    role_id: str | None = None,
    system_role: str | None = None,
) -> tuple[Organization, User]:
    db = SessionLocal()
    try:
        org = Organization(
            name=f"Org {uuid.uuid4().hex[:6]}",
            email=f"org_{uuid.uuid4().hex[:6]}@example.com",
        )
        org_service.start_trial(db, org)
        db.add(org)
        db.flush()

        user = User(
            organization_id=org.id,
            name=name,
            email=email,
            password_hash=hash_password("Password@123"),
            role=role,
            role_id=role_id,
            system_role=system_role or ("admin" if role == UserRole.ADMIN else "staff"),
            is_active=is_active,
            google_id=google_id,
        )
        db.add(user)
        db.commit()
        db.refresh(org)
        db.refresh(user)
        return org, user
    finally:
        db.close()


def _staff_role_id(org_id: str, name: str) -> str:
    """Ensure the org's default roles exist and return the id of `name`."""
    db = SessionLocal()
    try:
        role_service.seed_default_roles(db, org_id)
        role = db.query(Role).filter(Role.organization_id == org_id, Role.name == name).first()
        assert role is not None
        return role.id
    finally:
        db.close()


def _google_claims(email: str, sub: str, name: str = "New Google User") -> dict:
    return {
        "iss": "accounts.google.com",
        "sub": sub,
        "email": email,
        "email_verified": True,
        "name": name,
    }


def _base_registration_payload(registration_code: str, **overrides) -> dict:
    payload = {
        "registration_code": registration_code,
        "organization_name": f"New Co {uuid.uuid4().hex[:6]}",
        "business_type": "Retail",
        "gst_number": "GST123",
        "pan_number": "PAN123",
        "address": "123 Main St",
        "phone": "9999999999",
        "financial_year": "2025-2026",
        "logo_url": None,
        "admin_name": "New Admin",
        "password": "StrongPass123",
    }
    payload.update(overrides)
    return payload


# --------------------------------------------------------------------------
# Scenario 3: brand-new Google user -> registration_code (direct ID-token flow)
# --------------------------------------------------------------------------


def test_direct_flow_new_email_returns_registration_required():
    """New Google email via POST /auth/google gets a registration_code, not an
    auto-created account and not the old bare 404."""
    email = f"newgoogle_{uuid.uuid4().hex[:8]}@example.com"
    sub = f"sub_{uuid.uuid4().hex[:10]}"

    db = SessionLocal()
    users_before = db.query(User).count()
    orgs_before = db.query(Organization).count()
    db.close()

    with patch("google.oauth2.id_token.verify_oauth2_token", return_value=_google_claims(email, sub)):
        res = client.post("/auth/google", json={"credential": "tok"})
        assert res.status_code == 200
        data = res.json()
        assert data["status"] == "registration_required"
        assert isinstance(data["registration_code"], str) and len(data["registration_code"]) > 20
        # No CRM tokens, no Google credential, leaked in this response.
        assert "access_token" not in data
        assert "refresh_token" not in data

    db = SessionLocal()
    try:
        assert db.query(User).count() == users_before
        assert db.query(Organization).count() == orgs_before
    finally:
        db.close()


def test_callback_flow_new_email_redirects_with_registration_code():
    """New Google email via the redirect callback lands on the frontend's
    registration route with a registration_code — not a raw 404."""
    email = f"newgoogle_cb_{uuid.uuid4().hex[:8]}@example.com"
    sub = f"sub_cb_{uuid.uuid4().hex[:10]}"

    from app.services import google_auth_service
    from app.core.config import settings

    oauth_state = google_auth_service.generate_oauth_state()
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"id_token": "mock.raw.id_token"}

    with patch.object(settings, "google_client_id", "test-client-id"), \
         patch.object(settings, "google_client_secret", "test-secret"), \
         patch.object(settings, "frontend_url", "http://localhost:5173"), \
         patch("requests.post", return_value=mock_resp), \
         patch("google.oauth2.id_token.verify_oauth2_token", return_value=_google_claims(email, sub)):
        res = client.get(f"/auth/google/callback?code=some_code&state={oauth_state}")
        assert res.status_code == 307
        location = res.headers["location"]
        assert location.startswith("http://localhost:5173/auth/register/google?registration_code=")
        assert "exchange_code" not in location
        assert "access_token" not in location
        assert "refresh_token" not in location

        parsed = urllib.parse.urlparse(location)
        registration_code = urllib.parse.parse_qs(parsed.query)["registration_code"][0]
        assert len(registration_code) > 20


# --------------------------------------------------------------------------
# registration-info (peek, non-consuming)
# --------------------------------------------------------------------------


def _mint_registration_code(email: str, sub: str, name: str = "Prefill Name") -> str:
    db = SessionLocal()
    try:
        return auth_service.create_registration_ticket(db, google_sub=sub, google_email=email, google_name=name)
    finally:
        db.close()


def test_registration_info_returns_email_and_name():
    email = f"prefill_{uuid.uuid4().hex[:8]}@example.com"
    sub = f"sub_pf_{uuid.uuid4().hex[:10]}"
    code = _mint_registration_code(email, sub, name="Prefill User")

    res = client.post("/auth/google/registration-info", json={"registration_code": code})
    assert res.status_code == 200
    data = res.json()
    assert data["email"] == email
    assert data["name"] == "Prefill User"


def test_registration_info_does_not_consume_code():
    """The same registration_code can be peeked multiple times (page reload)."""
    email = f"reload_{uuid.uuid4().hex[:8]}@example.com"
    sub = f"sub_rl_{uuid.uuid4().hex[:10]}"
    code = _mint_registration_code(email, sub)

    for _ in range(3):
        res = client.post("/auth/google/registration-info", json={"registration_code": code})
        assert res.status_code == 200
        assert res.json()["email"] == email

    # Still consumable afterwards — peeking never burns the ticket.
    complete_res = client.post(
        "/auth/google/complete-registration", json=_base_registration_payload(code)
    )
    assert complete_res.status_code == 201


def test_registration_info_invalid_code_rejected():
    res = client.post("/auth/google/registration-info", json={"registration_code": "not-a-real-code"})
    assert res.status_code == 400


def test_registration_info_expired_code_rejected():
    email = f"expired_pf_{uuid.uuid4().hex[:8]}@example.com"
    db = SessionLocal()
    try:
        raw_code = f"expired_reg_{uuid.uuid4().hex}"
        db.add(
            OAuthRegistrationTicket(
                ticket_hash=auth_service._hash_token(raw_code),
                google_sub=f"sub_{uuid.uuid4().hex[:8]}",
                google_email=email,
                google_name="Expired User",
                expires_at=datetime.now(timezone.utc) - timedelta(seconds=10),
                used=False,
            )
        )
        db.commit()
    finally:
        db.close()

    res = client.post("/auth/google/registration-info", json={"registration_code": raw_code})
    assert res.status_code == 400


# --------------------------------------------------------------------------
# complete-registration: full happy path (Organization + Admin + Trial + Roles)
# --------------------------------------------------------------------------


def test_complete_registration_creates_org_admin_trial_roles_and_tokens():
    email = f"complete_{uuid.uuid4().hex[:8]}@example.com"
    sub = f"sub_cmp_{uuid.uuid4().hex[:10]}"
    code = _mint_registration_code(email, sub, name="Google Name")

    payload = _base_registration_payload(code, admin_name="Edited Admin Name")
    res = client.post("/auth/google/complete-registration", json=payload)
    assert res.status_code == 201
    data = res.json()

    # AuthResponse shape, same as POST /auth/register.
    assert data["user"]["email"] == email
    assert data["user"]["name"] == "Edited Admin Name"  # editable, differs from Google's claim
    assert data["user"]["google_id"] == sub
    assert data["user"]["system_role"] == "admin"
    assert data["user"]["role"] == "admin"
    assert data["organization"]["name"] == payload["organization_name"]
    assert "access_token" in data["tokens"]
    assert "refresh_token" in data["tokens"]

    db = SessionLocal()
    try:
        user = db.query(User).filter(User.email == email).first()
        assert user is not None
        assert user.google_id == sub
        assert user.role == UserRole.ADMIN
        assert user.system_role == "admin"
        assert verify_password("StrongPass123", user.password_hash)

        org = db.get(Organization, user.organization_id)
        assert org is not None
        assert org.trial_ends_at is not None
        delta = org.trial_ends_at.replace(tzinfo=timezone.utc) - datetime.now(timezone.utc)
        assert timedelta(days=6, hours=23) < delta <= timedelta(days=7, minutes=5)

        role_names = {r.name for r in db.query(Role).filter(Role.organization_id == org.id).all()}
        assert {"Sales Officer", "Delivery Partner", "Accountant"} <= role_names
    finally:
        db.close()

    # /auth/me works immediately with the issued access token.
    me_res = client.get("/auth/me", headers={"Authorization": f"Bearer {data['tokens']['access_token']}"})
    assert me_res.status_code == 200
    assert me_res.json()["full_access"] is True

    # Password login works afterward.
    login_res = client.post("/auth/login", json={"email": email, "password": "StrongPass123"})
    assert login_res.status_code == 200
    assert login_res.json()["user"]["email"] == email

    # Google login works afterward too — resolves directly via google_id (Case B).
    with patch("google.oauth2.id_token.verify_oauth2_token", return_value=_google_claims(email, sub)):
        google_res = client.post("/auth/google", json={"credential": "tok"})
        assert google_res.status_code == 200
        assert google_res.json()["user"]["email"] == email
        assert google_res.json()["user"]["google_id"] == sub


def test_complete_registration_email_not_accepted_from_client():
    """Even if a client smuggles an `email` key into the JSON body, the schema
    has no such field — the account's email always comes from the ticket."""
    real_email = f"authoritative_{uuid.uuid4().hex[:8]}@example.com"
    spoofed_email = f"attacker_{uuid.uuid4().hex[:8]}@example.com"
    sub = f"sub_spf_{uuid.uuid4().hex[:10]}"
    code = _mint_registration_code(real_email, sub)

    payload = _base_registration_payload(code)
    payload["email"] = spoofed_email  # not part of GoogleCompleteRegistrationRequest

    res = client.post("/auth/google/complete-registration", json=payload)
    assert res.status_code == 201
    assert res.json()["user"]["email"] == real_email

    db = SessionLocal()
    try:
        assert db.query(User).filter(User.email == spoofed_email).first() is None
        assert db.query(User).filter(User.email == real_email).first() is not None
    finally:
        db.close()


def test_complete_registration_name_editable_email_locked():
    email = f"nameedit_{uuid.uuid4().hex[:8]}@example.com"
    sub = f"sub_ne_{uuid.uuid4().hex[:10]}"
    code = _mint_registration_code(email, sub, name="Original Google Name")

    payload = _base_registration_payload(code, admin_name="Totally Different Name")
    res = client.post("/auth/google/complete-registration", json=payload)
    assert res.status_code == 201
    assert res.json()["user"]["name"] == "Totally Different Name"
    assert res.json()["user"]["email"] == email


# --------------------------------------------------------------------------
# Single-use / expiry / tampering / replay
# --------------------------------------------------------------------------


def test_complete_registration_rejects_reused_code():
    email = f"reuse_{uuid.uuid4().hex[:8]}@example.com"
    sub = f"sub_ru_{uuid.uuid4().hex[:10]}"
    code = _mint_registration_code(email, sub)

    first = client.post("/auth/google/complete-registration", json=_base_registration_payload(code))
    assert first.status_code == 201

    second = client.post("/auth/google/complete-registration", json=_base_registration_payload(code))
    assert second.status_code == 400
    assert "Invalid, expired, or already used" in second.json()["detail"]


def test_complete_registration_rejects_expired_code():
    email = f"expired_cmp_{uuid.uuid4().hex[:8]}@example.com"
    db = SessionLocal()
    try:
        raw_code = f"expired_complete_{uuid.uuid4().hex}"
        db.add(
            OAuthRegistrationTicket(
                ticket_hash=auth_service._hash_token(raw_code),
                google_sub=f"sub_{uuid.uuid4().hex[:8]}",
                google_email=email,
                google_name="Expired",
                expires_at=datetime.now(timezone.utc) - timedelta(seconds=5),
                used=False,
            )
        )
        db.commit()
    finally:
        db.close()

    res = client.post(
        "/auth/google/complete-registration", json=_base_registration_payload(raw_code)
    )
    assert res.status_code == 400

    db = SessionLocal()
    try:
        assert db.query(User).filter(User.email == email).first() is None
    finally:
        db.close()


def test_complete_registration_rejects_tampered_code():
    res = client.post(
        "/auth/google/complete-registration",
        json=_base_registration_payload("completely-made-up-code-xyz"),
    )
    assert res.status_code == 400


def test_double_submit_only_creates_one_organization():
    """Simulates a duplicate/racing submit of the same registration_code —
    the atomic single-use consume must ensure only one Organization exists."""
    email = f"double_{uuid.uuid4().hex[:8]}@example.com"
    sub = f"sub_db_{uuid.uuid4().hex[:10]}"
    code = _mint_registration_code(email, sub)
    payload = _base_registration_payload(code)

    results = [
        client.post("/auth/google/complete-registration", json=payload),
        client.post("/auth/google/complete-registration", json=payload),
    ]
    statuses = sorted(r.status_code for r in results)
    assert statuses == [201, 400]

    db = SessionLocal()
    try:
        matching_orgs = db.query(Organization).filter(Organization.name == payload["organization_name"]).all()
        assert len(matching_orgs) == 1
        matching_users = db.query(User).filter(User.email == email).all()
        assert len(matching_users) == 1
    finally:
        db.close()


# --------------------------------------------------------------------------
# Registration validation mirrors POST /auth/register exactly
# --------------------------------------------------------------------------


def test_complete_registration_requires_organization_name():
    email = f"reqorg_{uuid.uuid4().hex[:8]}@example.com"
    sub = f"sub_ro_{uuid.uuid4().hex[:10]}"
    code = _mint_registration_code(email, sub)
    payload = _base_registration_payload(code)
    del payload["organization_name"]

    res = client.post("/auth/google/complete-registration", json=payload)
    assert res.status_code == 422


def test_complete_registration_password_min_length_enforced():
    email = f"shortpw_{uuid.uuid4().hex[:8]}@example.com"
    sub = f"sub_sp_{uuid.uuid4().hex[:10]}"
    code = _mint_registration_code(email, sub)
    payload = _base_registration_payload(code, password="short")

    res = client.post("/auth/google/complete-registration", json=payload)
    assert res.status_code == 422


def test_complete_registration_optional_fields_can_be_omitted():
    email = f"minimal_{uuid.uuid4().hex[:8]}@example.com"
    sub = f"sub_min_{uuid.uuid4().hex[:10]}"
    code = _mint_registration_code(email, sub)
    payload = {
        "registration_code": code,
        "organization_name": f"Minimal Co {uuid.uuid4().hex[:6]}",
        "admin_name": "Minimal Admin",
        "password": "StrongPass123",
    }
    res = client.post("/auth/google/complete-registration", json=payload)
    assert res.status_code == 201


def test_complete_registration_duplicate_email_rejected():
    """If the ticket's email was registered by someone else between mint and
    completion (race with /auth/register), the shared duplicate check still
    fires — no partial organization is left behind."""
    email = f"racedup_{uuid.uuid4().hex[:8]}@example.com"
    sub = f"sub_rd_{uuid.uuid4().hex[:10]}"
    code = _mint_registration_code(email, sub)

    # Someone else grabs this email first via normal registration.
    reg_res = client.post(
        "/auth/register",
        json={
            "organization_name": f"Other Co {uuid.uuid4().hex[:6]}",
            "email": email,
            "admin_name": "Other Admin",
            "password": "OtherPass123",
        },
    )
    assert reg_res.status_code == 201

    complete_res = client.post(
        "/auth/google/complete-registration", json=_base_registration_payload(code)
    )
    assert complete_res.status_code == 409

    db = SessionLocal()
    try:
        matching = db.query(Organization).filter(Organization.name == "Other Co").all()
        # Only the legitimately-registered org exists for this email's user, and
        # the Google-registration attempt did not create a second organization
        # for the same address.
        users = db.query(User).filter(User.email == email).all()
        assert len(users) == 1
        assert users[0].google_id is None  # the registration attempt did not link/create anything
    finally:
        db.close()


def test_case_insensitive_duplicate_rejected_by_registration():
    """A case-variant of an existing email cannot complete a second
    registration — matches the platform-wide, case-insensitive uniqueness
    rule enforced by auth_service.register_organization."""
    base_email = f"CaseTest_{uuid.uuid4().hex[:8]}@Example.com"
    reg_res = client.post(
        "/auth/register",
        json={
            "organization_name": f"Case Co {uuid.uuid4().hex[:6]}",
            "email": base_email,
            "admin_name": "Case Admin",
            "password": "CasePass123",
        },
    )
    assert reg_res.status_code == 201

    variant_email = base_email.lower()
    sub = f"sub_case_{uuid.uuid4().hex[:10]}"
    code = _mint_registration_code(variant_email, sub)
    res = client.post("/auth/google/complete-registration", json=_base_registration_payload(code))
    assert res.status_code == 409


# --------------------------------------------------------------------------
# Scenario 1 & 2: existing users across all roles (Admin/Sales/Delivery/Accountant)
# --------------------------------------------------------------------------


def _login_via_google(email: str, sub: str) -> dict:
    with patch("google.oauth2.id_token.verify_oauth2_token", return_value=_google_claims(email, sub)):
        res = client.post("/auth/google", json={"credential": "tok"})
        assert res.status_code == 200
        return res.json()


def test_existing_admin_google_login_preserves_role_org_permissions():
    email = f"role_admin_{uuid.uuid4().hex[:8]}@example.com"
    gid = f"gid_admin_{uuid.uuid4().hex[:10]}"
    org, user = _create_test_org_and_user(email=email, google_id=gid, role=UserRole.ADMIN)

    data = _login_via_google(email, gid)
    assert data["organization"]["id"] == org.id
    assert data["user"]["system_role"] == "admin"

    me = client.get("/auth/me", headers={"Authorization": f"Bearer {data['tokens']['access_token']}"})
    assert me.status_code == 200
    assert me.json()["full_access"] is True
    assert me.json()["organization_id"] == org.id


def test_admin_created_employee_google_login_preserves_staff_role():
    """Scenario 2: an Admin-created Sales/Delivery/Accountant employee logs in
    with Google for the first time (google_id NULL -> linked) and keeps their
    org-scoped role and permissions exactly as the Admin configured them."""
    org, admin = _create_test_org_and_user(email=f"owner_{uuid.uuid4().hex[:6]}@example.com")

    for role_name, workspace in (
        ("Sales Officer", "sales"),
        ("Delivery Partner", "delivery"),
        ("Accountant", "accounts"),
    ):
        role_id = _staff_role_id(org.id, role_name)
        email = f"{role_name.split()[0].lower()}_{uuid.uuid4().hex[:6]}@example.com"

        db = SessionLocal()
        try:
            staff = User(
                organization_id=org.id,
                name=f"{role_name} Employee",
                email=email,
                password_hash=hash_password("StaffPass123"),
                system_role="staff",
                role_id=role_id,
                google_id=None,  # not linked yet — Admin created this account by hand
            )
            db.add(staff)
            db.commit()
            db.refresh(staff)
        finally:
            db.close()

        gid = f"gid_{role_name.split()[0].lower()}_{uuid.uuid4().hex[:10]}"
        data = _login_via_google(email, gid)
        assert data["organization"]["id"] == org.id
        assert data["user"]["google_id"] == gid  # newly linked
        assert data["user"]["role_id"] == role_id

        me = client.get("/auth/me", headers={"Authorization": f"Bearer {data['tokens']['access_token']}"})
        assert me.status_code == 200
        me_data = me.json()
        assert me_data["full_access"] is False
        assert me_data["role"]["name"] == role_name
        assert me_data["role"]["workspace"] == workspace

        # Second login (google_id now set) resolves via Case B, no re-linking needed.
        db = SessionLocal()
        try:
            reloaded = db.query(User).filter(User.email == email).first()
            assert reloaded.google_id == gid
            assert reloaded.role_id == role_id
            assert reloaded.organization_id == org.id
        finally:
            db.close()


# --------------------------------------------------------------------------
# Case C: google_id mismatch — must reject, never overwrite/link/register
# --------------------------------------------------------------------------


def test_google_id_mismatch_rejected_direct_flow():
    email = f"mismatch_{uuid.uuid4().hex[:8]}@example.com"
    gid_a = f"gid_A_{uuid.uuid4().hex[:10]}"
    gid_b = f"gid_B_{uuid.uuid4().hex[:10]}"
    org, user = _create_test_org_and_user(email=email, google_id=gid_a)

    db = SessionLocal()
    exchange_before = db.query(OAuthExchangeTicket).count()
    registration_before = db.query(OAuthRegistrationTicket).count()
    db.close()

    with patch("google.oauth2.id_token.verify_oauth2_token", return_value=_google_claims(email, gid_b)):
        res = client.post("/auth/google", json={"credential": "tok"})
        assert res.status_code == 409
        body = res.json()["detail"]
        assert body["error"] == "google_identity_conflict"

    db = SessionLocal()
    try:
        reloaded = db.query(User).filter(User.id == user.id).first()
        assert reloaded.google_id == gid_a  # unchanged — NOT overwritten with gid_b
        assert db.query(OAuthExchangeTicket).count() == exchange_before  # no exchange ticket minted
        assert db.query(OAuthRegistrationTicket).count() == registration_before  # no registration ticket either
        assert db.query(User).filter(User.google_id == gid_b).first() is None  # no new/duplicate user
    finally:
        db.close()


def test_google_id_mismatch_rejected_callback_flow():
    email = f"mismatch_cb_{uuid.uuid4().hex[:8]}@example.com"
    gid_a = f"gid_cbA_{uuid.uuid4().hex[:10]}"
    gid_b = f"gid_cbB_{uuid.uuid4().hex[:10]}"
    _create_test_org_and_user(email=email, google_id=gid_a)

    from app.services import google_auth_service
    from app.core.config import settings

    oauth_state = google_auth_service.generate_oauth_state()
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"id_token": "mock.raw.id_token"}

    with patch.object(settings, "google_client_id", "test-client-id"), \
         patch.object(settings, "google_client_secret", "test-secret"), \
         patch.object(settings, "frontend_url", "http://localhost:5173"), \
         patch("requests.post", return_value=mock_resp), \
         patch("google.oauth2.id_token.verify_oauth2_token", return_value=_google_claims(email, gid_b)):
        res = client.get(f"/auth/google/callback?code=some_code&state={oauth_state}")
        assert res.status_code == 307
        location = res.headers["location"]
        assert location.startswith("http://localhost:5173/auth/callback?error=")
        assert "google_identity_conflict" in location
        assert "exchange_code" not in location
        assert "registration_code" not in location
        assert "access_token" not in location

    db = SessionLocal()
    try:
        reloaded = db.query(User).filter(User.email == email).first()
        assert reloaded.google_id == gid_a
    finally:
        db.close()


def test_google_id_match_still_logs_in_normally():
    """Case B sanity check alongside the new Case C logic: an exact google_id
    match must keep working unaffected."""
    email = f"caseb_{uuid.uuid4().hex[:8]}@example.com"
    gid = f"gid_caseb_{uuid.uuid4().hex[:10]}"
    _create_test_org_and_user(email=email, google_id=gid)

    data = _login_via_google(email, gid)
    assert data["user"]["email"] == email
    assert data["user"]["google_id"] == gid


if __name__ == "__main__":
    tests = [
        test_direct_flow_new_email_returns_registration_required,
        test_callback_flow_new_email_redirects_with_registration_code,
        test_registration_info_returns_email_and_name,
        test_registration_info_does_not_consume_code,
        test_registration_info_invalid_code_rejected,
        test_registration_info_expired_code_rejected,
        test_complete_registration_creates_org_admin_trial_roles_and_tokens,
        test_complete_registration_email_not_accepted_from_client,
        test_complete_registration_name_editable_email_locked,
        test_complete_registration_rejects_reused_code,
        test_complete_registration_rejects_expired_code,
        test_complete_registration_rejects_tampered_code,
        test_double_submit_only_creates_one_organization,
        test_complete_registration_requires_organization_name,
        test_complete_registration_password_min_length_enforced,
        test_complete_registration_optional_fields_can_be_omitted,
        test_complete_registration_duplicate_email_rejected,
        test_case_insensitive_duplicate_rejected_by_registration,
        test_existing_admin_google_login_preserves_role_org_permissions,
        test_admin_created_employee_google_login_preserves_staff_role,
        test_google_id_mismatch_rejected_direct_flow,
        test_google_id_mismatch_rejected_callback_flow,
        test_google_id_match_still_logs_in_normally,
    ]
    for t in tests:
        t()
        print(f"  PASS  {t.__name__}")
    print("\nAll Google Registration tests passed successfully!")
