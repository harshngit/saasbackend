import os
import sys
import time
import urllib.parse
import uuid
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath("."))

from fastapi.testclient import TestClient

from app.core.config import settings
from app.core.database import Base, SessionLocal, auto_add_missing_columns, engine
from app.core.security import hash_password
from app.main import app
from app.models import Organization, User, UserRole
from app.services import google_auth_service, org_service

# Ensure SQLite DB has all tables & columns migrated
Base.metadata.create_all(bind=engine)
auto_add_missing_columns()

client = TestClient(app, follow_redirects=False)


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


def test_get_auth_google_redirects_and_url_parameters():
    """1, 2, 3, 4, 5, 6: Verify GET /auth/google generates signed state and redirects with correct params."""
    with patch.object(settings, "google_client_id", "test-client-id.apps.googleusercontent.com"), \
         patch.object(settings, "google_redirect_uri", "http://localhost:8000/auth/google/callback"):
        res = client.get("/auth/google")
        assert res.status_code == 307
        location = res.headers.get("location")
        assert location is not None
        assert location.startswith("https://accounts.google.com/o/oauth2/v2/auth?")

        parsed = urllib.parse.urlparse(location)
        query = urllib.parse.parse_qs(parsed.query)

        assert query["response_type"] == ["code"]
        assert query["client_id"] == ["test-client-id.apps.googleusercontent.com"]
        assert query["redirect_uri"] == ["http://localhost:8000/auth/google/callback"]
        assert query["scope"] == ["openid email profile"]
        assert "state" in query

        # Verify state is signed and valid
        state = query["state"][0]
        claims = google_auth_service.verify_oauth_state(state)
        assert "nonce" in claims
        assert claims["type"] == "oauth_state"


def test_callback_missing_state_rejected():
    """7. Missing state parameter is rejected."""
    res = client.get("/auth/google/callback?code=some_code")
    assert res.status_code == 400
    assert "Missing OAuth code or state" in res.json()["detail"]


def test_callback_tampered_state_rejected():
    """8. Tampered state parameter is rejected."""
    res = client.get("/auth/google/callback?code=some_code&state=tampered.fake.state")
    assert res.status_code == 400
    assert "Invalid or tampered OAuth state" in res.json()["detail"]


def test_callback_expired_state_rejected():
    """9. Expired state parameter is rejected."""
    # Generate state with past timestamp
    now = time.time() - 100
    import jwt
    expired_state = jwt.encode(
        {"nonce": "expired", "type": "oauth_state", "iat": now - 600, "exp": now - 10},
        settings.jwt_secret,
        algorithm="HS256",
    )
    res = client.get(f"/auth/google/callback?code=some_code&state={expired_state}")
    assert res.status_code == 400
    assert "OAuth state parameter has expired" in res.json()["detail"]


def test_callback_google_error_redirects_to_frontend():
    """Handle user cancellation or error from Google."""
    with patch.object(settings, "frontend_url", "http://localhost:5173"):
        res = client.get("/auth/google/callback?error=access_denied&error_description=User%20denied%20access")
        assert res.status_code == 307
        assert res.headers["location"].startswith("http://localhost:5173/auth/callback?error=")


def test_callback_token_exchange_failure_handled():
    """10. Google token endpoint returning non-200 error is handled."""
    state = google_auth_service.generate_oauth_state()

    mock_resp = MagicMock()
    mock_resp.status_code = 400
    mock_resp.text = '{"error": "invalid_grant"}'

    with patch.object(settings, "google_client_id", "test-client-id"), \
         patch.object(settings, "google_client_secret", "test-secret"), \
         patch("requests.post", return_value=mock_resp):
        res = client.get(f"/auth/google/callback?code=bad_code&state={state}")
        assert res.status_code == 400
        assert "Failed to exchange authorization code with Google" in res.json()["detail"]


def test_callback_invalid_google_id_token():
    """11, 12, 13: Invalid Google token claims (audience, issuer) are rejected."""
    state = google_auth_service.generate_oauth_state()

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"id_token": "mock.raw.id_token"}

    with patch.object(settings, "google_client_id", "test-client-id"), \
         patch.object(settings, "google_client_secret", "test-secret"), \
         patch("requests.post", return_value=mock_resp), \
         patch("google.oauth2.id_token.verify_oauth2_token", side_effect=ValueError("Invalid audience")):
        res = client.get(f"/auth/google/callback?code=valid_code&state={state}")
        assert res.status_code == 401
        assert "Invalid or expired Google token" in res.json()["detail"]


def test_callback_unverified_google_email_rejected():
    """14. Unverified Google email is rejected."""
    state = google_auth_service.generate_oauth_state()

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"id_token": "mock.raw.id_token"}

    fake_claims = {
        "iss": "accounts.google.com",
        "sub": "123456789",
        "email": "unverified@example.com",
        "email_verified": False,
        "name": "Unverified User",
    }

    with patch.object(settings, "google_client_id", "test-client-id"), \
         patch.object(settings, "google_client_secret", "test-secret"), \
         patch("requests.post", return_value=mock_resp), \
         patch("google.oauth2.id_token.verify_oauth2_token", return_value=fake_claims):
        res = client.get(f"/auth/google/callback?code=valid_code&state={state}")
        assert res.status_code == 401
        assert "Google email is not verified" in res.json()["detail"]


def test_callback_successful_login_with_existing_google_id_and_ticket_exchange():
    """15, 18, 19, 20, 21: Existing user with google_id gets redirected with exchange_code and exchanges for tokens."""
    email = f"redirect_user_{uuid.uuid4().hex[:6]}@example.com"
    gid = f"gid_{uuid.uuid4().hex[:12]}"
    org, user = _create_test_org_and_user(email=email, google_id=gid)

    state = google_auth_service.generate_oauth_state()

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"id_token": "mock.raw.id_token"}

    fake_claims = {
        "iss": "accounts.google.com",
        "sub": gid,
        "email": email,
        "email_verified": True,
        "name": "Google User",
    }

    with patch.object(settings, "google_client_id", "test-client-id"), \
         patch.object(settings, "google_client_secret", "test-secret"), \
         patch.object(settings, "frontend_url", "http://localhost:5173"), \
         patch("requests.post", return_value=mock_resp), \
         patch("google.oauth2.id_token.verify_oauth2_token", return_value=fake_claims):
        
        callback_res = client.get(f"/auth/google/callback?code=google_code&state={state}")
        assert callback_res.status_code == 307
        redirect_url = callback_res.headers.get("location")
        assert redirect_url is not None
        assert redirect_url.startswith("http://localhost:5173/auth/callback?exchange_code=")

        # 20. Confirm JWTs and sensitive tokens are NOT in the URL
        assert "access_token" not in redirect_url
        assert "refresh_token" not in redirect_url
        assert "eyJh" not in redirect_url

        # Extract exchange code
        parsed = urllib.parse.urlparse(redirect_url)
        params = urllib.parse.parse_qs(parsed.query)
        exchange_code = params["exchange_code"][0]

        # 21. POST /auth/exchange consumes the ticket
        exchange_res = client.post("/auth/exchange", json={"code": exchange_code})
        assert exchange_res.status_code == 200
        data = exchange_res.json()
        assert data["user"]["id"] == user.id
        assert data["user"]["email"] == email
        assert data["organization"]["id"] == org.id
        assert "access_token" in data["tokens"]
        assert "refresh_token" in data["tokens"]

        # Validate access token works on /auth/me
        me_res = client.get(
            "/auth/me", headers={"Authorization": f"Bearer {data['tokens']['access_token']}"}
        )
        assert me_res.status_code == 200
        assert me_res.json()["user"]["id"] == user.id


def test_callback_account_linking_by_email():
    """16. Existing user without google_id is linked by verified email without duplicate user creation."""
    email = f"link_oauth_{uuid.uuid4().hex[:6]}@example.com"
    gid = f"gid_new_{uuid.uuid4().hex[:12]}"
    org, user = _create_test_org_and_user(email=email, google_id=None)

    db = SessionLocal()
    count_before = db.query(User).count()
    db.close()

    state = google_auth_service.generate_oauth_state()

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"id_token": "mock.raw.id_token"}

    fake_claims = {
        "iss": "https://accounts.google.com",
        "sub": gid,
        "email": email,
        "email_verified": True,
        "name": "Link User",
    }

    with patch.object(settings, "google_client_id", "test-client-id"), \
         patch.object(settings, "google_client_secret", "test-secret"), \
         patch("requests.post", return_value=mock_resp), \
         patch("google.oauth2.id_token.verify_oauth2_token", return_value=fake_claims):
        
        callback_res = client.get(f"/auth/google/callback?code=google_code&state={state}")
        assert callback_res.status_code == 307
        parsed = urllib.parse.urlparse(callback_res.headers.get("location"))
        exchange_code = urllib.parse.parse_qs(parsed.query)["exchange_code"][0]

        exchange_res = client.post("/auth/exchange", json={"code": exchange_code})
        assert exchange_res.status_code == 200
        assert exchange_res.json()["user"]["google_id"] == gid

    # Verify user count did not increase and google_id was updated
    db = SessionLocal()
    try:
        updated = db.get(User, user.id)
        assert updated.google_id == gid
        assert db.query(User).count() == count_before
    finally:
        db.close()


def test_exchange_ticket_cannot_be_reused():
    """22. Exchange ticket is single-use and cannot be replayed."""
    email = f"single_use_{uuid.uuid4().hex[:6]}@example.com"
    gid = f"gid_{uuid.uuid4().hex[:12]}"
    _create_test_org_and_user(email=email, google_id=gid)

    state = google_auth_service.generate_oauth_state()

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"id_token": "mock.raw.id_token"}

    fake_claims = {
        "iss": "accounts.google.com",
        "sub": gid,
        "email": email,
        "email_verified": True,
        "name": "Single Use User",
    }

    with patch.object(settings, "google_client_id", "test-client-id"), \
         patch.object(settings, "google_client_secret", "test-secret"), \
         patch("requests.post", return_value=mock_resp), \
         patch("google.oauth2.id_token.verify_oauth2_token", return_value=fake_claims):
        
        res = client.get(f"/auth/google/callback?code=google_code&state={state}")
        parsed = urllib.parse.urlparse(res.headers.get("location"))
        exchange_code = urllib.parse.parse_qs(parsed.query)["exchange_code"][0]

        # First consumption -> 200
        r1 = client.post("/auth/exchange", json={"code": exchange_code})
        assert r1.status_code == 200

        # Second consumption attempt -> 400 (Replay rejected)
        r2 = client.post("/auth/exchange", json={"code": exchange_code})
        assert r2.status_code == 400
        assert "Invalid, expired, or already used exchange ticket" in r2.json()["detail"]


def test_exchange_ticket_expired():
    """23. Expired exchange ticket is rejected."""
    email = f"exp_ticket_{uuid.uuid4().hex[:6]}@example.com"
    _, user = _create_test_org_and_user(email=email)

    import hashlib
    from datetime import datetime, timedelta, timezone
    from app.models import OAuthExchangeTicket

    db = SessionLocal()
    raw_code = f"expired_ticket_code_{uuid.uuid4().hex}"
    db.add(
        OAuthExchangeTicket(
            user_id=user.id,
            ticket_hash=hashlib.sha256(raw_code.encode()).hexdigest(),
            expires_at=datetime.now(timezone.utc) - timedelta(seconds=10),
            used=False,
        )
    )
    db.commit()
    db.close()

    res = client.post("/auth/exchange", json={"code": raw_code})
    assert res.status_code == 400
    assert "Invalid, expired, or already used" in res.json()["detail"]


def test_callback_inactive_user_rejected():
    """17. Inactive user cannot authenticate via OAuth callback."""
    email = f"inactive_oauth_{uuid.uuid4().hex[:6]}@example.com"
    gid = f"gid_inact_{uuid.uuid4().hex[:12]}"
    _create_test_org_and_user(email=email, is_active=False, google_id=gid)

    state = google_auth_service.generate_oauth_state()

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"id_token": "mock.raw.id_token"}

    fake_claims = {
        "iss": "accounts.google.com",
        "sub": gid,
        "email": email,
        "email_verified": True,
        "name": "Inactive User",
    }

    with patch.object(settings, "google_client_id", "test-client-id"), \
         patch.object(settings, "google_client_secret", "test-secret"), \
         patch("requests.post", return_value=mock_resp), \
         patch("google.oauth2.id_token.verify_oauth2_token", return_value=fake_claims):
        res = client.get(f"/auth/google/callback?code=google_code&state={state}")
        assert res.status_code == 403
        assert "Account is deactivated" in res.json()["detail"]


def test_both_flows_coexist():
    """24, 25: Existing email/password login and POST /auth/google still work."""
    email = f"coexist_{uuid.uuid4().hex[:6]}@example.com"
    gid = f"gid_coexist_{uuid.uuid4().hex[:12]}"
    _create_test_org_and_user(email=email, google_id=gid)

    # 1. Email/Password login
    pass_res = client.post("/auth/login", json={"email": email, "password": "Password@123"})
    assert pass_res.status_code == 200
    assert pass_res.json()["user"]["email"] == email

    # 2. POST /auth/google (ID token direct flow)
    fake_claims = {
        "iss": "accounts.google.com",
        "sub": gid,
        "email": email,
        "email_verified": True,
        "name": "Coexist User",
    }
    with patch("google.oauth2.id_token.verify_oauth2_token", return_value=fake_claims):
        id_res = client.post("/auth/google", json={"credential": "valid.id.token"})
        assert id_res.status_code == 200
        assert id_res.json()["user"]["email"] == email


if __name__ == "__main__":
    test_get_auth_google_redirects_and_url_parameters()
    print("  PASS  test_get_auth_google_redirects_and_url_parameters")
    test_callback_missing_state_rejected()
    print("  PASS  test_callback_missing_state_rejected")
    test_callback_tampered_state_rejected()
    print("  PASS  test_callback_tampered_state_rejected")
    test_callback_expired_state_rejected()
    print("  PASS  test_callback_expired_state_rejected")
    test_callback_google_error_redirects_to_frontend()
    print("  PASS  test_callback_google_error_redirects_to_frontend")
    test_callback_token_exchange_failure_handled()
    print("  PASS  test_callback_token_exchange_failure_handled")
    test_callback_invalid_google_id_token()
    print("  PASS  test_callback_invalid_google_id_token")
    test_callback_unverified_google_email_rejected()
    print("  PASS  test_callback_unverified_google_email_rejected")
    test_callback_successful_login_with_existing_google_id_and_ticket_exchange()
    print("  PASS  test_callback_successful_login_with_existing_google_id_and_ticket_exchange")
    test_callback_account_linking_by_email()
    print("  PASS  test_callback_account_linking_by_email")
    test_exchange_ticket_cannot_be_reused()
    print("  PASS  test_exchange_ticket_cannot_be_reused")
    test_exchange_ticket_expired()
    print("  PASS  test_exchange_ticket_expired")
    test_callback_inactive_user_rejected()
    print("  PASS  test_callback_inactive_user_rejected")
    test_both_flows_coexist()
    print("  PASS  test_both_flows_coexist")
    print("\nAll Google OAuth Redirect Flow tests passed successfully!")
