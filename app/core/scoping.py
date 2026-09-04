"""Restrict a query to the records a staff user owns.

Two layers guard every business list endpoint:

1. **Organization** — always applied, from the authenticated user's
   `organization_id`. A client never sends it, and cannot widen it.
2. **Ownership** — applied only when the user's role has `data_scope` of
   `"own"` or `"team"` (the field roles: Sales Officer, Delivery Partner, and
   any custom role an Admin sets up this way). Back-office roles keep
   `data_scope == "all"` and see the whole firm, which is the default, so
   nothing narrows unless an Admin asks for it on the Roles screen.

Admins and the Super Admin are never narrowed inside their own scope.

Each module says which columns mean "mine" — a customer is mine if I am its sales
representative, an order is mine if I raised it or it is out for my delivery — and
`owned_by` ORs them together.

**Team scope** (`data_scope == "team"`) widens "mine" to "mine, plus anyone
currently on my Team" (`app.models.team.Team` / `User.team_id`). Team
membership is resolved fresh on every call — a Team is never copied onto a
CRM record, so moving a user between Teams changes what they and their new
teammates can see immediately, with no backfill. A user with `data_scope ==
"team"` but no Team (`team_id is None`) safely degrades to own-only: never a
crash, never an org-wide fallback. In practice this only ever changes
behavior for whichever modules a router actually calls `owned_by`/
`owns_record` on for that role's permissions — there is nothing team-specific
to configure per module, the same way "own" already works everywhere without
per-module wiring.
"""

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models import User
from app.services import role_service


def _team_member_ids(user: User):
    """A SQL subquery of user ids sharing `user`'s current Team — for
    query-side filtering (`.in_(...)`), never a Python loop over rows. Only
    meaningful when `user.team_id` is set; callers check that first.
    """
    return select(User.id).where(User.team_id == user.team_id)


def scope_to_team(db: Session, user: User) -> bool:
    """Whether this user's list results must be widened to their Team (their
    own records plus their current teammates'). False for a "team"-scope
    user with no Team — that degrades to own-only via scope_to_own instead.
    """
    return role_service.data_scope(db, user) == "team" and user.team_id is not None


def scope_to_own(db: Session, user: User) -> bool:
    """Whether this user's list results must be narrowed to their own records
    only. True for `data_scope == "own"`, and also for `data_scope == "team"`
    when the user currently has no Team — the documented safe fallback.
    """
    scope = role_service.data_scope(db, user)
    if scope == "own":
        return True
    return scope == "team" and user.team_id is None


def owned_by(query, db: Session, user: User, *columns, team_columns=None):  # noqa: ANN001, ANN002
    """Narrow `query` to rows where any of `columns` is this user, if their
    role says so. `columns` are the model columns that mean "belongs to this
    user" — unchanged, "own"-scope behavior.

    Under team scope, `team_columns` (a subset of `columns`; defaults to all
    of `columns` when omitted) *also* match a current teammate, not just this
    user — the rest of `columns` still only ever mean "== this user", scope
    or no scope. Every call site with a single ownership column needs no
    change at all (the default covers it); Orders is the one case with
    several columns that mean different things — created_by and
    assigned_delivery_partner_id are "own" concepts, not a Team-Scope
    ownership field, so its caller passes `team_columns=(SalesOrder.salesperson_id,)`
    to keep the widening to exactly that field.
    """
    if not columns:
        return query
    if scope_to_team(db, user):
        team_cols = set(team_columns) if team_columns is not None else set(columns)
        member_ids = _team_member_ids(user)
        clauses = [
            column.in_(member_ids) if column in team_cols else column == user.id
            for column in columns
        ]
        return query.filter(or_(*clauses))
    if scope_to_own(db, user):
        return query.filter(or_(*[column == user.id for column in columns]))
    return query


def owns_record(db: Session, user: User, record, *attributes: str, team_attributes=None) -> bool:  # noqa: ANN001
    """The same test for a single record, so a detail / edit / delete route cannot
    reach past what the list would have shown. True when the scope is "all".

    `team_attributes` mirrors `owned_by`'s parameter of the same name: the
    subset of `attributes` eligible for team-widening (defaults to all of
    them).
    """
    if scope_to_team(db, user):
        team_attrs = set(team_attributes) if team_attributes is not None else set(attributes)
        own_ids = {getattr(record, a, None) for a in attributes if a not in team_attrs}
        if user.id in own_ids:
            return True
        team_owner_ids = {getattr(record, a, None) for a in team_attrs}
        team_owner_ids.discard(None)
        if user.id in team_owner_ids:
            return True
        if not team_owner_ids:
            return False
        # One query, not one per candidate attribute/owner: is any owner id
        # among this user's current teammates?
        return (
            db.query(User.id)
            .filter(User.team_id == user.team_id, User.id.in_(team_owner_ids))
            .first()
            is not None
        )
    if scope_to_own(db, user):
        return any(getattr(record, attribute, None) == user.id for attribute in attributes)
    return True
