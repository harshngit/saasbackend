import os
import sys
import uuid
from unittest.mock import patch

sys.path.insert(0, os.path.abspath("."))

from fastapi.testclient import TestClient

from app.core.database import SessionLocal, auto_add_missing_columns
from app.core.security import hash_password
from app.main import app
from app.models import Organization, User, UserRole
from app.services import org_service

# Ensure SQLite DB has all columns migrated
auto_add_missing_columns()

client = TestClient(app)


def _create_test_org_and_user(
    email: str,
    name: str = "Test User",
    is_active: bool = True,
    google_id: str | None = None,
    role: UserRole = UserRole.ADMIN,
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
            system_role="admin" if role == UserRole.ADMIN else "staff",
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


def test_google_auth_endpoint_exists():
    """1. Verify POST /auth/google exists."""
    res = client.post("/auth/google", json={})
    assert res.status_code != 404


def test_google_auth_missing_credential():
    """2. Missing or empty credential should be rejected."""
    res1 = client.post("/auth/google", json={})
    assert res1.status_code == 422

    res2 = client.post("/auth/google", json={"credential": ""})
    assert res2.status_code in (400, 422)


def test_google_auth_invalid_token():
    """3. Invalid Google token is rejected."""
    with patch("google.oauth2.id_token.verify_oauth2_token", side_effect=ValueError("Invalid token")):
        res = client.post("/auth/google", json={"credential": "invalid.fake.token"})
        assert res.status_code == 401
        assert "Invalid or expired Google token" in res.json()["detail"]


def test_google_auth_expired_token():
    """4. Expired token is rejected."""
    with patch("google.oauth2.id_token.verify_oauth2_token", side_effect=ValueError("Token expired")):
        res = client.post("/auth/google", json={"credential": "expired.jwt.token"})
        assert res.status_code == 401
        assert "Invalid or expired Google token" in res.json()["detail"]


def test_google_auth_wrong_audience():
    """5. Wrong audience is rejected."""
    with patch("google.oauth2.id_token.verify_oauth2_token", side_effect=ValueError("Wrong audience")):
        res = client.post("/auth/google", json={"credential": "wrong.audience.token"})
        assert res.status_code == 401
        assert "Invalid or expired Google token" in res.json()["detail"]


def test_google_auth_wrong_issuer():
    """6. Wrong issuer is rejected."""
    fake_claims = {
        "iss": "https://evil.attacker.com",
        "sub": "1234567890",
        "email": "user@example.com",
        "email_verified": True,
        "name": "Evil User",
    }
    with patch("google.oauth2.id_token.verify_oauth2_token", return_value=fake_claims):
        res = client.post("/auth/google", json={"credential": "fake.jwt.token"})
        assert res.status_code == 401
        assert "Invalid Google token issuer" in res.json()["detail"]


def test_google_auth_unverified_email():
    """7. Unverified email is rejected to prevent account takeover."""
    fake_claims = {
        "iss": "accounts.google.com",
        "sub": "1234567890",
        "email": "unverified@example.com",
        "email_verified": False,
        "name": "Unverified User",
    }
    with patch("google.oauth2.id_token.verify_oauth2_token", return_value=fake_claims):
        res = client.post("/auth/google", json={"credential": "fake.jwt.token"})
        assert res.status_code == 401
        assert "Google email is not verified" in res.json()["detail"]


def test_google_auth_existing_user_with_google_id():
    """8. Existing user with google_id can log in and receives CRM JWTs."""
    email = f"google_user_{uuid.uuid4().hex[:6]}@example.com"
    gid = f"gid_{uuid.uuid4().hex[:12]}"
    org, user = _create_test_org_and_user(email=email, google_id=gid)

    fake_claims = {
        "iss": "https://accounts.google.com",
        "sub": gid,
        "email": email,
        "email_verified": True,
        "name": "Google User",
    }
    with patch("google.oauth2.id_token.verify_oauth2_token", return_value=fake_claims):
        res = client.post("/auth/google", json={"credential": "valid.google.token"})
        assert res.status_code == 200
        data = res.json()
        assert data["user"]["id"] == user.id
        assert data["user"]["email"] == email
        assert data["user"]["google_id"] == gid
        assert data["organization"]["id"] == org.id
        assert "access_token" in data["tokens"]
        assert "refresh_token" in data["tokens"]

        # Validate access token on /auth/me
        me_res = client.get(
            "/auth/me", headers={"Authorization": f"Bearer {data['tokens']['access_token']}"}
        )
        assert me_res.status_code == 200
        assert me_res.json()["user"]["id"] == user.id


def test_google_auth_account_linking_by_email():
    """9 & 10. Existing user matched by email has google_id linked with no duplicate user created."""
    email = f"link_user_{uuid.uuid4().hex[:6]}@example.com"
    gid = f"gid_{uuid.uuid4().hex[:12]}"
    org, user = _create_test_org_and_user(email=email, google_id=None)

    db = SessionLocal()
    initial_user_count = db.query(User).count()
    db.close()

    fake_claims = {
        "iss": "accounts.google.com",
        "sub": gid,
        "email": email,
        "email_verified": True,
        "name": "Link User",
    }
    with patch("google.oauth2.id_token.verify_oauth2_token", return_value=fake_claims):
        res = client.post("/auth/google", json={"credential": "valid.google.token"})
        assert res.status_code == 200
        data = res.json()
        assert data["user"]["id"] == user.id
        assert data["user"]["email"] == email
        assert data["user"]["google_id"] == gid

    # Verify DB state: google_id is saved and user count didn't change
    db = SessionLocal()
    try:
        updated_user = db.get(User, user.id)
        assert updated_user is not None
        assert updated_user.google_id == gid
        assert db.query(User).count() == initial_user_count
    finally:
        db.close()


def test_google_auth_organization_role_permissions_unchanged():
    """11, 12, 13. Org, role, and permissions remain intact after Google login."""
    email = f"perm_user_{uuid.uuid4().hex[:6]}@example.com"
    gid = f"gid_{uuid.uuid4().hex[:12]}"
    org, user = _create_test_org_and_user(email=email, role=UserRole.ADMIN)

    fake_claims = {
        "iss": "accounts.google.com",
        "sub": gid,
        "email": email,
        "email_verified": True,
        "name": "Perm User",
    }
    with patch("google.oauth2.id_token.verify_oauth2_token", return_value=fake_claims):
        res = client.post("/auth/google", json={"credential": "valid.google.token"})
        assert res.status_code == 200
        data = res.json()
        assert data["organization"]["id"] == org.id
        assert data["user"]["system_role"] == "admin"

        # Check /auth/me for permissions
        me_res = client.get(
            "/auth/me", headers={"Authorization": f"Bearer {data['tokens']['access_token']}"}
        )
        assert me_res.status_code == 200
        me_data = me_res.json()
        assert me_data["full_access"] is True
        assert me_data["organization_id"] == org.id


def test_google_auth_refresh_token_issuance_and_refresh():
    """14, 15, 17. Google login issues a valid refresh token that can be used with /auth/refresh."""
    email = f"refresh_user_{uuid.uuid4().hex[:6]}@example.com"
    gid = f"gid_{uuid.uuid4().hex[:12]}"
    _create_test_org_and_user(email=email, google_id=gid)

    fake_claims = {
        "iss": "accounts.google.com",
        "sub": gid,
        "email": email,
        "email_verified": True,
        "name": "Refresh User",
    }
    with patch("google.oauth2.id_token.verify_oauth2_token", return_value=fake_claims):
        res = client.post("/auth/google", json={"credential": "valid.google.token"})
        assert res.status_code == 200
        tokens = res.json()["tokens"]
        refresh_token = tokens["refresh_token"]

    # Use the refresh token
    ref_res = client.post("/auth/refresh", json={"refresh_token": refresh_token})
    assert ref_res.status_code == 200
    ref_data = ref_res.json()
    assert "access_token" in ref_data
    assert ref_data["refresh_token"] == refresh_token


def test_existing_password_login_still_works():
    """16. Existing email/password login still functions seamlessly."""
    email = f"pass_user_{uuid.uuid4().hex[:6]}@example.com"
    gid = f"gid_{uuid.uuid4().hex[:12]}"
    _create_test_org_and_user(email=email, google_id=gid)

    # Password login
    res = client.post("/auth/login", json={"email": email, "password": "Password@123"})
    assert res.status_code == 200
    assert res.json()["user"]["email"] == email
    assert "tokens" in res.json()


def test_inactive_user_blocked_from_google_login():
    """19. Deactivated/inactive users cannot log in with Google."""
    email = f"inactive_{uuid.uuid4().hex[:6]}@example.com"
    gid = f"gid_{uuid.uuid4().hex[:12]}"
    _create_test_org_and_user(email=email, is_active=False, google_id=gid)

    fake_claims = {
        "iss": "accounts.google.com",
        "sub": gid,
        "email": email,
        "email_verified": True,
        "name": "Inactive User",
    }
    with patch("google.oauth2.id_token.verify_oauth2_token", return_value=fake_claims):
        res = client.post("/auth/google", json={"credential": "valid.google.token"})
        assert res.status_code == 403
        assert "Account is deactivated" in res.json()["detail"]


def test_unregistered_google_user_returns_404():
    """18, 20. Google user with no existing CRM account returns 404."""
    fake_claims = {
        "iss": "accounts.google.com",
        "sub": f"gid_nonexistent_{uuid.uuid4().hex[:8]}",
        "email": f"unknown_{uuid.uuid4().hex[:6]}@example.com",
        "email_verified": True,
        "name": "Unknown User",
    }
    with patch("google.oauth2.id_token.verify_oauth2_token", return_value=fake_claims):
        res = client.post("/auth/google", json={"credential": "valid.google.token"})
        assert res.status_code == 404
        assert "No CRM account found" in res.json()["detail"]


def test_google_auth_multi_tenant_isolation():
    """18. Multi-tenant isolation remains strictly intact."""
    email_a = f"org_a_user_{uuid.uuid4().hex[:6]}@example.com"
    email_b = f"org_b_user_{uuid.uuid4().hex[:6]}@example.com"
    gid_a = f"gid_a_{uuid.uuid4().hex[:10]}"
    gid_b = f"gid_b_{uuid.uuid4().hex[:10]}"

    org_a, user_a = _create_test_org_and_user(email=email_a, google_id=gid_a)
    org_b, user_b = _create_test_org_and_user(email=email_b, google_id=gid_b)

    # Login as User A via Google
    fake_claims_a = {
        "iss": "accounts.google.com",
        "sub": gid_a,
        "email": email_a,
        "email_verified": True,
        "name": "Org A User",
    }
    with patch("google.oauth2.id_token.verify_oauth2_token", return_value=fake_claims_a):
        res = client.post("/auth/google", json={"credential": "token_a"})
        assert res.status_code == 200
        token_a = res.json()["tokens"]["access_token"]

    # Verify User A's token only sees Org A's users
    users_res = client.get("/users", headers={"Authorization": f"Bearer {token_a}"})
    assert users_res.status_code == 200
    user_emails = [u["email"] for u in users_res.json()]
    assert email_a in user_emails
    assert email_b not in user_emails  # Org B's user is invisible to Org A


def test_google_auth_missing_claims():
    """Verify Google token with missing required claims (sub or email) is rejected."""
    fake_claims = {
        "iss": "accounts.google.com",
        "name": "Missing Sub and Email",
        "email_verified": True,
    }
    with patch("google.oauth2.id_token.verify_oauth2_token", return_value=fake_claims):
        res = client.post("/auth/google", json={"credential": "claims.missing.token"})
        assert res.status_code == 401
        assert "missing required identity claims" in res.json()["detail"].lower()


if __name__ == "__main__":
    test_google_auth_endpoint_exists()
    print("  PASS  test_google_auth_endpoint_exists")
    test_google_auth_missing_credential()
    print("  PASS  test_google_auth_missing_credential")
    test_google_auth_invalid_token()
    print("  PASS  test_google_auth_invalid_token")
    test_google_auth_expired_token()
    print("  PASS  test_google_auth_expired_token")
    test_google_auth_wrong_audience()
    print("  PASS  test_google_auth_wrong_audience")
    test_google_auth_wrong_issuer()
    print("  PASS  test_google_auth_wrong_issuer")
    test_google_auth_unverified_email()
    print("  PASS  test_google_auth_unverified_email")
    test_google_auth_missing_claims()
    print("  PASS  test_google_auth_missing_claims")
    test_google_auth_existing_user_with_google_id()
    print("  PASS  test_google_auth_existing_user_with_google_id")
    test_google_auth_account_linking_by_email()
    print("  PASS  test_google_auth_account_linking_by_email")
    test_google_auth_organization_role_permissions_unchanged()
    print("  PASS  test_google_auth_organization_role_permissions_unchanged")
    test_google_auth_refresh_token_issuance_and_refresh()
    print("  PASS  test_google_auth_refresh_token_issuance_and_refresh")
    test_existing_password_login_still_works()
    print("  PASS  test_existing_password_login_still_works")
    test_inactive_user_blocked_from_google_login()
    print("  PASS  test_inactive_user_blocked_from_google_login")
    test_unregistered_google_user_returns_404()
    print("  PASS  test_unregistered_google_user_returns_404")
    test_google_auth_multi_tenant_isolation()
    print("  PASS  test_google_auth_multi_tenant_isolation")
    print("\nAll Google Auth tests passed successfully!")

