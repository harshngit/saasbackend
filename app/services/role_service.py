from sqlalchemy.orm import Session

from app.core.permissions import default_role_matrices, normalize_permissions
from app.models import Organization, Role


def seed_default_roles(db: Session, organization_id: str) -> None:
    """Create the 3 default roles for an org if they don't already exist (idempotent)."""
    existing = {
        name
        for (name,) in db.query(Role.name).filter(Role.organization_id == organization_id).all()
    }
    created = False
    for name, matrix in default_role_matrices().items():
        if name in existing:
            continue
        db.add(
            Role(
                organization_id=organization_id,
                name=name,
                is_default=True,
                permissions=normalize_permissions(matrix),
            )
        )
        created = True
    if created:
        db.commit()


def seed_default_roles_for_all_orgs(db: Session) -> None:
    """Backfill default roles for every existing organization (startup safety net)."""
    for (org_id,) in db.query(Organization.id).all():
        seed_default_roles(db, org_id)


def name_taken(db: Session, organization_id: str, name: str, exclude_id: str | None = None) -> bool:
    query = db.query(Role).filter(Role.organization_id == organization_id, Role.name == name)
    if exclude_id is not None:
        query = query.filter(Role.id != exclude_id)
    return db.query(query.exists()).scalar()
