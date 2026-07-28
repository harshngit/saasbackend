from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import require_system_role
from app.core.permissions import catalog, normalize_permissions
from app.models import Role, SystemRole, User
from app.schemas.role import RoleCreate, RoleOut, RoleUpdate

router = APIRouter(prefix="/roles", tags=["roles"])

_ADMIN = require_system_role(SystemRole.ADMIN)


def _get_owned_role(db: Session, role_id: str, admin: User) -> Role:
    role = db.get(Role, role_id)
    if role is None or role.organization_id != admin.organization_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Role not found")
    return role


# NOTE: /catalog must be declared before /{role_id} so it isn't captured as an id.
@router.get("/catalog")
def permission_catalog(_admin: User = Depends(_ADMIN)) -> dict:
    """All modules + actions (with labels) so the frontend can render the matrix UI."""
    return catalog()


@router.get("", response_model=list[RoleOut])
def list_roles(
    admin: User = Depends(_ADMIN),
    db: Session = Depends(get_db),
) -> list[Role]:
    return (
        db.query(Role)
        .filter(Role.organization_id == admin.organization_id)
        .order_by(Role.is_default.desc(), Role.name)
        .all()
    )


@router.post("", response_model=RoleOut, status_code=status.HTTP_201_CREATED)
def create_role(
    payload: RoleCreate,
    admin: User = Depends(_ADMIN),
    db: Session = Depends(get_db),
) -> Role:
    clash = (
        db.query(Role)
        .filter(Role.organization_id == admin.organization_id, Role.name == payload.name)
        .first()
    )
    if clash is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="A role with this name already exists")

    role = Role(
        organization_id=admin.organization_id,
        name=payload.name,
        is_default=False,
        permissions=normalize_permissions(payload.permissions),
    )
    db.add(role)
    db.commit()
    db.refresh(role)
    return role


@router.get("/{role_id}", response_model=RoleOut)
def get_role(
    role_id: str,
    admin: User = Depends(_ADMIN),
    db: Session = Depends(get_db),
) -> Role:
    return _get_owned_role(db, role_id, admin)


@router.put("/{role_id}", response_model=RoleOut)
def update_role(
    role_id: str,
    payload: RoleUpdate,
    admin: User = Depends(_ADMIN),
    db: Session = Depends(get_db),
) -> Role:
    """Edit a role's name and/or permissions. Allowed on default roles too."""
    role = _get_owned_role(db, role_id, admin)

    if payload.name is not None and payload.name != role.name:
        if name_clash := (
            db.query(Role)
            .filter(
                Role.organization_id == admin.organization_id,
                Role.name == payload.name,
                Role.id != role.id,
            )
            .first()
        ):
            _ = name_clash
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="A role with this name already exists")
        role.name = payload.name

    if payload.permissions is not None:
        role.permissions = normalize_permissions(payload.permissions)

    db.commit()
    db.refresh(role)
    return role


@router.delete("/{role_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_role(
    role_id: str,
    admin: User = Depends(_ADMIN),
    db: Session = Depends(get_db),
) -> None:
    role = _get_owned_role(db, role_id, admin)
    if role.is_default:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Default roles cannot be deleted (you can edit their permissions instead)",
        )
    assigned = db.query(User).filter(User.role_id == role.id).count()
    if assigned > 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot delete: {assigned} user(s) are assigned to this role. Reassign them first.",
        )
    db.delete(role)
    db.commit()
