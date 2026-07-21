"""Seed the platform Super Admin (and optional demo data).

Run with:  python -m app.seed
Idempotent — safe to run multiple times.
"""

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import Base, SessionLocal, engine
from app.core.security import hash_password
from app.models import Organization, OrganizationStatus, PlanTier, User, UserRole


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


def seed_demo_firm(db: Session) -> None:
    """A demo firm + admin so the login screen's admin@demo.com works out of the box."""
    if db.query(User).filter(User.email == "admin@demo.com").first() is not None:
        print("[seed] Demo firm already exists: admin@demo.com")
        return

    org = Organization(
        name="SAAS Distributors",
        gst_number="27AABCU9603R1ZM",
        email="admin@demo.com",
        plan=PlanTier.ENTERPRISE,
        status=OrganizationStatus.ACTIVE,
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
        seed_super_admin(db)
        seed_demo_firm(db)
    finally:
        db.close()
    print("[seed] Done.")


if __name__ == "__main__":
    main()
