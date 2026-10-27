"""Email delivery: masked diagnostics, SMTP 465 vs 587, the Resend/Brevo HTTPS providers,
the Super Admin test endpoint, and forgot-password staying generic.

SMTP is mocked at `smtplib.SMTP` / `smtplib.SMTP_SSL` and the HTTPS providers at
`email_service.http_requests.post`; nothing is ever sent.
"""
import logging
import os
import smtplib
import sys
import uuid
from unittest.mock import MagicMock, patch

import pytest
import requests

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi.testclient import TestClient

from app.core.config import settings
from app.core.database import Base, SessionLocal, auto_add_missing_columns, engine
from app.core.security import create_access_token, hash_password
from app.main import app
from app.models import AuthRateLimitEvent, Organization, User, UserRole
from app.services import email_service, org_service

Base.metadata.create_all(bind=engine)
auto_add_missing_columns()

client = TestClient(app)

SMTP_PASSWORD = "smtp-secret-pass-123"
API_KEY = "re_live_secret_key_456"
SMTP = {
    "email_provider": "smtp",
    "email_api_key": "",
    "email_from": "",
    "smtp_host": "smtp.example.com",
    "smtp_port": 587,
    "smtp_user": "mailer@beassuite.com",
    "smtp_password": SMTP_PASSWORD,
    "smtp_from": "Beas Suite <no-reply@beassuite.com>",
    "smtp_use_tls": True,
}
GENERIC = "If that email is registered, a reset link has been sent."


# ------------------------------------------------------------------ helpers


def _org() -> Organization:
    db = SessionLocal()
    try:
        org = Organization(name=f"Org {uuid.uuid4().hex[:8]}", email=f"org_{uuid.uuid4().hex[:8]}@example.com")
        org_service.start_trial(db, org)
        db.add(org)
        db.commit()
        db.refresh(org)
        return org
    finally:
        db.close()


def _user(kind: str, active: bool = True) -> User:
    role = {"staff": None, "admin": UserRole.ADMIN, "super_admin": UserRole.SUPER_ADMIN}[kind]
    org = None if kind == "super_admin" else _org()
    db = SessionLocal()
    try:
        user = User(
            organization_id=org.id if org else None,
            name=f"{kind} {uuid.uuid4().hex[:6]}",
            email=f"{kind}_{uuid.uuid4().hex[:10]}@example.com",
            password_hash=hash_password("Password@123"),
            role=role,
            system_role=kind,
            is_active=active,
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        return user
    finally:
        db.close()


def _auth(user: User) -> dict:
    return {"Authorization": f"Bearer {create_access_token(user.id, user.system_role, user.organization_id)}"}


class _Resp:
    def __init__(self, status_code: int, body: object = None) -> None:
        self.status_code = status_code
        self._body = body

    def json(self):  # noqa: ANN201
        if self._body is None:
            raise ValueError("no body")
        return self._body


@pytest.fixture(autouse=True)
def _isolation():
    """SMTP config by default; every real network path fails the test unless a test mocks it."""
    db = SessionLocal()
    db.query(AuthRateLimitEvent).delete()
    db.commit()
    db.close()
    with patch.multiple(settings, **SMTP, client_ip_header="", expose_reset_token=False), \
            patch("app.services.email_service.http_requests.post", side_effect=AssertionError("unmocked HTTP")), \
            patch("smtplib.SMTP", side_effect=AssertionError("unmocked SMTP")), \
            patch("smtplib.SMTP_SSL", side_effect=AssertionError("unmocked SMTP_SSL")):
        yield


def _smtp_server():
    """A context-manager SMTP double; returns (factory, server)."""
    server = MagicMock()
    factory = MagicMock()
    factory.return_value.__enter__.return_value = server
    factory.return_value.__exit__.return_value = False
    return factory, server


# ------------------------------------------------------------------ masking


@pytest.mark.parametrize("raw,masked", [
    ("shivam@gmail.com", "s***@gmail.com"),
    ("a@b.co", "a***@b.co"),
    ("  Mixed.Case@Example.COM ", "M***@Example.COM"),
    ("", "<invalid>"),
    (None, "<invalid>"),
    ("no-at-sign", "<invalid>"),
])
def test_mask_email(raw, masked):
    assert email_service.mask_email(raw) == masked


# ------------------------------------------------------------------ SMTP transport


def test_port_587_uses_starttls_and_sends_multipart(caplog):
    caplog.set_level(logging.DEBUG, logger="crm.email")
    factory, server = _smtp_server()
    with patch("smtplib.SMTP", factory):
        email_service.send_password_reset("shivam@gmail.com", "Shivam", "https://app/reset?token=SECRET-TOKEN")
    factory.assert_called_once()
    assert factory.call_args.args[:2] == ("smtp.example.com", 587)
    assert factory.call_args.kwargs["timeout"] == 15
    server.starttls.assert_called_once()
    server.login.assert_called_once_with("mailer@beassuite.com", SMTP_PASSWORD)
    msg = server.send_message.call_args.args[0]
    assert msg["Subject"] == "Reset your Beas Suite password"
    assert msg["From"] == "Beas Suite <no-reply@beassuite.com>"
    assert msg.is_multipart()
    types = [part.get_content_type() for part in msg.iter_parts()]
    assert types == ["text/plain", "text/html"]
    text, html_part = (part.get_content() for part in msg.iter_parts())
    assert "— Beas Suite" in text and f"{settings.reset_token_expire_minutes} minutes" in text
    assert "Reset password" in html_part and "SECRET-TOKEN" in html_part

    assert "email: sent to s***@gmail.com via smtp in" in caplog.text
    for secret in ("SECRET-TOKEN", "shivam@gmail.com", SMTP_PASSWORD):
        assert secret not in caplog.text


def test_port_465_uses_smtp_ssl_without_starttls():
    factory, server = _smtp_server()
    with patch.multiple(settings, smtp_port=465), patch("smtplib.SMTP_SSL", factory):
        email_service.send_email("x@example.com", "s", "b")
    assert factory.call_args.args[:2] == ("smtp.example.com", 465)
    assert factory.call_args.kwargs["timeout"] == 15
    server.starttls.assert_not_called()
    server.send_message.assert_called_once()


def test_transport_summary_names_ssl_vs_starttls():
    with patch.multiple(settings, smtp_port=465):
        assert "SMTP_SSL" in email_service.transport_summary()
    with patch.multiple(settings, smtp_port=587):
        assert "STARTTLS" in email_service.transport_summary()


def test_smtp_rejection_reports_code_and_text_without_password(caplog):
    caplog.set_level(logging.DEBUG, logger="crm.email")
    factory, server = _smtp_server()
    server.login.side_effect = smtplib.SMTPAuthenticationError(
        535, f"5.7.8 Username and Password not accepted {SMTP_PASSWORD} " + "x" * 400
    )
    with patch("smtplib.SMTP", factory), pytest.raises(email_service.EmailError) as caught:
        email_service.send_email("shivam@gmail.com", "s", "b")
    error = str(caught.value)
    assert error.startswith("SMTPAuthenticationError: 535 5.7.8 Username and Password not accepted")
    assert len(error) <= 200 and SMTP_PASSWORD not in error
    assert "email: FAILED to s***@gmail.com via smtp: SMTPAuthenticationError: 535" in caplog.text
    assert SMTP_PASSWORD not in caplog.text and "shivam@gmail.com" not in caplog.text


def test_connection_error_is_reported():
    with patch("smtplib.SMTP", side_effect=TimeoutError("timed out")), \
            pytest.raises(email_service.EmailError, match="TimeoutError: timed out"):
        email_service.send_email("x@example.com", "s", "b")


def test_local_from_falls_back_to_smtp_user(caplog):
    caplog.set_level(logging.DEBUG, logger="crm.email")
    with patch.multiple(settings, smtp_from="CRM SaaS <no-reply@crm-saas.local>"):
        assert email_service.from_address() == "mailer@beassuite.com"
        email_service.log_startup_config()
    assert "email: mode=smtp host=smtp.example.com port=587 tls=True from=mailer@beassuite.com" in caplog.text
    assert any(r.levelno == logging.WARNING and ".local" in r.getMessage() for r in caplog.records)
    assert SMTP_PASSWORD not in caplog.text


def test_email_from_overrides_smtp_from():
    with patch.multiple(settings, email_from="Beas Suite <hello@beassuite.com>"):
        assert email_service.from_address() == "Beas Suite <hello@beassuite.com>"


def test_console_mode_sends_nothing(caplog):
    caplog.set_level(logging.DEBUG, logger="crm.email")
    with patch.multiple(settings, smtp_host=""):
        assert email_service.delivery_mode() == "console"
        assert email_service.send_email("shivam@gmail.com", "s", "secret body") == 0
    assert "console mode, nothing sent to s***@gmail.com" in caplog.text
    assert "secret body" not in caplog.text


# ------------------------------------------------------------------ HTTPS providers


def test_resend_payload_and_success(caplog):
    caplog.set_level(logging.DEBUG, logger="crm.email")
    with patch.multiple(settings, email_provider="resend", email_api_key=API_KEY, smtp_host=""), \
            patch("app.services.email_service.http_requests.post", return_value=_Resp(200, {"id": "x"})) as post:
        email_service.send_password_reset("shivam@gmail.com", "S", "https://app/reset?token=TOK")
    url = post.call_args.args[0]
    kwargs = post.call_args.kwargs
    assert url == "https://api.resend.com/emails"
    assert kwargs["timeout"] == 10
    assert kwargs["headers"]["Authorization"] == f"Bearer {API_KEY}"
    assert kwargs["json"]["to"] == ["shivam@gmail.com"]
    assert kwargs["json"]["from"] == "Beas Suite <no-reply@beassuite.com>"
    assert kwargs["json"]["subject"] == "Reset your Beas Suite password"
    assert "html" in kwargs["json"] and "text" in kwargs["json"]
    assert "email: sent to s***@gmail.com via api in" in caplog.text
    assert API_KEY not in caplog.text and "TOK" not in caplog.text


def test_brevo_payload():
    with patch.multiple(settings, email_provider="brevo", email_api_key=API_KEY), \
            patch("app.services.email_service.http_requests.post", return_value=_Resp(201, {"messageId": "m"})) as post:
        email_service.send_email("x@example.com", "subj", "body", "<p>body</p>")
    kwargs = post.call_args.kwargs
    assert post.call_args.args[0] == "https://api.brevo.com/v3/smtp/email"
    assert kwargs["headers"]["api-key"] == API_KEY and kwargs["timeout"] == 10
    assert kwargs["json"]["sender"] == {"email": "no-reply@beassuite.com", "name": "Beas Suite"}
    assert kwargs["json"]["to"] == [{"email": "x@example.com"}]
    assert kwargs["json"]["htmlContent"] == "<p>body</p>"


@pytest.mark.parametrize("provider,response,expected", [
    ("resend", _Resp(403, {"name": "validation_error", "message": "The beassuite.com domain is not verified"}),
     "_ApiError: HTTP 403 The beassuite.com domain is not verified"),
    ("brevo", _Resp(401, {"code": "unauthorized", "message": "Key not found"}), "_ApiError: HTTP 401 Key not found"),
    ("resend", _Resp(500), "_ApiError: HTTP 500"),
])
def test_provider_errors_are_reported(provider, response, expected, caplog):
    caplog.set_level(logging.DEBUG, logger="crm.email")
    with patch.multiple(settings, email_provider=provider, email_api_key=API_KEY), \
            patch("app.services.email_service.http_requests.post", return_value=response), \
            pytest.raises(email_service.EmailError) as caught:
        email_service.send_email("shivam@gmail.com", "s", "b")
    assert str(caught.value) == expected
    assert f"email: FAILED to s***@gmail.com via api: {expected}" in caplog.text
    assert API_KEY not in caplog.text


def test_provider_timeout_and_missing_key():
    with patch.multiple(settings, email_provider="resend", email_api_key=API_KEY), \
            patch("app.services.email_service.http_requests.post", side_effect=requests.Timeout("read timed out")), \
            pytest.raises(email_service.EmailError, match="Timeout: read timed out"):
        email_service.send_email("x@example.com", "s", "b")
    with patch.multiple(settings, email_provider="brevo", email_api_key=""), \
            pytest.raises(email_service.EmailError, match="EMAIL_API_KEY is not configured"):
        email_service.send_email("x@example.com", "s", "b")


# ------------------------------------------------------------------ test endpoint


@pytest.mark.parametrize("kind", ["staff", "admin"])
def test_email_test_endpoint_is_super_admin_only(kind):
    user = _user(kind)
    r = client.post("/superadmin/email/test", json={"to": "x@example.com"}, headers=_auth(user))
    assert r.status_code == 403
    assert client.post("/superadmin/email/test", json={"to": "x@example.com"}).status_code in (401, 403)


def test_email_test_endpoint_reports_success_and_real_error():
    admin = _user("super_admin")
    factory, server = _smtp_server()
    with patch("smtplib.SMTP", factory):
        r = client.post("/superadmin/email/test", json={"to": "x@example.com"}, headers=_auth(admin))
    assert r.status_code == 200
    body = r.json()
    assert body["sent"] is True and body["mode"] == "smtp" and isinstance(body["duration_ms"], int)
    assert body["error"] is None

    with patch.multiple(settings, email_provider="resend", email_api_key=API_KEY), \
            patch("app.services.email_service.http_requests.post",
                  return_value=_Resp(403, {"message": "domain is not verified"})):
        r = client.post("/superadmin/email/test", json={"to": "x@example.com"}, headers=_auth(admin))
    assert r.status_code == 200
    assert r.json() == {"sent": False, "mode": "api", "duration_ms": None,
                        "error": "_ApiError: HTTP 403 domain is not verified"}


def test_email_test_endpoint_console_mode_and_rate_limit():
    admin = _user("super_admin")
    with patch.multiple(settings, smtp_host=""):
        r = client.post("/superadmin/email/test", json={"to": "x@example.com"}, headers=_auth(admin))
        assert r.status_code == 200
        assert r.json()["sent"] is False and r.json()["mode"] == "console"
        for _ in range(4):
            assert client.post("/superadmin/email/test", json={"to": "x@example.com"},
                               headers=_auth(admin)).status_code == 200
        assert client.post("/superadmin/email/test", json={"to": "x@example.com"},
                           headers=_auth(admin)).status_code == 429
    assert client.post("/superadmin/email/test", json={"to": "not-an-email"}, headers=_auth(admin)).status_code == 422


# ------------------------------------------------------------------ forgot-password stays generic


def test_forgot_password_response_identical_and_logs_masked(caplog):
    caplog.set_level(logging.DEBUG)
    user = _user("staff")
    inactive = _user("staff", active=False)
    factory, server = _smtp_server()
    server.send_message.side_effect = smtplib.SMTPRecipientsRefused({user.email: (550, b"5.1.1 mailbox unavailable")})
    with patch("smtplib.SMTP", factory):
        responses = [
            client.post("/auth/forgot-password", json={"email": email})
            for email in (user.email, inactive.email, "nobody_" + uuid.uuid4().hex[:6] + "@example.com")
        ]
    assert [r.status_code for r in responses] == [200, 200, 200]
    assert all(r.json() == {"detail": GENERIC, "reset_token": None} for r in responses)

    masked = email_service.mask_email(user.email)
    assert f"reset: sending via smtp to {masked}" in caplog.text
    assert f"email: FAILED to {masked} via smtp: SMTPRecipientsRefused: 550 5.1.1 mailbox unavailable" in caplog.text
    assert f"reset: no active user for {email_service.mask_email(inactive.email)}" in caplog.text
    for raw in (user.email, inactive.email, SMTP_PASSWORD, "token="):
        assert raw not in caplog.text
