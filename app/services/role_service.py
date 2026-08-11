from sqlalchemy.orm import Session

from app.core.permissions import (
    DEFAULT_ROLE_SETTINGS,
    default_role_matrices,
    normalize_permissions,
)
from app.models import STAFF_ROLE_NAME, Organization, Role, UserRole


def seed_default_roles(db: Session, organization_id: str) -> None:
    """Create the 3 default roles for an org if they don't already exist (idempotent).

    Also backfills `workspace` / `data_scope` on defaults seeded before those
    columns existed, so an upgraded database gets them without a migration.
    """
    existing = {
        role.name: role
        for role in db.query(Role).filter(Role.organization_id == organization_id).all()
    }
    changed = False
    for name, matrix in default_role_matrices().items():
        settings = DEFAULT_ROLE_SETTINGS.get(name, {})
        role = existing.get(name)
        if role is None:
            db.add(
                Role(
                    organization_id=organization_id,
                    name=name,
                    is_default=True,
                    workspace=settings.get("workspace"),
                    data_scope=settings.get("data_scope", "all"),
                    permissions=normalize_permissions(matrix),
                )
            )
            changed = True
            continue
        # Only fill blanks — an Admin who has changed either one keeps their choice.
        if role.workspace is None and settings.get("workspace"):
            role.workspace = settings["workspace"]
            changed = True
        if role.data_scope is None:
            role.data_scope = settings.get("data_scope", "all")
            changed = True
    if changed:
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


# ----------------------- Effective access for a user -----------------------
# One place that answers "what may this user do, and whose records may they see",
# so /auth/me, the permission guard and the list-scoping helper never disagree.


def is_full_access(user) -> bool:  # noqa: ANN001
    """Admins and the platform Super Admin are not permission-checked inside their
    own scope — an Admin owns their firm."""
    return user.effective_system_role in ("admin", "super_admin")


def role_for(db: Session, user) -> Role | None:  # noqa: ANN001
    """The user's org-scoped role, or None (Admins hold no role)."""
    if not user.role_id:
        return None
    return db.get(Role, user.role_id)


def effective_permissions(db: Session, user) -> dict:  # noqa: ANN001
    """The permission matrix to enforce and to report on /auth/me. An Admin gets
    every module/action so callers can read one field for any kind of user."""
    from app.core.permissions import full_access_matrix

    if is_full_access(user):
        return full_access_matrix()
    role = role_for(db, user)
    return (role.permissions if role is not None else {}) or {}


def data_scope(db: Session, user) -> str:  # noqa: ANN001
    """"own" when the user's role only sees its own records, else "all"."""
    if is_full_access(user):
        return "all"
    role = role_for(db, user)
    return (role.data_scope if role is not None else None) or "all"
