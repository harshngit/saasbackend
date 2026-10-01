"""Seed the platform Super Admin, plan catalog, and optional demo data.

Run with:  python -m app.seed
Idempotent — safe to run multiple times.
"""

import secrets
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import Base, SessionLocal, engine
from app.core.security import hash_password
from app.models import Organization, OrganizationStatus, Plan, UpgradeStatus, User, UserRole


def _resolve_seed_password(configured_password: str | None) -> str:
    """Return configured password if provided, otherwise generate a secure random one.
    Never logs or exposes the password."""
    if configured_password and configured_password.strip():
        return configured_password.strip()
    return secrets.token_urlsafe(16)


# Starter catalog. The Super Admin can edit / deactivate / add more via the API.
_DEFAULT_PLANS = [
    {
        "name": "Free", "price_monthly": 0, "price_yearly": 0, "is_default": True,
        "max_users": 3, "max_orders": 50,
        "features": ["Up to 3 users", "50 orders/month", "Basic dashboard"],
    },
    {
        "name": "Basic", "price_monthly": 499, "price_yearly": 4999,
        "original_price_monthly": 799, "original_price_yearly": 7999,
        "max_users": 10, "max_orders": 1000,
        "features": ["Up to 10 users", "1,000 orders/month", "Reports", "GST invoicing"],
    },
    {
        "name": "Pro", "price_monthly": 999, "price_yearly": 9999,
        "original_price_monthly": 1499, "original_price_yearly": 14999,
        "max_users": 50, "max_orders": None,
        "features": ["Up to 50 users", "Unlimited orders", "Advanced analytics", "Priority support"],
    },
    {
        "name": "Enterprise", "price_monthly": 2499, "price_yearly": 24999,
        "max_users": None, "max_orders": None,
        "features": ["Unlimited users", "Unlimited orders", "Dedicated support", "Custom integrations"],
    },
]


def seed_plans(db: Session) -> Plan:
    """Ensure the catalog exists; return the default (free) plan."""
    for spec in _DEFAULT_PLANS:
        if db.query(Plan).filter(Plan.name == spec["name"]).first() is None:
            db.add(Plan(**spec))
    db.commit()
    default = db.query(Plan).filter(Plan.is_default.is_(True)).first()
    # Backfill any org without a plan onto the default plan.
    if default is not None:
        db.query(Organization).filter(Organization.plan_id.is_(None)).update(
            {Organization.plan_id: default.id}
        )
        db.commit()
    print("[seed] Plan catalog ready")
    return default


def seed_super_admin(db: Session) -> None:
    existing = db.query(User).filter(User.email == settings.super_admin_email).first()
    if existing is not None:
        print(f"[seed] Super Admin already exists: {settings.super_admin_email}")
        return

    if not settings.super_admin_password:
        print(
            "[seed] SUPER_ADMIN_PASSWORD is not set — refusing to create a Super "
            "Admin with no explicit password. Set SUPER_ADMIN_PASSWORD and re-run."
        )
        return

    admin = User(
        organization_id=None,  # platform-level, no tenant
        name=settings.super_admin_name,
        email=settings.super_admin_email,
        password_hash=hash_password(settings.super_admin_password),
        role=UserRole.SUPER_ADMIN,
        system_role="super_admin",
    )
    db.add(admin)
    db.commit()
    print(f"[seed] Created Super Admin: {settings.super_admin_email}")


def seed_demo_firm(db: Session, default_plan: Plan | None) -> None:
    """A demo firm + admin so the login screen's admin@demo.com works out of the box."""
    if db.query(User).filter(User.email == "admin@demo.com").first() is not None:
        print("[seed] Demo firm already exists: admin@demo.com")
        return

    org = Organization(
        name="SAAS Distributors",
        gst_number="27AABCU9603R1ZM",
        email="admin@demo.com",
        status=OrganizationStatus.ACTIVE,
        plan_id=default_plan.id if default_plan else None,
    )
    db.add(org)
    db.flush()

    password = _resolve_seed_password(settings.demo_admin_password)
    db.add(
        User(
            organization_id=org.id,
            name="Anita Sharma",
            email="admin@demo.com",
            password_hash=hash_password(password),
            role=UserRole.ADMIN,
            system_role="admin",
        )
    )
    db.commit()
    print("[seed] Created demo firm 'SAAS Distributors' with admin@demo.com")


def seed_testing_paid_user(db: Session) -> None:
    """Create the demo user testing@gmail.com on Pro/active, if it doesn't exist yet.

    Idempotent the same way seed_super_admin/seed_demo_firm are: if the row
    already exists, this is a pure no-op — it must never reset an existing
    user's password or silently rewrite their organization's plan/status.
    Without that guard, re-running seed against an environment where a real
    person happened to register with this exact email would overwrite their
    password with a publicly-known value on every run.
    """
    testing_email = "testing@gmail.com"
    pro_plan = db.query(Plan).filter(Plan.name == "Pro").first()
    pro_plan_id = pro_plan.id if pro_plan else None

    existing = db.query(User).filter(User.email == testing_email).first()
    if existing is not None:
        print(f"[seed] Testing user already exists: {testing_email} (left unchanged)")
        return

    org = Organization(
        name="Testing Paid Org",
        gst_number="27AABCU9603R1ZM",
        email=testing_email,
        status=OrganizationStatus.ACTIVE,
        plan_id=pro_plan_id,
        upgrade_status=UpgradeStatus.APPROVED.value,
    )
    db.add(org)
    db.flush()

    password = _resolve_seed_password(settings.testing_user_password)
    user = User(
        organization_id=org.id,
        name="Testing User",
        email=testing_email,
        password_hash=hash_password(password),
        role=UserRole.ADMIN,
        system_role="admin",
    )
    db.add(user)
    db.commit()

    # Ensure default roles for the new organization
    from app.services.role_service import seed_default_roles
    seed_default_roles(db, org.id)

    print(f"[seed] Created new user {testing_email} and organization as Paid (Pro)")



def main() -> None:
    Base.metadata.create_all(bind=engine)
    from app.core.database import auto_add_missing_columns
    auto_add_missing_columns()
    db = SessionLocal()
    try:
        default_plan = seed_plans(db)
        seed_super_admin(db)
        seed_demo_firm(db, default_plan)
        seed_testing_paid_user(db)
        # Ensure every org has its 3 default roles (backfill for existing orgs).
        from app.services.role_service import backfill_user_roles, seed_default_roles_for_all_orgs

        seed_default_roles_for_all_orgs(db)
        print("[seed] Default roles ensured for all orgs")
        # Backfill system_role + role_id on users that predate Phase 2.
        backfill_user_roles(db)
        print("[seed] User system_role / role_id backfilled")
    finally:
        db.close()
    print("[seed] Done.")


if __name__ == "__main__":
    main()
