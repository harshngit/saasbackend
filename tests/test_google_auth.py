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


def test_unregistered_google_user_returns_registration_required():
    """18, 20. Google user with no existing CRM account gets a registration_code,
    NOT an auto-created account and NOT the old bare 404."""
    email = f"unknown_{uuid.uuid4().hex[:6]}@example.com"
    fake_claims = {
        "iss": "accounts.google.com",
        "sub": f"gid_nonexistent_{uuid.uuid4().hex[:8]}",
        "email": email,
        "email_verified": True,
        "name": "Unknown User",
    }
    db = SessionLocal()
    users_before = db.query(User).count()
    db.close()

    with patch("google.oauth2.id_token.verify_oauth2_token", return_value=fake_claims):
        res = client.post("/auth/google", json={"credential": "valid.google.token"})
        assert res.status_code == 200
        data = res.json()
        assert data["status"] == "registration_required"
        assert isinstance(data["registration_code"], str) and len(data["registration_code"]) > 20

    # No CRM account was created merely by verifying the Google identity.
    db = SessionLocal()
    try:
        assert db.query(User).count() == users_before
        assert db.query(User).filter(User.email == email).first() is None
    finally:
        db.close()


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


# ---------------------------------------------------------------------------
# Audience hardening and case-insensitive account matching
# ---------------------------------------------------------------------------

TEST_WEB_CLIENT_ID = "test-web-client.apps.googleusercontent.com"


def _signed_google_token(audience: str, **overrides) -> tuple[str, dict]:
    """A real RS256 ID token signed with a throwaway key, plus the certs mapping that
    google-auth should trust -- so verify_oauth2_token runs for real, offline."""
    import time

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from google.auth import crypt
    from google.auth import jwt as google_jwt

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    public_pem = key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    now = int(time.time())
    claims = {
        "iss": "https://accounts.google.com",
        "aud": audience,
        "sub": f"gid_{uuid.uuid4().hex[:12]}",
        "email": f"signed_{uuid.uuid4().hex[:6]}@example.com",
        "email_verified": True,
        "name": "Signed User",
        "iat": now,
        "exp": now + 600,
        **overrides,
    }
    signer = crypt.RSASigner.from_string(private_pem, key_id="test-key")
    token = google_jwt.encode(signer, claims).decode()
    return token, {"test-key": public_pem.decode()}


def test_configured_client_id_is_the_expected_audience():
    """The configured web client ID is what the token's `aud` is checked against."""
    fake_claims = {"iss": "accounts.google.com", "sub": "aud-check", "email": "aud@example.com",
                   "email_verified": True, "name": "Aud"}
    with patch("app.services.google_auth_service.settings.google_client_id", f"  {TEST_WEB_CLIENT_ID}  "), \
            patch("google.oauth2.id_token.verify_oauth2_token", return_value=fake_claims) as verify:
        client.post("/auth/google", json={"credential": "some.google.token"})
    assert verify.call_args.kwargs["audience"] == TEST_WEB_CLIENT_ID


def test_missing_client_id_fails_closed_with_503():
    """No configured audience: never verify (google-auth would skip the aud check)."""
    for unset in ("", "   ", None):
        with patch("app.services.google_auth_service.settings.google_client_id", unset), \
                patch("google.oauth2.id_token.verify_oauth2_token") as verify:
            res = client.post("/auth/google", json={"credential": "some.google.token"})
        assert res.status_code == 503
        assert res.json()["detail"] == "Google sign-in is not configured"
        verify.assert_not_called()


def test_real_signature_check_accepts_matching_and_rejects_other_audience():
    """Real google-auth verification (offline certs): right audience in, other app's token out."""
    good, certs = _signed_google_token(TEST_WEB_CLIENT_ID)
    other_app, other_certs = _signed_google_token("some-other-app.apps.googleusercontent.com")
    expired, expired_certs = _signed_google_token(TEST_WEB_CLIENT_ID, iat=1_000_000, exp=1_000_600)
    with patch("app.services.google_auth_service.settings.google_client_id", TEST_WEB_CLIENT_ID):
        with patch("google.oauth2.id_token._fetch_certs", return_value=certs):
            ok = client.post("/auth/google", json={"credential": good})
        with patch("google.oauth2.id_token._fetch_certs", return_value=other_certs):
            wrong = client.post("/auth/google", json={"credential": other_app})
        with patch("google.oauth2.id_token._fetch_certs", return_value=expired_certs):
            stale = client.post("/auth/google", json={"credential": expired})
        with patch("google.oauth2.id_token._fetch_certs", return_value=other_certs):
            forged = client.post("/auth/google", json={"credential": good})  # signed by a key Google doesn't hold
    assert ok.status_code == 200 and ok.json()["status"] == "registration_required"
    for res in (wrong, stale, forged):
        assert res.status_code == 401
        assert "Invalid or expired Google token" in res.json()["detail"]


def test_mixed_case_staff_email_matches_and_keeps_account():
    """A staff account stored as Ravi.X@Firm.com signs in with Google's ravi.x@firm.com."""
    tag = uuid.uuid4().hex[:6]
    stored_email = f"Ravi.{tag}@Firm-Example.COM"
    org, user = _create_test_org_and_user(email=stored_email, role=UserRole.SALES_OFFICER)
    gid = f"gid_{uuid.uuid4().hex[:12]}"
    fake_claims = {"iss": "accounts.google.com", "sub": gid, "email": stored_email.lower(),
                   "email_verified": True, "name": "Ravi"}

    db = SessionLocal()
    users_before = db.query(User).count()
    db.close()

    with patch("google.oauth2.id_token.verify_oauth2_token", return_value=fake_claims):
        res = client.post("/auth/google", json={"credential": "valid.google.token"})
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["user"]["id"] == user.id
    assert data["organization"]["id"] == org.id
    assert data["user"]["system_role"] == "staff" and data["user"]["role"] == UserRole.SALES_OFFICER.value
    # (UserOut's EmailStr lowercases the domain when serializing; the stored row is checked below)
    assert data["user"]["email"].lower() == stored_email.lower()

    db = SessionLocal()
    try:
        linked = db.get(User, user.id)
        assert linked.google_id == gid and linked.email == stored_email
        assert db.query(User).count() == users_before  # no new account
    finally:
        db.close()


def test_mixed_case_email_linked_to_other_google_identity_still_conflicts():
    tag = uuid.uuid4().hex[:6]
    stored_email = f"Linked.{tag}@Example.com"
    _, user = _create_test_org_and_user(email=stored_email, google_id=f"gid_{uuid.uuid4().hex[:12]}")
    fake_claims = {"iss": "accounts.google.com", "sub": "a-different-google-account",
                   "email": stored_email.lower(), "email_verified": True, "name": "Other"}
    with patch("google.oauth2.id_token.verify_oauth2_token", return_value=fake_claims):
        res = client.post("/auth/google", json={"credential": "valid.google.token"})
    assert res.status_code == 409
    assert res.json()["detail"]["error"] == "google_identity_conflict"
    db = SessionLocal()
    try:
        assert db.get(User, user.id).google_id != "a-different-google-account"
    finally:
        db.close()


def test_duplicate_case_insensitive_emails_are_not_linked():
    """Two rows differing only by email casing: refuse rather than pick one.

    Runs the resolver on its own in-memory database, since the shared test database
    may enforce case-insensitive email uniqueness and refuse to hold such rows."""
    import pytest
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from sqlalchemy.pool import StaticPool

    from app.core.database import Base
    from app.routers.auth import GoogleAccountAmbiguous, _resolve_google_identity

    mem = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(mem)
    db = Session(bind=mem)
    try:
        db.add_all([
            User(name="A", email="dup@example.com", password_hash="h", system_role="staff", is_active=True),
            User(name="B", email="DUP@example.com", password_hash="h", system_role="staff", is_active=True),
        ])
        db.commit()
        with pytest.raises(GoogleAccountAmbiguous):
            _resolve_google_identity(db, "gid-dup", "dup@example.com")
        assert all(u.google_id is None for u in db.query(User).all())  # nothing linked

        # An exact google_id link still wins over the ambiguous email
        db.query(User).filter(User.name == "B").update({"google_id": "gid-b"})
        db.commit()
        assert _resolve_google_identity(db, "gid-b", "dup@example.com").name == "B"
    finally:
        db.close()
        mem.dispose()

    # The endpoint turns the ambiguity into the documented 409
    with patch("app.routers.auth._resolve_google_identity", side_effect=GoogleAccountAmbiguous()), \
            patch("google.oauth2.id_token.verify_oauth2_token", return_value={
                "iss": "accounts.google.com", "sub": "gid-dup", "email": "dup@example.com",
                "email_verified": True, "name": "Dup"}):
        res = client.post("/auth/google", json={"credential": "valid.google.token"})
    assert res.status_code == 409
    assert res.json()["detail"]["error"] == "google_account_ambiguous"


def test_no_matching_account_still_requires_registration():
    fake_claims = {"iss": "accounts.google.com", "sub": f"gid_{uuid.uuid4().hex[:12]}",
                   "email": f"Brand.New.{uuid.uuid4().hex[:6]}@example.com".lower(),
                   "email_verified": True, "name": "New"}
    db = SessionLocal()
    users_before = db.query(User).count()
    db.close()
    with patch("google.oauth2.id_token.verify_oauth2_token", return_value=fake_claims):
        res = client.post("/auth/google", json={"credential": "valid.google.token"})
    assert res.status_code == 200
    assert res.json()["status"] == "registration_required" and res.json()["registration_code"]
    db = SessionLocal()
    try:
        assert db.query(User).count() == users_before
    finally:
        db.close()


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
    test_unregistered_google_user_returns_registration_required()
    print("  PASS  test_unregistered_google_user_returns_registration_required")
    test_google_auth_multi_tenant_isolation()
    print("  PASS  test_google_auth_multi_tenant_isolation")
    print("\nAll Google Auth tests passed successfully!")

