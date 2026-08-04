from sqlalchemy.orm import Session

from app.core.permissions import default_role_matrices, normalize_permissions
from app.models import STAFF_ROLE_NAME, Organization, Role, UserRole


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


def backfill_user_roles(db: Session) -> None:
    """Set system_role (and role_id for staff) on existing users that predate Phase 2."""
    from app.models import User, system_role_for

    changed = False
    for user in db.query(User).all():
        if not user.system_role:
            user.system_role = system_role_for(user.role)
            changed = True
        if (
            user.system_role == "staff"
            and user.role_id is None
            and user.organization_id is not None
            and user.role is not None
        ):
            role = default_role_for_legacy(db, user.organization_id, user.role)
            if role is not None:
                user.role_id = role.id
                changed = True
    if changed:
        db.commit()


def name_taken(db: Session, organization_id: str, name: str, exclude_id: str | None = None) -> bool:
    query = db.query(Role).filter(Role.organization_id == organization_id, Role.name == name)
    if exclude_id is not None:
        query = query.filter(Role.id != exclude_id)
    return db.query(query.exists()).scalar()


def get_role_in_org(db: Session, organization_id: str, role_id: str) -> Role | None:
    """Return the role only if it belongs to this org (else None — no cross-org access)."""
    role = db.get(Role, role_id)
    if role is None or role.organization_id != organization_id:
        return None
    return role


def _comparable(name: str) -> str:
    """"Sales Officer" / "sales_officer" / "sales-officer" all compare equal, so a
    role can be named by its label or by a legacy enum value."""
    return name.strip().lower().replace("_", " ").replace("-", " ")


def get_role_by_name(db: Session, organization_id: str, name: str) -> Role | None:
    """Find one of the org's roles by name, ignoring case / spacing / separators."""
    wanted = _comparable(name)
    for role in db.query(Role).filter(Role.organization_id == organization_id).all():
        if _comparable(role.name) == wanted:
            return role
    return None


def resolve_role(
    db: Session,
    organization_id: str,
    role_id: str | None = None,
    role_name: str | None = None,
) -> Role | None:
    """Resolve whatever the client sent to one of the org's roles: `role_id` wins,
    otherwise the name is matched against the roles the firm actually has — custom
    roles from the Roles page included."""
    if role_id:
        return get_role_in_org(db, organization_id, role_id)
    if role_name:
        return get_role_by_name(db, organization_id, role_name)
    return None


def role_names(db: Session, organization_id: str) -> list[str]:
    """The firm's role names, for error messages / dropdowns."""
    return [
        name
        for (name,) in db.query(Role.name)
        .filter(Role.organization_id == organization_id)
        .order_by(Role.name)
        .all()
    ]


def default_role_for_legacy(db: Session, organization_id: str, legacy_role: UserRole) -> Role | None:
    """Find the org's default role matching a legacy staff role enum."""
    name = STAFF_ROLE_NAME.get(legacy_role)
    if name is None:
        return None
    return (
        db.query(Role)
        .filter(Role.organization_id == organization_id, Role.name == name)
        .first()
    )
