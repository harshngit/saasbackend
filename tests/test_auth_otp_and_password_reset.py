"""Staff mobile OTP login (Twilio Verify), the emailed password-reset link, and changing
the login email (PATCH /users/{id}) then signing in with it.

Twilio is mocked at its HTTP boundary (`otp_service.http_requests.post`) and email at
`send_password_reset`; any call escaping those mocks fails the test, so no SMS or email
is ever sent. Rate-limit events are cleared before each test (every TestClient request
comes from one address), and windows are exercised by ageing stored events.
"""
import importlib.util
import logging
import os
import pathlib
import smtplib
import sys
import threading
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
import requests

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fastapi.testclient import TestClient

from app.core.config import settings
from app.core.database import Base, SessionLocal, auto_add_missing_columns, engine
from app.core.phone import normalize_indian_mobile
from app.core.security import create_access_token, decode_token, hash_password
from app.main import app
from app.models import (
    ActivityLog,
    AuthRateLimitEvent,
    OAuthExchangeTicket,
    Organization,
    PasswordResetToken,
    RefreshToken,
    User,
    UserRole,
)
from app.services import auth_service, org_service, password_service, rate_limit_service

Base.metadata.create_all(bind=engine)
auto_add_missing_columns()

client = TestClient(app)
IS_POSTGRES = engine.dialect.name == "postgresql"

PASSWORD = "Password@123"
TWILIO = {
    "twilio_account_sid": "ACtest0000000000000000000000000000",
    "twilio_auth_token": "secret-auth-token-value",
    "twilio_verify_service_sid": "VAtest000000000000000000000000000",
}
SEND_OK = {"detail": "If this number is registered, an OTP has been sent.", "resend_after": 30}
INVALID_OTP = {"detail": "Invalid or expired OTP"}


# ------------------------------------------------------------------ helpers


def _phone() -> str:
    """A random number libphonenumber accepts as an Indian mobile."""
    while True:
        candidate = "9" + str(uuid.uuid4().int)[:9]
        if normalize_indian_mobile(candidate):
            return candidate


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


def _user(org: Organization | None, kind: str = "staff", phone: str | None = None, active: bool = True,
          email: str | None = None, synced: bool = True) -> User:
    """A user written straight to the DB. `synced=False` leaves staff_phone_e164 empty,
    like a row written before that column existed."""
    role = {"staff": None, "admin": UserRole.ADMIN, "super_admin": UserRole.SUPER_ADMIN}[kind]
    db = SessionLocal()
    try:
        user = User(
            organization_id=org.id if org is not None else None,
            name=f"{kind} {uuid.uuid4().hex[:6]}",
            email=email or f"{kind}_{uuid.uuid4().hex[:10]}@example.com",
            phone=phone,
            staff_phone_e164=normalize_indian_mobile(phone) if kind == "staff" and synced else None,
            password_hash=hash_password(PASSWORD),
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


def _row(user_id: str) -> User:
    db = SessionLocal()
    try:
        return db.get(User, user_id)
    finally:
        db.close()


def _auth(user: User) -> dict:
    return {"Authorization": f"Bearer {create_access_token(user.id, user.system_role, user.organization_id)}"}


def _age_events(by: timedelta) -> None:
    """Move every stored rate-limit event back in time."""
    db = SessionLocal()
    try:
        for event in db.query(AuthRateLimitEvent).all():
            event.created_at = event.created_at - by
        db.commit()
    finally:
        db.close()


class _Resp:
    def __init__(self, status_code: int, body: object = None) -> None:
        self.status_code = status_code
        self._body = body

    def json(self):  # noqa: ANN201
        if self._body is None:
            raise ValueError("no body")
        return self._body


def _twilio(start=None, check=None):
    """Patch Twilio's HTTP boundary. `start` / `check` are responses, exceptions or callables."""
    start = start if start is not None else _Resp(201, {"status": "pending"})

    def fake_post(url, data=None, auth=None, timeout=None):  # noqa: ANN001, ANN202
        result = start if url.endswith("/Verifications") else check
        if isinstance(result, Exception):
            raise result
        if callable(result):
            return result(data)
        return result

    return patch("app.services.otp_service.http_requests.post", side_effect=fake_post)


def _approved(data):  # noqa: ANN001, ANN202
    return _Resp(200, {"status": "approved", "valid": True, "to": data["To"]})


WRONG_CODE = _Resp(200, {"status": "pending", "valid": False})


@pytest.fixture(autouse=True)
def _isolation():
    """Fake Twilio credentials, no SMTP, no exposed tokens, empty rate-limit table -- and
    any real Twilio or SMTP call that escapes the per-test mocks fails the test."""
    db = SessionLocal()
    db.query(AuthRateLimitEvent).delete()
    db.commit()
    db.close()
    with patch.multiple(settings, **TWILIO, client_ip_header="", smtp_host="", email_provider="smtp",
                        expose_reset_token=False), \
            patch("app.services.otp_service.http_requests.post", side_effect=AssertionError("unmocked Twilio call")), \
            patch("smtplib.SMTP", side_effect=AssertionError("unmocked SMTP connection")):
        yield


@pytest.fixture
def no_plan_limits():
    with patch("app.services.entitlement_service.check_limit"), \
            patch("app.services.entitlement_service.check_entitlement"):
        yield


def _send(phone: str, **kw):  # noqa: ANN202
    return client.post("/auth/phone/send-otp", json={"phone": phone}, **kw)


def _verify(phone: str, code: str = "123456", **kw):  # noqa: ANN202
    return client.post("/auth/phone/verify-otp", json={"phone": phone, "code": code}, **kw)


# ------------------------------------------------------------------ phone numbers


@pytest.mark.parametrize("raw", ["9876543210", "+91 98765-43210", "09876543210", "919876543210", "98765 43210",
                                 "+919876543210"])
def test_indian_mobile_spellings_normalize_to_e164(raw):
    assert normalize_indian_mobile(raw) == "+919876543210"


@pytest.mark.parametrize("raw", ["", "   ", None, "12ab", "98765432101", "987654321", "5876543210",
                                 "080 2345 6789", "+14155550100", "+44 7911 123456"])
def test_invalid_landline_and_foreign_numbers_are_rejected(raw):
    assert normalize_indian_mobile(raw) is None


# ------------------------------------------------------------------ OTP login


def test_staff_sends_and_verifies_otp_like_password_login():
    org = _org()
    phone = _phone()
    staff = _user(org, "staff", phone=phone)
    assert _row(staff.id).phone_verified is False and _row(staff.id).last_login is None

    with _twilio(check=_approved) as post:
        r = _send(f"+91 {phone[:5]}-{phone[5:]}")
        assert (r.status_code, r.json()) == (200, SEND_OK)
        sent = post.call_args
        assert sent.args[0].endswith(f"/{TWILIO['twilio_verify_service_sid']}/Verifications")
        assert sent.kwargs["data"] == {"To": f"+91{phone}", "Channel": "sms"}
        assert sent.kwargs["auth"] == (TWILIO["twilio_account_sid"], TWILIO["twilio_auth_token"])
        assert sent.kwargs["timeout"]

        r = _verify(phone, "654321")
    assert r.status_code == 200, r.text
    body = r.json()
    assert post.call_args.kwargs["data"] == {"To": f"+91{phone}", "Code": "654321"}

    # Same session as /auth/login: shape, claims, organization, refresh behaviour
    login = client.post("/auth/login", json={"email": staff.email, "password": PASSWORD}).json()
    assert set(body) == set(login) and set(body["tokens"]) == set(login["tokens"])
    assert body["user"]["id"] == staff.id and body["organization"]["id"] == org.id
    claims = decode_token(body["tokens"]["access_token"])
    assert (claims["sub"], claims["type"], claims["role"], claims["org"]) == (staff.id, "access", "staff", org.id)
    assert client.post("/auth/refresh", json={"refresh_token": body["tokens"]["refresh_token"]}).status_code == 200

    # phone_verified, last_login, the audit event and /auth/me
    row = _row(staff.id)
    assert row.phone_verified is True and row.last_login is not None
    db = SessionLocal()
    try:
        events = db.query(ActivityLog).filter(ActivityLog.user_id == staff.id, ActivityLog.type == "auth").all()
        assert [e.title for e in events] == ["Logged in via phone OTP"]
        assert events[0].organization_id == org.id
    finally:
        db.close()
    me = client.get("/auth/me", headers={"Authorization": f"Bearer {body['tokens']['access_token']}"}).json()
    assert me["phone_verified"] is True and me["user"]["phone_verified"] is True
    assert {"id", "organization_id", "name", "role", "permissions", "full_access", "data_scope", "user",
            "organization", "theme"} <= set(me)


def test_admin_and_super_admin_never_use_otp():
    org = _org()
    for user in (_user(org, "admin", phone=_phone()), _user(None, "super_admin", phone=_phone())):
        with _twilio(check=_approved) as post:
            assert _send(user.phone).json() == SEND_OK
            assert (_verify(user.phone).status_code, _verify(user.phone).json()) == (400, INVALID_OTP)
        post.assert_not_called()
        assert _row(user.id).phone_verified is False
        # Their normal login is untouched
        assert client.post("/auth/login", json={"email": user.email, "password": PASSWORD}).status_code == 200


def test_unregistered_numbers_get_the_generic_answer_and_no_sms():
    db = SessionLocal()
    before = db.query(User).count()
    db.close()
    phone = _phone()
    with _twilio(check=_approved) as post:
        r = _send(phone)
        assert (r.status_code, r.json()) == (200, SEND_OK)
        assert (_verify(phone).status_code, _verify(phone).json()) == (400, INVALID_OTP)
    post.assert_not_called()
    db = SessionLocal()
    assert db.query(User).count() == before
    db.close()


def test_inactive_staff_cannot_use_otp():
    phone = _phone()
    _user(_org(), "staff", phone=phone, active=False)
    with _twilio(check=_approved) as post:
        assert _send(phone).json() == SEND_OK
        assert _verify(phone).status_code == 400
    post.assert_not_called()


def test_ambiguous_legacy_numbers_never_send_or_authenticate():
    phone = _phone()
    # Written before staff_phone_e164 existed, in two spellings, in two firms
    _user(_org(), "staff", phone=phone, synced=False)
    _user(_org(), "staff", phone=f"+91 ({phone[:5]}) {phone[5:]}", synced=False)
    with _twilio(check=_approved) as post:
        assert _send(phone).json() == SEND_OK
        assert _verify(phone).status_code == 400
    post.assert_not_called()

    # An unambiguous legacy row works, and gets its canonical number filled in
    lone = _phone()
    legacy = _user(_org(), "staff", phone=f"0{lone}", synced=False)
    with _twilio(check=_approved) as post:
        _send(lone)
        assert post.call_count == 1
        assert _verify(lone).status_code == 200
    assert _row(legacy.id).staff_phone_e164 == f"+91{lone}"


def test_shared_admin_number_does_not_block_staff(no_plan_limits):
    phone = _phone()
    _user(_org(), "admin", phone=phone)
    _user(_org(), "admin", phone=phone)  # admins may share
    org = _org()
    admin = _user(org, "admin", phone=phone)
    r = client.post("/users", headers=_auth(admin), json={
        "contact_information": {"official_email": f"s_{uuid.uuid4().hex[:8]}@example.com", "mobile_number": phone},
        "login_security": {"password": PASSWORD},
    })
    assert r.status_code == 201, r.text
    with _twilio(check=_approved) as post:
        _send(phone)
        assert post.call_count == 1  # the staff account, not the admins
        body = _verify(phone).json()
    assert body["user"]["id"] == r.json()["id"]


def test_staff_numbers_unique_across_firms_on_create_and_update(no_plan_limits):
    phone, other = _phone(), _phone()
    org_a, org_b = _org(), _org()
    admin_a, admin_b = _user(org_a, "admin"), _user(org_b, "admin")

    def create(admin, mobile):  # noqa: ANN001, ANN202
        return client.post("/users", headers=_auth(admin), json={
            "contact_information": {"official_email": f"s_{uuid.uuid4().hex[:8]}@example.com", "mobile_number": mobile},
            "login_security": {"password": PASSWORD},
        })

    first = create(admin_a, phone)
    assert first.status_code == 201, first.text
    for spelling in (phone, f"+91{phone}", f"0{phone}", f"+91 {phone[:5]} {phone[5:]}", f"+91-{phone}"):
        dup = create(admin_b, spelling)
        assert dup.status_code == 409, spelling
        assert dup.json()["detail"] == "This mobile number is already assigned to another staff account"

    second = create(admin_b, other)
    assert second.status_code == 201
    staff_b = second.json()["id"]
    r = client.patch(f"/users/{staff_b}", headers=_auth(admin_b),
                     json={"contact_information": {"mobile_number": f"+91-{phone}"}})
    assert r.status_code == 409
    # Unrelated edits and keeping one's own number are fine
    assert client.patch(f"/users/{staff_b}", headers=_auth(admin_b),
                        json={"basic_information": {"first_name": "Renamed"}}).status_code == 200
    assert client.patch(f"/users/{staff_b}", headers=_auth(admin_b),
                        json={"contact_information": {"mobile_number": f"0{other}"}}).status_code == 200
    row = _row(staff_b)
    assert row.phone == f"0{other}"  # stored as entered, never rewritten
    assert row.staff_phone_e164 == f"+91{other}"

    # A legacy duplicate can still be edited, as long as its number is not touched
    legacy_phone = _phone()
    org_c = _org()
    admin_c = _user(org_c, "admin")
    legacy = _user(org_c, "staff", phone=legacy_phone, synced=False)
    _user(_org(), "staff", phone=legacy_phone, synced=False)
    assert client.patch(f"/users/{legacy.id}", headers=_auth(admin_c),
                        json={"basic_information": {"first_name": "Still editable"}}).status_code == 200
    assert client.patch(f"/users/{legacy.id}", headers=_auth(admin_c),
                        json={"contact_information": {"mobile_number": legacy_phone}}).status_code == 409


def test_changing_the_staff_number_clears_phone_verified(no_plan_limits):
    org = _org()
    admin = _user(org, "admin")
    phone = _phone()
    staff = _user(org, "staff", phone=phone)
    with _twilio(check=_approved):
        _send(phone)
        assert _verify(phone).status_code == 200
    assert _row(staff.id).phone_verified is True
    assert client.patch(f"/users/{staff.id}", headers=_auth(admin),
                        json={"contact_information": {"mobile_number": f"+91{phone}"}}).status_code == 200
    assert _row(staff.id).phone_verified is True  # same number, another spelling
    assert client.patch(f"/users/{staff.id}", headers=_auth(admin),
                        json={"contact_information": {"mobile_number": _phone()}}).status_code == 200
    assert _row(staff.id).phone_verified is False


def test_wrong_expired_used_or_malformed_codes_answer_400_not_401():
    phone = _phone()
    _user(_org(), "staff", phone=phone)
    cases = [
        WRONG_CODE,
        _Resp(404, {"code": 20404}),  # expired / already used / never requested
        _Resp(400, {"code": 60200}),  # invalid parameter
        _Resp(429, {"code": 60202}),  # Twilio's own max check attempts
        _Resp(200, {"status": "approved", "valid": True, "to": "+919999999999"}),  # another number
    ]
    for response in cases:
        with _twilio(check=response):
            r = _verify(phone)
        assert (r.status_code, r.json()) == (400, INVALID_OTP)
        _age_events(timedelta(minutes=16))  # keep under the failure limit
    with _twilio(check=_approved) as post:
        for code in ("12345", "1234567", "12ab56", " 123456", ""):
            assert (_verify(phone, code).status_code, _verify(phone, code).json()) == (400, INVALID_OTP)
        assert _send("12345").status_code == 422
        assert _send("+14155550100").status_code == 422
    post.assert_not_called()


def test_twilio_unconfigured_fails_closed_and_other_logins_still_work():
    phone = _phone()
    staff = _user(_org(), "staff", phone=phone)
    for blank in ("twilio_account_sid", "twilio_auth_token", "twilio_verify_service_sid"):
        with patch.multiple(settings, **{blank: ""}), _twilio(check=_approved) as post:
            for r in (_send(phone), _verify(phone)):
                assert (r.status_code, r.json()) == (503, {"detail": "Phone login not configured"})
        post.assert_not_called()
    with patch.multiple(settings, twilio_account_sid="", twilio_auth_token="", twilio_verify_service_sid=""):
        assert client.post("/auth/login", json={"email": staff.email, "password": PASSWORD}).status_code == 200


def test_provider_errors_map_to_the_agreed_statuses():
    phone = _phone()
    _user(_org(), "staff", phone=phone)
    send_cases = [
        (_Resp(429, {"code": 60203, "message": "Max send attempts reached for +91..."}), 429),
        (_Resp(400, {"code": 60200, "message": "Invalid parameter"}), 400),
        (_Resp(500, {"code": 20500}), 502),
        (_Resp(503, None), 502),
        (_Resp(201, {"status": "canceled"}), 502),
        (requests.Timeout("slow"), 502),
        (requests.ConnectionError("down"), 502),
    ]
    for failure, expected in send_cases:
        _age_events(timedelta(days=3))
        with _twilio(start=failure):
            r = _send(phone)
        assert r.status_code == expected, failure
        assert "+91" not in r.text and "Max send" not in r.text and "secret" not in r.text
    verify_cases = [(_Resp(429, {"code": 60203}), 429), (_Resp(500, {}), 502),
                    (requests.Timeout("slow"), 502), (_Resp(503, None), 502)]
    for failure, expected in verify_cases:
        with _twilio(check=failure):
            r = _verify(phone)
        assert r.status_code == expected and "tokens" not in r.text
    # Provider trouble is not the user's failed attempt
    db = SessionLocal()
    try:
        assert db.query(AuthRateLimitEvent).filter(AuthRateLimitEvent.scope == "otp_verify_failure_phone").count() == 0
    finally:
        db.close()


def test_send_limits_resend_and_ip():
    phone = _phone()
    _user(_org(), "staff", phone=phone)
    unknown = _phone()
    with _twilio() as post:
        for number in (phone, unknown):  # identical answers, registered or not
            assert (_send(number).status_code, _send(number).status_code) == (200, 429)
        assert _send(phone).json() == {"detail": "Too many attempts. Please try again later."}
        assert post.call_count == 1

        # 5 per 15 minutes
        for _ in range(3):
            _age_events(timedelta(seconds=31))
            assert _send(phone).status_code == 200
        _age_events(timedelta(seconds=31))
        assert _send(phone).status_code == 200  # fifth
        _age_events(timedelta(seconds=31))
        assert _send(phone).status_code == 429  # sixth inside 15 min
        # 10 per day
        _age_events(timedelta(minutes=16))
        for _ in range(5):
            assert _send(phone).status_code == 200
            _age_events(timedelta(seconds=31))
        _age_events(timedelta(minutes=16))
        assert _send(phone).status_code == 429  # eleventh in a day
        _age_events(timedelta(days=1, minutes=1))
        assert _send(phone).status_code == 200

    # 30 per IP per hour, using the configured proxy header
    with patch.multiple(settings, client_ip_header="CF-Connecting-IP"), _twilio():
        ip_a = {"CF-Connecting-IP": "203.0.113.9"}
        assert [_send(_phone(), headers=ip_a).status_code for _ in range(31)] == [200] * 30 + [429]
        assert _send(_phone(), headers={"CF-Connecting-IP": "198.51.100.7"}).status_code == 200


def test_sixth_failed_verification_is_429_and_successes_do_not_count():
    phone = _phone()
    _user(_org(), "staff", phone=phone)
    with _twilio(check=_approved):
        for _ in range(4):
            assert _verify(phone).status_code == 200  # successes are not failures
    with _twilio(check=WRONG_CODE):
        assert [_verify(phone).status_code for _ in range(5)] == [400] * 5
    with _twilio(check=_approved) as post:
        assert _verify(phone).status_code == 429  # sixth, even with a right code
        post.assert_not_called()
        _age_events(timedelta(minutes=16))
        assert _verify(phone).status_code == 200


def test_rate_limit_events_are_pruned_after_two_days():
    db = SessionLocal()
    try:
        now = datetime.now(timezone.utc)
        db.add_all([
            AuthRateLimitEvent(scope="old", key_hash="a" * 64, created_at=now - timedelta(days=2, minutes=1)),
            AuthRateLimitEvent(scope="recent", key_hash="b" * 64, created_at=now - timedelta(days=1, hours=23)),
        ])
        db.commit()
        rate_limit_service.record(db, "new", "key")
        db.commit()
        assert {e.scope for e in db.query(AuthRateLimitEvent).all()} == {"recent", "new"}
        stored = db.query(AuthRateLimitEvent).filter(AuthRateLimitEvent.scope == "new").one()
        assert stored.key_hash != "key" and len(stored.key_hash) == 64
    finally:
        db.close()


@pytest.mark.skipif(not IS_POSTGRES, reason="advisory locks serialize concurrent requests on PostgreSQL only")
def test_concurrent_sends_for_one_number_cannot_bypass_the_limit():
    import time

    phone = _phone()
    _user(_org(), "staff", phone=phone)
    results: list[int] = []
    barrier = threading.Barrier(6)
    real_exceeded = rate_limit_service.exceeded

    def slow_exceeded(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        # Widen the gap between counting and recording, so an unserialized check
        # would let every request see the same zero count.
        verdict = real_exceeded(*args, **kwargs)
        time.sleep(0.3)
        return verdict

    def worker():  # noqa: ANN202
        barrier.wait()
        results.append(_send(phone).status_code)

    with _twilio(), patch.object(rate_limit_service, "exceeded", slow_exceeded):
        threads = [threading.Thread(target=worker) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    assert sorted(results) == [200] + [429] * 5


def test_otp_codes_secrets_tokens_and_numbers_are_not_logged(caplog):
    phone = _phone()
    _user(_org(), "staff", phone=phone)
    caplog.set_level(logging.DEBUG)
    with _twilio(check=_approved):
        _send(phone)
        tokens = _verify(phone, "987123").json()["tokens"]
    _age_events(timedelta(minutes=1))
    with _twilio(start=_Resp(429, {"code": 60203, "message": f"limit for +91{phone}"}), check=_Resp(500, {})):
        _send(phone)
        _verify(phone, "987124")
    with _twilio(start=requests.Timeout("x")):
        _age_events(timedelta(minutes=1))
        _send(phone)
    text = caplog.text
    for secret in ("987123", "987124", phone, TWILIO["twilio_auth_token"], tokens["access_token"],
                   tokens["refresh_token"]):
        assert secret not in text


def test_password_login_unchanged_and_records_last_login():
    staff = _user(_org(), "staff", phone=_phone())
    r = client.post("/auth/login", json={"email": staff.email, "password": PASSWORD})
    assert r.status_code == 200 and r.json()["user"]["id"] == staff.id
    assert _row(staff.id).last_login is not None and _row(staff.id).phone_verified is False
    assert client.post("/auth/login", json={"email": staff.email, "password": "wrong-password"}).status_code == 401


# ------------------------------------------------------------------ password reset


def _forgot(email: str, **kw):  # noqa: ANN202
    with patch("app.routers.auth.send_password_reset") as send:
        r = client.post("/auth/forgot-password", json={"email": email}, **kw)
    return r, send


def _token_from(send) -> str:  # noqa: ANN001
    link = send.call_args.kwargs["reset_link"]
    assert link.startswith(settings.frontend_reset_url)
    return link.split("token=", 1)[1]


def test_forgot_password_answers_alike_for_registered_and_unknown_emails():
    user = _user(_org(), "admin", email=f"Mixed.Case_{uuid.uuid4().hex[:6]}@Example.com")
    registered, send = _forgot(user.email.lower())  # the reset form lowercases
    unknown, send_unknown = _forgot(f"nobody_{uuid.uuid4().hex[:8]}@example.com")
    assert (registered.status_code, registered.json()) == (unknown.status_code, unknown.json())
    assert registered.json()["reset_token"] is None
    assert send.call_count == 1 and send.call_args.kwargs["to"] == user.email
    send_unknown.assert_not_called()
    inactive = _user(_org(), "staff", active=False)
    r, send_inactive = _forgot(inactive.email)
    assert r.json() == unknown.json()
    send_inactive.assert_not_called()


def test_forgot_password_is_rate_limited_without_revealing_accounts():
    user = _user(_org(), "staff")
    unknown = f"nobody_{uuid.uuid4().hex[:8]}@example.com"
    for email in (user.email, unknown):
        statuses = [_forgot(email)[0].status_code for _ in range(4)]
        assert statuses == [200, 200, 200, 429], email
    assert _forgot(user.email)[0].json() == {"detail": "Too many attempts. Please try again later."}
    _age_events(timedelta(minutes=16))
    r, send = _forgot(user.email)
    assert r.status_code == 200 and send.call_count == 1

    with patch.multiple(settings, client_ip_header="CF-Connecting-IP"):
        ip = {"CF-Connecting-IP": "192.0.2.44"}
        statuses = [_forgot(f"x{i}_{uuid.uuid4().hex[:6]}@example.com", headers=ip)[0].status_code for i in range(11)]
        assert statuses == [200] * 10 + [429]


def test_reset_token_is_random_hashed_single_use_and_expires():
    user = _user(_org(), "staff")
    _, send = _forgot(user.email)
    token = _token_from(send)
    assert len(token) >= 43
    db = SessionLocal()
    try:
        rows = db.query(PasswordResetToken).filter(PasswordResetToken.user_id == user.id).all()
        assert len(rows) == 1 and rows[0].token_hash != token and len(rows[0].token_hash) == 64
        assert db.query(PasswordResetToken).filter(PasswordResetToken.token_hash == token).count() == 0
    finally:
        db.close()

    ok = client.post("/auth/reset-password", json={"token": token, "new_password": "NewPass@123"})
    assert ok.status_code == 200
    again = client.post("/auth/reset-password", json={"token": token, "new_password": "Other@1234"})
    assert again.status_code == 400

    # Expired (timezone-safe: holds on a PostgreSQL session that is not UTC too)
    _, send = _forgot(user.email)
    expired = _token_from(send)
    db = SessionLocal()
    try:
        db.query(PasswordResetToken).filter(PasswordResetToken.user_id == user.id, PasswordResetToken.used.is_(False)) \
            .update({PasswordResetToken.expires_at: datetime.now(timezone.utc) - timedelta(minutes=1)})
        db.commit()
    finally:
        db.close()
    assert client.post("/auth/reset-password", json={"token": expired, "new_password": "NewPass@123"}).status_code == 400
    assert client.post("/auth/reset-password", json={"token": "made-up", "new_password": "NewPass@123"}).status_code == 400

    # A newer link supersedes an older one (past the 3-per-15-minutes request limit first)
    _age_events(timedelta(minutes=16))
    _, send = _forgot(user.email)
    older = _token_from(send)
    _, send = _forgot(user.email)
    newer = _token_from(send)
    assert client.post("/auth/reset-password", json={"token": older, "new_password": "NewPass@123"}).status_code == 400
    assert client.post("/auth/reset-password", json={"token": newer, "new_password": "NewPass@123"}).status_code == 200


def test_token_consumption_has_a_single_winner():
    user = _user(_org(), "staff")
    db = SessionLocal()
    try:
        raw = password_service.create_reset_token(db, user)
        assert password_service.consume_reset_token(db, raw).id == user.id
        assert password_service.consume_reset_token(db, raw) is None
    finally:
        db.close()


def test_reset_changes_only_that_password_revokes_sessions_and_does_not_sign_in():
    org = _org()
    user, bystander = _user(org, "staff"), _user(org, "staff")
    session = client.post("/auth/login", json={"email": user.email, "password": PASSWORD}).json()["tokens"]

    _, send = _forgot(user.email)
    r = client.post("/auth/reset-password", json={"token": _token_from(send), "new_password": "Brand@New1",
                                                 "confirm_password": "Brand@New1"})
    assert r.status_code == 200 and "tokens" not in r.json()

    assert client.post("/auth/login", json={"email": user.email, "password": PASSWORD}).status_code == 401
    assert client.post("/auth/login", json={"email": user.email, "password": "Brand@New1"}).status_code == 200
    assert client.post("/auth/refresh", json={"refresh_token": session["refresh_token"]}).status_code == 401
    assert client.post("/auth/login", json={"email": bystander.email, "password": PASSWORD}).status_code == 200
    db = SessionLocal()
    try:
        assert db.query(RefreshToken).filter(RefreshToken.user_id == bystander.id, RefreshToken.revoked.is_(True)).count() == 0
    finally:
        db.close()


def test_reset_validation_and_ineligible_accounts():
    user = _user(_org(), "staff")
    _, send = _forgot(user.email)
    token = _token_from(send)
    mismatch = client.post("/auth/reset-password", json={"token": token, "new_password": "Brand@New1",
                                                        "confirm_password": "Different1"})
    assert mismatch.status_code == 422
    assert client.post("/auth/reset-password", json={"token": token, "new_password": "short"}).status_code == 422
    db = SessionLocal()
    try:
        db.get(User, user.id).is_active = False
        db.commit()
    finally:
        db.close()
    # Deactivated in the meantime: refused, password unchanged
    assert client.post("/auth/reset-password", json={"token": token, "new_password": "Brand@New1"}).status_code == 400
    db = SessionLocal()
    try:
        db.get(User, user.id).is_active = True
        db.commit()
    finally:
        db.close()
    assert client.post("/auth/login", json={"email": user.email, "password": PASSWORD}).status_code == 200


def test_direct_reset_by_email_alone_is_gone():
    user = _user(_org(), "admin")
    r = client.post("/auth/reset-password-direct", json={"email": user.email, "new_password": "Hijacked@123"})
    assert r.status_code in (404, 405)
    assert client.post("/auth/check-email", json={"email": user.email}).status_code in (404, 405)
    assert client.post("/auth/login", json={"email": user.email, "password": PASSWORD}).status_code == 200
    assert client.post("/auth/login", json={"email": user.email, "password": "Hijacked@123"}).status_code == 401


def test_email_failure_answers_generically_and_logs_no_secret(caplog):
    user = _user(_org(), "staff")
    caplog.set_level(logging.DEBUG)
    with patch("app.routers.auth.send_password_reset", side_effect=smtplib.SMTPException("relay down")), \
            patch.multiple(settings, expose_reset_token=True):
        r = client.post("/auth/forgot-password", json={"email": user.email})
    assert r.status_code == 200
    assert r.json()["detail"] == "If that email is registered, a reset link has been sent."
    assert r.json()["reset_token"] not in caplog.text

    # Console mode (no SMTP host) says an email would be sent, never what is in it
    caplog.clear()
    with patch.multiple(settings, smtp_host="", email_provider="smtp", expose_reset_token=True):
        token = client.post("/auth/forgot-password", json={"email": user.email}).json()["reset_token"]
    assert "console mode, nothing sent" in caplog.text and token not in caplog.text
    assert user.email not in caplog.text


# ------------------------------------------------------------------ expiry across time zones


def test_expired_refresh_tokens_and_oauth_tickets_are_rejected():
    """Expiry checks convert stored timestamps instead of relabelling them as UTC, which
    on a non-UTC PostgreSQL session let expired credentials through for hours."""
    user = _user(_org(), "admin")
    tokens = client.post("/auth/login", json={"email": user.email, "password": PASSWORD}).json()["tokens"]
    db = SessionLocal()
    try:
        db.query(RefreshToken).filter(RefreshToken.user_id == user.id).update(
            {RefreshToken.expires_at: datetime.now(timezone.utc) - timedelta(minutes=1)})
        db.commit()
        assert auth_service.get_active_refresh(db, tokens["refresh_token"]) is None
    finally:
        db.close()
    assert client.post("/auth/refresh", json={"refresh_token": tokens["refresh_token"]}).status_code == 401

    db = SessionLocal()
    try:
        raw = auth_service.create_exchange_ticket(db, user.id)
        db.query(OAuthExchangeTicket).filter(OAuthExchangeTicket.user_id == user.id).update(
            {OAuthExchangeTicket.expires_at: datetime.now(timezone.utc) - timedelta(seconds=5)})
        db.commit()
        assert auth_service.consume_exchange_ticket(db, raw) is None
        fresh = auth_service.create_exchange_ticket(db, user.id)
        assert auth_service.consume_exchange_ticket(db, fresh).id == user.id
    finally:
        db.close()


# ------------------------------------------------------------------ migration


def test_migration_backfills_unambiguous_staff_numbers_only(tmp_path):
    import sqlalchemy as sa
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    path = pathlib.Path(__file__).resolve().parents[1] / "alembic" / "versions" / "f5a6b7c8d9e0_staff_phone_otp_and_auth_rate_limits.py"
    spec = importlib.util.spec_from_file_location("otp_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    mig_engine = sa.create_engine(f"sqlite:///{tmp_path / 'otp.db'}")
    with mig_engine.begin() as conn:
        conn.execute(sa.text('CREATE TABLE users (id VARCHAR(36) PRIMARY KEY, phone VARCHAR(20), '
                             'system_role VARCHAR(20), role VARCHAR(20))'))
        rows = [
            ("unique-staff", "98765 43210", "staff", None),
            ("dup-a", "9123456789", "staff", None),
            ("dup-b", "+919123456789", "staff", None),
            ("admin-shares", "9876543210", "admin", "ADMIN"),
            ("legacy-staff", "09988776655", None, "SALES_OFFICER"),
            ("legacy-admin", "9988776611", None, "ADMIN"),
            ("not-indian", "+14155550100", "staff", None),
            ("landline", "080 2345 6789", "staff", None),
        ]
        for row in rows:
            conn.execute(sa.text("INSERT INTO users VALUES (:i, :p, :s, :r)"), dict(zip("ipsr", row)))

    def run(step):  # noqa: ANN001, ANN202
        with mig_engine.begin() as conn:
            with Operations.context(MigrationContext.configure(conn)):
                step()

    def values() -> dict:
        with mig_engine.connect() as conn:
            return {r[0]: (r[1], r[2], r[3]) for r in conn.execute(
                sa.text("SELECT id, staff_phone_e164, phone_verified, last_login FROM users")).fetchall()}

    run(migration.upgrade)
    expected = {
        "unique-staff": ("+919876543210", 0, None), "dup-a": (None, 0, None), "dup-b": (None, 0, None),
        "admin-shares": (None, 0, None), "legacy-staff": ("+919988776655", 0, None),
        "legacy-admin": (None, 0, None), "not-indian": (None, 0, None), "landline": (None, 0, None),
    }
    assert values() == expected
    inspector = sa.inspect(mig_engine)
    assert "auth_rate_limit_events" in inspector.get_table_names()
    assert any(ix["name"] == "ix_users_staff_phone_e164" and ix["unique"] for ix in inspector.get_indexes("users"))
    with mig_engine.connect() as conn:
        assert conn.execute(sa.text("SELECT phone FROM users WHERE id = 'unique-staff'")).scalar() == "98765 43210"

    run(migration.upgrade)  # idempotent
    assert values() == expected
    run(migration.downgrade)
    inspector = sa.inspect(mig_engine)
    assert "auth_rate_limit_events" not in inspector.get_table_names()
    assert not any(ix["name"] == "ix_users_staff_phone_e164" for ix in inspector.get_indexes("users"))
    run(migration.upgrade)  # and back up again
    assert values() == expected
    mig_engine.dispose()


# ------------------------------------------------------------------ changing the login email


def _login(email: str, password: str = PASSWORD):  # noqa: ANN202
    return client.post("/auth/login", json={"email": email, "password": password})


def _set_email(admin: User, target_id: str, body: dict, **kw):  # noqa: ANN202
    return client.patch(f"/users/{target_id}", json=body, headers=_auth(admin), **kw)


def test_email_change_moves_login_to_the_new_address():
    """The reported bug: admin@demo.com renamed in the profile, then the new address did not
    sign in (it was stored with the casing typed, login matched case-sensitively)."""
    admin = _user(_org(), "admin", email=f"admin_{uuid.uuid4().hex[:8]}@demo.com")
    before = _row(admin.id)
    new = f"Shivam.Wadkar_{uuid.uuid4().hex[:6]}@Gmail.COM"
    r = _set_email(admin, admin.id, {"contact_information": {"official_email": f"  {new} "}})
    assert r.status_code == 200, r.text
    assert r.json()["contact_information"]["official_email"] == new.lower()

    after = _row(admin.id)
    assert after.email == new.lower()  # stored lowercased, like registration and Google sign-in
    assert (after.password_hash, after.role, after.system_role, after.organization_id, after.name) == \
        (before.password_hash, before.role, before.system_role, before.organization_id, before.name)

    # No email-verification policy exists, so the new address is the login at once.
    for spelling in (new.lower(), new, new.upper()):
        assert _login(spelling).status_code == 200, spelling
    # The old one is gone, answered exactly like an address that never existed.
    old, unknown = _login(admin.email), _login(f"nobody_{uuid.uuid4().hex[:8]}@demo.com")
    assert old.status_code == unknown.status_code == 401 and old.json() == unknown.json()
    # Tokens carry the user id, not the email: the open session keeps working.
    assert client.get("/auth/me", headers=_auth(admin)).status_code == 200


def test_email_inside_a_section_by_its_column_name_is_applied_not_dropped():
    admin = _user(_org(), "admin")
    new = f"col_{uuid.uuid4().hex[:8]}@example.com"
    assert _set_email(admin, admin.id, {"contact_information": {"email": new}}).status_code == 200
    assert _row(admin.id).email == new and _login(new).status_code == 200


def test_email_already_used_by_another_account_is_409_in_any_casing():
    org = _org()
    admin, other = _user(org, "admin"), _user(_org(), "staff")
    for taken in (other.email, other.email.upper(), f" {other.email.title()} "):
        r = _set_email(admin, admin.id, {"official_email": taken})
        assert r.status_code == 409 and r.json()["detail"] == "Email already registered", taken
    assert _row(admin.id).email == admin.email
    assert _login(admin.email).status_code == 200


def test_email_race_lost_at_commit_is_409_and_saves_nothing():
    """Two updates checked the same free address at once: the second commit hits
    uq_user_org_email and must answer 409 with nothing written, not 500."""
    org = _org()
    admin, colleague = _user(org, "admin"), _user(org, "staff")
    with patch("app.routers.users._email_taken", return_value=False):
        r = _set_email(admin, admin.id, {"official_email": colleague.email, "first_name": "Changed"})
    assert r.status_code == 409 and r.json()["detail"] == "Email already registered"
    row = _row(admin.id)
    assert row.email == admin.email and row.first_name != "Changed"


def test_only_the_firm_admin_can_change_an_account_email():
    org = _org()
    admin, staff = _user(org, "admin"), _user(org, "staff")
    outsider = _user(_org(), "admin")
    grab = {"official_email": f"grab_{uuid.uuid4().hex[:8]}@example.com"}
    assert client.patch(f"/users/{admin.id}", json=grab).status_code in (401, 403)  # no token
    assert _set_email(staff, admin.id, grab).status_code == 403
    assert _set_email(outsider, admin.id, grab).status_code == 404
    # Self-service is personal details only: the login email is not one of them.
    assert client.patch("/auth/me", json={"email": grab["official_email"]}, headers=_auth(staff)).status_code == 422
    assert _row(admin.id).email == admin.email and _row(staff.id).email == staff.email


def test_rows_stored_with_mixed_case_still_sign_in():
    """Emails saved before they were lowercased keep their casing; login matches them
    case-insensitively."""
    legacy = _user(_org(), "staff", email=f"Legacy.{uuid.uuid4().hex[:8]}@Example.com")
    for spelling in (legacy.email, legacy.email.lower(), legacy.email.upper()):
        assert _login(spelling).status_code == 200, spelling


def test_email_change_keeps_existing_sessions():
    """Documented decision: tokens carry the user id, not the email, so changing the
    email signs nobody out — the access and refresh tokens issued before keep working."""
    admin = _user(_org(), "admin")
    tokens = _login(admin.email).json()["tokens"]
    new = f"moved_{uuid.uuid4().hex[:8]}@example.com"
    assert _set_email(admin, admin.id, {"official_email": new}).status_code == 200
    me = client.get("/auth/me", headers={"Authorization": f"Bearer {tokens['access_token']}"})
    assert me.status_code == 200 and me.json()["user"]["email"] == new
    assert client.post("/auth/refresh", json={"refresh_token": tokens["refresh_token"]}).status_code == 200


# ---- legacy duplicates: a database the unique index could not be built over


@pytest.fixture
def legacy_db(tmp_path):
    """A scratch SQLite copy of the schema WITHOUT ux_users_email_ci, so rows sharing an
    email can exist, served to the app in place of the real database."""
    import sqlalchemy as sa
    from sqlalchemy.orm import sessionmaker

    from app.core.database import get_db

    legacy = sa.create_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
    Base.metadata.create_all(bind=legacy)
    with legacy.begin() as conn:
        conn.execute(sa.text("DROP INDEX ux_users_email_ci"))
    make = sessionmaker(bind=legacy, autoflush=False)

    def _db():  # noqa: ANN202
        db = make()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _db
    try:
        yield make
    finally:
        app.dependency_overrides.pop(get_db, None)
        legacy.dispose()


def _legacy_user(make, email: str, org: str | None = None) -> str:  # noqa: ANN001
    db = make()
    try:
        user = User(organization_id=org, name="Legacy", email=email, password_hash=hash_password(PASSWORD),
                    system_role="staff" if org else "super_admin", role=None if org else UserRole.SUPER_ADMIN)
        db.add(user)
        db.commit()
        return user.id
    finally:
        db.close()


def test_login_refuses_exact_and_case_only_duplicates_like_an_unknown_email(legacy_db):
    from app.routers.auth import _user_by_login_email

    _legacy_user(legacy_db, "same@example.com", "firm-a")
    _legacy_user(legacy_db, "same@example.com", "firm-b")          # exact duplicate, two firms
    _legacy_user(legacy_db, "Case@Example.com", "firm-a")
    _legacy_user(legacy_db, "case@example.com", None)              # case-only, firm vs platform
    unique = _legacy_user(legacy_db, "Only.One@Example.com", "firm-c")

    unknown = _login("nobody-here@example.com")
    assert unknown.status_code == 401
    for spelling in ("same@example.com", "SAME@example.com", "case@example.com", "Case@Example.com"):
        r = _login(spelling)
        assert (r.status_code, r.json()) == (401, unknown.json()), spelling  # reveals nothing
    db = legacy_db()
    try:
        assert _user_by_login_email(db, "same@example.com") is None
        assert _user_by_login_email(db, " ONLY.ONE@example.com ").id == unique
    finally:
        db.close()


def test_duplicate_email_index_log_names_counts_never_addresses(tmp_path, caplog, monkeypatch):
    import sqlalchemy as sa
    from sqlalchemy.orm import Session as OrmSession

    from app.core import database

    legacy = sa.create_engine(f"sqlite:///{tmp_path / 'log.db'}")
    Base.metadata.create_all(bind=legacy)
    with legacy.begin() as conn:
        conn.execute(sa.text("DROP INDEX ux_users_email_ci"))
    with OrmSession(legacy) as db:
        for email in ("Secret.Person@Example.com", "secret.person@example.com", "Other.Person@corp.example",
                      "other.person@corp.example", "other.PERSON@corp.example"):
            db.add(User(name="x", email=email, password_hash="x", system_role="staff"))
        db.commit()
    monkeypatch.setattr(database, "engine", legacy)

    with caplog.at_level(logging.INFO, logger="crm.db"):
        database.enforce_unique_email()
    text_logged = caplog.text.lower()
    assert "2 email(s) are shared by 5 accounts" in text_logged
    assert "secret" not in text_logged and "person" not in text_logged and "corp.example" not in text_logged
    assert "app.scripts.email_conflicts" in text_logged

    with legacy.begin() as conn:  # resolved by the owner -> the next boot builds the index
        conn.execute(sa.text("DELETE FROM users WHERE email <> LOWER(email)"))
    database.enforce_unique_email()
    with legacy.connect() as conn:
        assert conn.execute(sa.text("SELECT COUNT(*) FROM sqlite_master WHERE name = 'ux_users_email_ci'")).scalar() == 1
    legacy.dispose()


def test_duplicate_email_detection_ignores_other_constraints():
    from sqlalchemy.exc import IntegrityError

    from app.core.database import commit_or_email_conflict, is_duplicate_email_error

    def err(message: str) -> IntegrityError:
        return IntegrityError("INSERT", {}, Exception(message))

    assert is_duplicate_email_error(err("UNIQUE constraint failed: index 'ux_users_email_ci'"))
    assert is_duplicate_email_error(err('duplicate key value violates unique constraint "ux_users_email_ci"'))
    assert is_duplicate_email_error(err("UNIQUE constraint failed: users.organization_id, users.email"))
    assert is_duplicate_email_error(err('duplicate key value violates unique constraint "uq_user_org_email"'))
    assert not is_duplicate_email_error(err("UNIQUE constraint failed: users.employee_id"))
    assert not is_duplicate_email_error(err('null value in column "personal_email" violates not-null constraint'))

    class _Db:
        rolled_back = False

        def commit(self):  # noqa: ANN202
            raise err("UNIQUE constraint failed: users.username")

        def rollback(self):  # noqa: ANN202
            self.rolled_back = True

    db = _Db()
    with pytest.raises(IntegrityError):  # re-raised as is, not turned into a 409
        commit_or_email_conflict(db)
    assert db.rolled_back


# ---- races, decided by the real unique index


def _index_present() -> bool:
    import sqlalchemy as sa

    with engine.connect() as conn:
        if IS_POSTGRES:
            query = ("SELECT COUNT(*) FROM pg_indexes WHERE schemaname = current_schema() "
                     "AND indexname = 'ux_users_email_ci'")
        else:
            query = "SELECT COUNT(*) FROM sqlite_master WHERE type = 'index' AND name = 'ux_users_email_ci'"
        return bool(conn.execute(sa.text(query)).scalar())


@pytest.fixture
def email_index():
    """These tests need ux_users_email_ci itself: a fresh test database has it; one carrying
    legacy duplicate emails cannot, and the tests are skipped there."""
    if not _index_present():
        from app.core.database import enforce_unique_email
        enforce_unique_email()
    if not _index_present():
        pytest.skip("this database holds duplicate emails, so ux_users_email_ci cannot exist; use a fresh one")


def _rows_with(email: str) -> int:
    from sqlalchemy import func as sa_func

    db = SessionLocal()
    try:
        return db.query(User).filter(sa_func.lower(User.email) == email.lower()).count()
    finally:
        db.close()


def test_the_index_rejects_case_variants_across_firms_and_the_platform(email_index):
    from sqlalchemy.exc import IntegrityError

    email = f"Indexed.{uuid.uuid4().hex[:8]}@example.com"
    _user(_org(), "staff", email=email)
    for org in (_org(), None):
        with pytest.raises(IntegrityError):
            _user(org, "staff" if org else "super_admin", email=email.upper())
    assert _rows_with(email) == 1


def test_create_and_update_races_answer_409_and_save_nothing(email_index, no_plan_limits):
    """The pre-check passed (patched to miss the other account, as when two requests run
    at once); the unique index decides, and nothing of the request is kept."""
    taken = _user(_org(), "staff", email=f"taken_{uuid.uuid4().hex[:8]}@example.com").email
    admin, root = _user(_org(), "admin"), _user(None, "super_admin")
    db = SessionLocal()
    try:
        manager = User(name="SM", email=f"sm_{uuid.uuid4().hex[:8]}@example.com",
                       password_hash=hash_password(PASSWORD), system_role="sales_manager")
        db.add(manager)
        db.commit()
        manager_id = manager.id
    finally:
        db.close()
    other_root = _user(None, "super_admin")
    conflict = {"detail": "Email already registered"}

    with patch("app.routers.users._email_taken", return_value=False), \
            patch("app.routers.superadmin._email_taken", return_value=False):
        attempts = {
            "POST /users": client.post("/users", headers=_auth(admin), json={
                "name": "Racer", "contact_information": {"official_email": taken.upper()},
                "login_security": {"password": PASSWORD}}),
            "PATCH /users": _set_email(admin, admin.id, {"official_email": taken, "first_name": "Kept?"}),
            "POST /superadmin/admins": client.post("/superadmin/admins", headers=_auth(root), json={
                "name": "Racer", "email": taken, "password": PASSWORD}),
            "PATCH /superadmin/admins": client.patch(f"/superadmin/admins/{other_root.id}", headers=_auth(root),
                                                     json={"email": taken, "name": "Kept?"}),
            "POST /platform/staff": client.post("/platform/staff", headers=_auth(root), json={
                "name": "Racer", "email": taken, "password": PASSWORD}),
            "PATCH /platform/staff": client.patch(f"/platform/staff/{manager_id}", headers=_auth(root),
                                                  json={"email": taken, "name": "Kept?"}),
        }
    for route, r in attempts.items():
        assert (r.status_code, r.json()) == (409, conflict), (route, r.text)
    assert _rows_with(taken) == 1
    for user_id in (admin.id, other_root.id, manager_id):
        row = _row(user_id)
        assert row.first_name != "Kept?" and row.name != "Kept?" and row.email.lower() != taken


def test_registration_race_answers_409_and_creates_no_firm(email_index):
    email = f"Race.{uuid.uuid4().hex[:8]}@example.com"
    firm = f"Race Firm {uuid.uuid4().hex[:8]}"
    real_start = org_service.start_trial

    def rival_registers_first(db, org):  # noqa: ANN001, ANN202
        _user(None, "super_admin", email=email.lower())  # a platform account, committed after the pre-check
        return real_start(db, org)

    with patch("app.services.auth_service.org_service.start_trial", side_effect=rival_registers_first):
        r = client.post("/auth/register", json={"organization_name": firm, "admin_name": "Racer",
                                                "email": email, "password": PASSWORD})
    assert (r.status_code, r.json()) == (409, {"detail": "Email already registered"})
    assert _rows_with(email) == 1
    db = SessionLocal()
    try:
        assert db.query(Organization).filter(Organization.name == firm).count() == 0
    finally:
        db.close()


# ---- the migration that installs ux_users_email_ci


def _email_migration():  # noqa: ANN202
    path = pathlib.Path(__file__).resolve().parents[1] / "alembic" / "versions" / "b7c8d9e0f1a2_users_email_unique_ci.py"
    spec = importlib.util.spec_from_file_location("email_index_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    return migration


@pytest.fixture
def migration_db(tmp_path):
    """A bare `users` table on the test engine's dialect: a scratch SQLite file, or a
    throwaway schema on the Postgres test database (dropped afterwards)."""
    import sqlalchemy as sa

    schema = None
    if IS_POSTGRES:
        schema = f"mig_{uuid.uuid4().hex[:10]}"
        with engine.begin() as conn:
            conn.execute(sa.text(f'CREATE SCHEMA "{schema}"'))
        mig = sa.create_engine(engine.url.render_as_string(hide_password=False),
                               connect_args={"options": f"-csearch_path={schema}"})
    else:
        mig = sa.create_engine(f"sqlite:///{tmp_path / 'mig.db'}")
    with mig.begin() as conn:
        conn.execute(sa.text("CREATE TABLE users (id VARCHAR(36) PRIMARY KEY, organization_id VARCHAR(36), "
                             "system_role VARCHAR(20), email VARCHAR(255) NOT NULL)"))
    try:
        yield mig
    finally:
        mig.dispose()
        if schema:
            with engine.begin() as conn:
                conn.execute(sa.text(f'DROP SCHEMA "{schema}" CASCADE'))


def _run(mig, step) -> None:  # noqa: ANN001
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    with mig.begin() as conn:
        with Operations.context(MigrationContext.configure(conn)):
            step()


def _insert(mig, *emails: str, org: str | None = "firm-1") -> None:  # noqa: ANN001
    import sqlalchemy as sa

    with mig.begin() as conn:
        for email in emails:
            conn.execute(sa.text("INSERT INTO users (id, organization_id, email) VALUES (:i, :o, :e)"),
                         {"i": str(uuid.uuid4()), "o": org, "e": email})


def _has_index(mig) -> bool:  # noqa: ANN001
    import sqlalchemy as sa

    with mig.connect() as conn:
        if mig.dialect.name == "postgresql":
            return bool(conn.execute(sa.text(
                "SELECT COUNT(*) FROM pg_indexes WHERE schemaname = current_schema() "
                "AND indexname = 'ux_users_email_ci'")).scalar())
        return bool(conn.execute(sa.text(
            "SELECT COUNT(*) FROM sqlite_master WHERE name = 'ux_users_email_ci'")).scalar())


def test_email_index_migration_on_clean_data_creates_enforces_and_downgrades(migration_db):
    import sqlalchemy as sa

    migration = _email_migration()
    _insert(migration_db, "a@example.com", "B@Example.com", org="firm-1")
    _insert(migration_db, "c@example.com", org=None)
    _run(migration_db, migration.upgrade)
    assert _has_index(migration_db)
    for org in ("firm-2", None):  # another firm, the platform: both refused
        with pytest.raises(sa.exc.IntegrityError):
            _insert(migration_db, "b@example.com", org=org)
    _run(migration_db, migration.upgrade)  # already there: a no-op, not an error
    _run(migration_db, migration.downgrade)
    assert not _has_index(migration_db)
    _insert(migration_db, "A@EXAMPLE.COM")  # nothing enforces it any more


def test_email_index_migration_refuses_conflicts_and_changes_nothing(migration_db):
    import sqlalchemy as sa

    migration = _email_migration()
    _insert(migration_db, "Private.Person@Example.com", org="firm-1")
    _insert(migration_db, " private.person@example.com", org=None)   # case + spaces, platform
    _insert(migration_db, "twin@example.com", "twin@example.com")    # exact, same firm
    with pytest.raises(RuntimeError) as failure:
        _run(migration_db, migration.upgrade)
    message = str(failure.value)
    assert "2 email(s) are shared by 4 accounts" in message and "app.scripts.email_conflicts" in message
    assert "private" not in message.lower() and "twin" not in message
    assert not _has_index(migration_db)
    with migration_db.connect() as conn:
        assert conn.execute(sa.text("SELECT COUNT(*) FROM users")).scalar() == 4


def test_email_index_migration_accepts_the_startup_index_and_rejects_a_different_one(migration_db):
    import sqlalchemy as sa

    migration = _email_migration()
    with migration_db.begin() as conn:  # what enforce_unique_email builds
        conn.execute(sa.text('CREATE UNIQUE INDEX "ux_users_email_ci" ON "users" (LOWER("email"))'))
    _run(migration_db, migration.upgrade)
    assert _has_index(migration_db)

    _run(migration_db, migration.downgrade)
    with migration_db.begin() as conn:
        conn.execute(sa.text('CREATE INDEX "ux_users_email_ci" ON users (email)'))  # not unique, not lower()
    with pytest.raises(RuntimeError, match="different definition"):
        _run(migration_db, migration.upgrade)


def test_email_conflict_report_is_masked_and_categorized(migration_db):
    from app.scripts.email_conflicts import find_conflicts

    _insert(migration_db, "Same.Firm@example.com", "same.firm@example.com", org="firm-1")
    _insert(migration_db, "cross@example.com", org="firm-1")
    _insert(migration_db, "cross@example.com", org="firm-2")
    _insert(migration_db, "Mixed@Example.com", org="firm-3")
    _insert(migration_db, "mixed@example.com", "MIXED@example.com", org=None)
    _insert(migration_db, "fine@example.com", org="firm-4")
    with migration_db.connect() as conn:
        report = find_conflicts(conn)
    assert report["accounts"] == 7 and len(report["groups"]) == 3
    assert report["counts"] == {"same_firm": 1, "cross_firm": 1, "firm_and_platform": 1, "platform": 1,
                                "case_or_spaces": 2}
    printed = repr(report).lower()
    assert "same.firm@" not in printed and "cross@" not in printed and "mixed@" not in printed
    assert "fine" not in printed


def test_supplier_code_migration_keeps_the_transaction_usable_when_its_steps_fail(migration_db):
    """s7t8u9v0w1x2 on a database that already has the constraint (a fresh install: the
    baseline builds it from the models) and where the backfill fails. Its best-effort steps
    must not poison the migration transaction on PostgreSQL — before the fix every later
    statement, the alembic_version update included, failed with InFailedSqlTransaction."""
    import sqlalchemy as sa
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    path = pathlib.Path(__file__).resolve().parents[1] / "alembic" / "versions" / "s7t8u9v0w1x2_add_supplier_code_to_suppliers.py"
    spec = importlib.util.spec_from_file_location("supplier_code_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    with migration_db.begin() as conn:  # no organizations table: the backfill will fail
        conn.execute(sa.text("CREATE TABLE suppliers (id VARCHAR(36) PRIMARY KEY, organization_id VARCHAR(36), "
                             "supplier_code VARCHAR(50), "
                             "CONSTRAINT uq_suppliers_org_supplier_code UNIQUE (organization_id, supplier_code))"))
    with migration_db.begin() as conn:
        with Operations.context(MigrationContext.configure(conn)):
            migration.upgrade()
        assert conn.execute(sa.text("SELECT COUNT(*) FROM suppliers")).scalar() == 0  # still usable
    with migration_db.connect() as conn:
        names = {uc["name"] for uc in sa.inspect(conn).get_unique_constraints("suppliers")}
        assert "uq_suppliers_org_supplier_code" in names
