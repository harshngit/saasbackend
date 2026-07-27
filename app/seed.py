"""Seed the platform Super Admin, plan catalog, and optional demo data.

Run with:  python -m app.seed
Idempotent — safe to run multiple times.
"""

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import Base, SessionLocal, engine
from app.core.security import hash_password
from app.models import Organization, OrganizationStatus, Plan, User, UserRole

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

    admin = User(
        organization_id=None,  # platform-level, no tenant
        name=settings.super_admin_name,
        email=settings.super_admin_email,
        password_hash=hash_password(settings.super_admin_password),
        role=UserRole.SUPER_ADMIN,
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

    db.add(
        User(
            organization_id=org.id,
            name="Anita Sharma",
            email="admin@demo.com",
            password_hash=hash_password("Admin@123"),
            role=UserRole.ADMIN,
        )
    )
    db.commit()
    print("[seed] Created demo firm 'SAAS Distributors' with admin@demo.com")


def main() -> None:
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        default_plan = seed_plans(db)
        seed_super_admin(db)
        seed_demo_firm(db, default_plan)
        # Ensure every org has its 3 default roles (backfill for existing orgs).
        from app.services.role_service import seed_default_roles_for_all_orgs

        seed_default_roles_for_all_orgs(db)
        print("[seed] Default roles ensured for all orgs")
    finally:
        db.close()
    print("[seed] Done.")


if __name__ == "__main__":
    main()
