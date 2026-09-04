from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field

from app.core.permissions import DATA_SCOPES, DEFAULT_DATA_SCOPE

# permissions shape: { "<module>": { "view": bool, "create": bool, ... }, ... }
PermissionMatrix = dict[str, dict[str, bool]]


def _normalize_scope(value: object) -> object:
    """Accept "All" / "OWN" for the two data scopes."""
    return value.strip().lower() if isinstance(value, str) else value


def _scope_or_default(value: object) -> object:
    """Roles written before this column existed hold NULL — read those as "all",
    the same widest-scope default a new role gets. Without this, one legacy row
    makes the whole roles list fail to serialize."""
    return DEFAULT_DATA_SCOPE if value is None else _normalize_scope(value)


# On the way in: a scope must be one of the two. On the way out: NULL from an old
# row reads as the default rather than failing.
DataScope = Annotated[
    str, BeforeValidator(_normalize_scope), Field(pattern=f"^({'|'.join(DATA_SCOPES)})$")
]
DataScopeOut = Annotated[str, BeforeValidator(_scope_or_default)]

_SCOPE_HELP = (
    "all = the role sees every record in the firm; "
    "own = list endpoints return only records assigned to or created by the "
    "logged-in user (customers, leads, quotations, sales orders, deliveries); "
    "team = records owned by the logged-in user plus records owned by anyone "
    "currently on the same Team (see app.core.scoping) -- applies to leads, "
    "customers, visits, follow-ups, quotations and sales orders; a user with "
    "no Team falls back to own-only"
)


class RoleOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    organization_id: str
    name: str
    workspace: str | None = None
    description: str | None = None
    data_scope: DataScopeOut = Field(default=DEFAULT_DATA_SCOPE, description=_SCOPE_HELP)
    is_default: bool
    permissions: PermissionMatrix
    # How many staff currently hold this role — the Roles screen shows it, and it
    # is why a delete can be refused.
    assigned_users: int = 0
    created_at: datetime
    updated_at: datetime


class RoleCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    workspace: str | None = Field(
        default=None, max_length=50,
        description="Which dashboard to open after login, e.g. sales / delivery / accounts",
    )
    description: str | None = Field(default=None, max_length=500)
    data_scope: DataScope = Field(default="all", description=_SCOPE_HELP)
    permissions: PermissionMatrix = Field(default_factory=dict)


class RoleUpdate(BaseModel):
    """Partial update — only the fields you send change. `permissions` is a full
    replace of the matrix, so send the whole thing as the Edit Role screen has it."""

    name: str | None = Field(default=None, min_length=1, max_length=100)
    workspace: str | None = Field(default=None, max_length=50)
    description: str | None = Field(default=None, max_length=500)
    data_scope: DataScope | None = Field(default=None, description=_SCOPE_HELP)
    permissions: PermissionMatrix | None = None


class RoleBriefOut(BaseModel):
    """The role as /auth/me reports it — what the frontend routes on."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    workspace: str | None = None
    data_scope: DataScopeOut = DEFAULT_DATA_SCOPE
    is_default: bool = False
