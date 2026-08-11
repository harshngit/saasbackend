from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import require_system_role
from app.core.permissions import catalog, normalize_permissions
from app.models import Role, SystemRole, User
from app.schemas.role import RoleCreate, RoleOut, RoleUpdate
from app.services import role_service

router = APIRouter(prefix="/roles", tags=["roles"])

_ADMIN = require_system_role(SystemRole.ADMIN)


def _get_owned_role(db: Session, role_id: str, admin: User) -> Role:
    role = role_service.get_role_in_org(db, admin.organization_id, role_id)
    if role is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Role not found")
    return role


def _out(db: Session, role: Role) -> RoleOut:
    """The role plus how many staff hold it, which the Roles screen shows."""
    out = RoleOut.model_validate(role)
    out.assigned_users = db.query(func.count(User.id)).filter(User.role_id == role.id).scalar() or 0
    return out


# NOTE: /catalog must be declared before /{role_id} so it isn't captured as an id.
@router.get("/catalog")
def permission_catalog(_admin: User = Depends(_ADMIN)) -> dict:
    """All modules + actions (with labels) so the frontend can render the matrix UI."""
    return catalog()


@router.get("", response_model=list[RoleOut])
def list_roles(
    admin: User = Depends(_ADMIN),
    db: Session = Depends(get_db),
) -> list[RoleOut]:
    """Every role this organization has — its own custom roles and the seeded
    defaults. Never another firm's."""
    roles = (
        db.query(Role)
        .filter(Role.organization_id == admin.organization_id)
        .order_by(Role.is_default.desc(), Role.name)
        .all()
    )
    return [_out(db, role) for role in roles]


@router.post("", response_model=RoleOut, status_code=status.HTTP_201_CREATED)
def create_role(
    payload: RoleCreate,
    admin: User = Depends(_ADMIN),
    db: Session = Depends(get_db),
) -> RoleOut:
    """Admin creates a role and sets its permission matrix.

    `workspace` is what the frontend routes on after login (sales / delivery /
    accounts / …). `data_scope` decides whether holders of this role see the whole
    firm's records or only their own — see the field description.

    Module keys are normalized: `orders` is accepted for `sales_orders`, and any
    module with no granted action is dropped (deny-by-default). GET /roles/catalog
    lists the canonical keys.
    """
    if role_service.name_taken(db, admin.organization_id, payload.name):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="A role with this name already exists")

    role = Role(
        organization_id=admin.organization_id,
        name=payload.name,
        workspace=payload.workspace,
        description=payload.description,
        data_scope=payload.data_scope,
        is_default=False,
        permissions=normalize_permissions(payload.permissions),
    )
    db.add(role)
    db.commit()
    db.refresh(role)
    return _out(db, role)


@router.get("/{role_id}", response_model=RoleOut)
def get_role(
    role_id: str,
    admin: User = Depends(_ADMIN),
    db: Session = Depends(get_db),
) -> RoleOut:
    """Full role details + permission matrix — what the Edit Role screen loads."""
    return _out(db, _get_owned_role(db, role_id, admin))


@router.patch("/{role_id}", response_model=RoleOut)
def update_role(
    role_id: str,
    payload: RoleUpdate,
    admin: User = Depends(_ADMIN),
    db: Session = Depends(get_db),
) -> RoleOut:
    """Edit a role's name, workspace, description, data scope and/or permissions.
    Allowed on the seeded default roles too. Takes effect for every staff member
    holding the role on their next request — nothing is copied onto the users."""
    role = _get_owned_role(db, role_id, admin)
    data = payload.model_dump(exclude_unset=True)

    if "name" in data and data["name"] != role.name:
        if role_service.name_taken(db, admin.organization_id, data["name"], exclude_id=role.id):
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="A role with this name already exists")
        role.name = data["name"]
    for field in ("workspace", "description", "data_scope"):
        if field in data:
            setattr(role, field, data[field])
    if data.get("permissions") is not None:
        role.permissions = normalize_permissions(data["permissions"])

    db.commit()
    db.refresh(role)
    return _out(db, role)


@router.delete("/{role_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_role(
    role_id: str,
    admin: User = Depends(_ADMIN),
    db: Session = Depends(get_db),
) -> None:
    """Delete a custom role. The seeded defaults cannot be deleted (edit their
    permissions instead), and a role still held by staff is refused — reassign
    those users first."""
    role = _get_owned_role(db, role_id, admin)
    if role.is_default:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Default roles cannot be deleted (you can edit their permissions instead)",
        )
    assigned = db.query(func.count(User.id)).filter(User.role_id == role.id).scalar() or 0
    if assigned > 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot delete: {assigned} user(s) are assigned to this role. Reassign them first.",
        )
    db.delete(role)
    db.commit()
