"""Regression tests for the seed-script security fixes:

- app.core.config.Settings.super_admin_password has no hardcoded default —
  a known password baked into source code would be a working production
  credential for anyone who reads the repo.
- app.seed.seed_super_admin() refuses to create a Super Admin with no
  explicit password, and never touches an already-existing one.
- app.seed.seed_testing_paid_user() is idempotent like the other seed
  functions: re-running it must never reset an existing user's password or
  silently rewrite their organization's plan/status.
- Removal of hardcoded passwords ("Admin@123", "12345678") from app.seed.
- Support for DEMO_ADMIN_PASSWORD and TESTING_USER_PASSWORD with cryptographically
  secure random password fallback.
- seed_on_startup remains False by default.
- No seed passwords logged or printed.

Runs only against the local test database — never production, and never
seeds a real password into source or test code.
"""

import io
import os
import sys
import uuid
from contextlib import redirect_stdout

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.core.config import Settings, settings
from app.core.database import SessionLocal
from app.core.security import verify_password
from app.models import Organization, OrganizationStatus, Plan, User, UpgradeStatus
from app.seed import _resolve_seed_password, seed_demo_firm, seed_super_admin, seed_testing_paid_user


def test_super_admin_password_has_no_hardcoded_default():
    """The settings class itself must not carry a known-password default —
    only an explicit SUPER_ADMIN_PASSWORD (env/.env) should ever populate it."""
    fresh = Settings(_env_file=None)  # bypass .env entirely to see the bare class default
    assert fresh.super_admin_password == ""
    assert fresh.demo_admin_password == ""
    assert fresh.testing_user_password == ""


def test_seed_on_startup_is_false_by_default():
    fresh = Settings(_env_file=None)
    assert fresh.seed_on_startup is False


def test_no_hardcoded_passwords_in_seed_source():
    seed_file_path = os.path.join(os.path.dirname(__file__), "..", "app", "seed.py")
    with open(seed_file_path, "r", encoding="utf-8") as f:
        content = f.read()
    assert "Admin@123" not in content, "Literal 'Admin@123' must not be in app/seed.py"
    assert "12345678" not in content, "Literal '12345678' must not be in app/seed.py"


def test_seed_password_helper_entropy_and_randomness():
    # When configured, returns configured password
    assert _resolve_seed_password("MyExplicitPass123!") == "MyExplicitPass123!"

    # When None or empty, generates secure random fallback
    pw1 = _resolve_seed_password(None)
    pw2 = _resolve_seed_password("")
    pw3 = _resolve_seed_password("   ")
    assert len(pw1) >= 16
    assert len(pw2) >= 16
    assert len(pw3) >= 16
    assert pw1 != pw2
    assert pw2 != pw3


def test_seed_super_admin_refuses_without_a_password(monkeypatch):
    monkeypatch.setattr(settings, "super_admin_password", "")
    monkeypatch.setattr(settings, "super_admin_email", f"noseed_{uuid.uuid4().hex[:8]}@example.com")

    db = SessionLocal()
    try:
        seed_super_admin(db)
        created = db.query(User).filter(User.email == settings.super_admin_email).first()
        assert created is None  # refused — nothing was created
    finally:
        db.close()


def test_seed_super_admin_creates_with_explicit_password(monkeypatch):
    test_password = f"TestPass-{uuid.uuid4().hex[:10]}"  # generated per-run, never a real/shared password
    monkeypatch.setattr(settings, "super_admin_password", test_password)
    monkeypatch.setattr(settings, "super_admin_email", f"seedok_{uuid.uuid4().hex[:8]}@example.com")

    db = SessionLocal()
    try:
        seed_super_admin(db)
        created = db.query(User).filter(User.email == settings.super_admin_email).first()
        assert created is not None
        assert verify_password(test_password, created.password_hash)
    finally:
        db.close()


def test_seed_super_admin_does_not_touch_existing_admin(monkeypatch):
    original_password = f"Original-{uuid.uuid4().hex[:10]}"
    email = f"existing_{uuid.uuid4().hex[:8]}@example.com"
    monkeypatch.setattr(settings, "super_admin_email", email)
    monkeypatch.setattr(settings, "super_admin_password", original_password)

    db = SessionLocal()
    try:
        seed_super_admin(db)
        first = db.query(User).filter(User.email == email).first()
        assert first is not None
        original_hash = first.password_hash

        # Re-run with a DIFFERENT password configured — must be a no-op.
        monkeypatch.setattr(settings, "super_admin_password", "SomethingElse-99999")
        seed_super_admin(db)

        db.expire_all()
        again = db.query(User).filter(User.email == email).first()
        assert again.password_hash == original_hash  # untouched
        assert verify_password(original_password, again.password_hash)
    finally:
        db.close()


def test_seed_testing_paid_user_does_not_reset_existing_password():
    """The vulnerability this fixes: seed_testing_paid_user() used to
    unconditionally overwrite testing@gmail.com's password to a hardcoded
    value on every run. It must now be a true no-op if the user exists."""
    db = SessionLocal()
    try:
        db.query(User).filter(User.email == "testing@gmail.com").delete()
        db.query(Organization).filter(Organization.email == "testing@gmail.com").delete()
        db.commit()

        org = Organization(name="Pre-existing Testing Org", email="testing@gmail.com", status=OrganizationStatus.TRIAL)
        db.add(org)
        db.flush()
        distinct_password_hash = "not-a-real-hash-just-a-sentinel-value"
        user = User(
            organization_id=org.id, name="Real Person", email="testing@gmail.com",
            password_hash=distinct_password_hash, system_role="admin",
        )
        db.add(user)
        db.commit()
        user_id, org_id = user.id, org.id

        seed_testing_paid_user(db)

        db.expire_all()
        after_user = db.get(User, user_id)
        after_org = db.get(Organization, org_id)
        assert after_user.password_hash == distinct_password_hash  # untouched
        assert after_org.status == OrganizationStatus.TRIAL  # untouched — not force-upgraded to ACTIVE/Pro
    finally:
        db.query(User).filter(User.email == "testing@gmail.com").delete()
        db.query(Organization).filter(Organization.email == "testing@gmail.com").delete()
        db.commit()
        db.close()


def test_seed_testing_paid_user_creates_with_env_password(monkeypatch):
    test_pw = f"TestingSecret-{uuid.uuid4().hex[:8]}"
    monkeypatch.setattr(settings, "testing_user_password", test_pw)

    db = SessionLocal()
    try:
        db.query(User).filter(User.email == "testing@gmail.com").delete()
        db.query(Organization).filter(Organization.email == "testing@gmail.com").delete()
        db.commit()

        seed_testing_paid_user(db)

        created = db.query(User).filter(User.email == "testing@gmail.com").first()
        assert created is not None
        assert verify_password(test_pw, created.password_hash)
        assert created.organization.status == OrganizationStatus.ACTIVE
        assert created.organization.upgrade_status == UpgradeStatus.APPROVED.value
    finally:
        db.query(User).filter(User.email == "testing@gmail.com").delete()
        db.query(Organization).filter(Organization.email == "testing@gmail.com").delete()
        db.commit()
        db.close()


def test_seed_testing_paid_user_creates_with_random_fallback_when_empty(monkeypatch):
    monkeypatch.setattr(settings, "testing_user_password", "")

    db = SessionLocal()
    try:
        db.query(User).filter(User.email == "testing@gmail.com").delete()
        db.query(Organization).filter(Organization.email == "testing@gmail.com").delete()
        db.commit()

        seed_testing_paid_user(db)

        created = db.query(User).filter(User.email == "testing@gmail.com").first()
        assert created is not None
        assert created.password_hash is not None
        assert not verify_password("12345678", created.password_hash)
    finally:
        db.query(User).filter(User.email == "testing@gmail.com").delete()
        db.query(Organization).filter(Organization.email == "testing@gmail.com").delete()
        db.commit()
        db.close()


def test_seed_demo_firm_creates_with_env_password(monkeypatch):
    test_pw = f"DemoSecret-{uuid.uuid4().hex[:8]}"
    monkeypatch.setattr(settings, "demo_admin_password", test_pw)

    db = SessionLocal()
    try:
        db.query(User).filter(User.email == "admin@demo.com").delete()
        db.query(Organization).filter(Organization.email == "admin@demo.com").delete()
        db.commit()

        default_plan = db.query(Plan).first()
        seed_demo_firm(db, default_plan)

        created = db.query(User).filter(User.email == "admin@demo.com").first()
        assert created is not None
        assert verify_password(test_pw, created.password_hash)
    finally:
        db.query(User).filter(User.email == "admin@demo.com").delete()
        db.query(Organization).filter(Organization.email == "admin@demo.com").delete()
        db.commit()
        db.close()


def test_seed_demo_firm_creates_with_random_fallback_when_empty(monkeypatch):
    monkeypatch.setattr(settings, "demo_admin_password", "")

    db = SessionLocal()
    try:
        db.query(User).filter(User.email == "admin@demo.com").delete()
        db.query(Organization).filter(Organization.email == "admin@demo.com").delete()
        db.commit()

        default_plan = db.query(Plan).first()
        seed_demo_firm(db, default_plan)

        created = db.query(User).filter(User.email == "admin@demo.com").first()
        assert created is not None
        assert created.password_hash is not None
        assert not verify_password("Admin@123", created.password_hash)
    finally:
        db.query(User).filter(User.email == "admin@demo.com").delete()
        db.query(Organization).filter(Organization.email == "admin@demo.com").delete()
        db.commit()
        db.close()


def test_seed_does_not_log_passwords(monkeypatch):
    secret_demo = "SuperSecretDemoPass987!"
    secret_test = "SuperSecretTestPass654!"
    monkeypatch.setattr(settings, "demo_admin_password", secret_demo)
    monkeypatch.setattr(settings, "testing_user_password", secret_test)

    buf = io.StringIO()
    db = SessionLocal()
    try:
        db.query(User).filter(User.email.in_(["admin@demo.com", "testing@gmail.com"])).delete()
        db.query(Organization).filter(Organization.email.in_(["admin@demo.com", "testing@gmail.com"])).delete()
        db.commit()

        default_plan = db.query(Plan).first()
        with redirect_stdout(buf):
            seed_demo_firm(db, default_plan)
            seed_testing_paid_user(db)

        output = buf.getvalue()
        assert secret_demo not in output, "demo password must never be logged/printed"
        assert secret_test not in output, "testing password must never be logged/printed"
    finally:
        db.query(User).filter(User.email.in_(["admin@demo.com", "testing@gmail.com"])).delete()
        db.query(Organization).filter(Organization.email.in_(["admin@demo.com", "testing@gmail.com"])).delete()
        db.commit()
        db.close()
