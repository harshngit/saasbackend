"""Restrict a query to the records a staff user owns.

Two layers guard every business list endpoint:

1. **Organization** — always applied, from the authenticated user's
   `organization_id`. A client never sends it, and cannot widen it.
2. **Ownership** — applied only when the user's role has `data_scope == "own"`
   (the field roles: Sales Officer, Delivery Partner). Back-office roles keep
   `data_scope == "all"` and see the whole firm, which is the default, so nothing
   narrows unless an Admin asks for it on the Roles screen.

Admins and the Super Admin are never narrowed inside their own scope.

Each module says which columns mean "mine" — a customer is mine if I am its sales
representative, an order is mine if I raised it or it is out for my delivery — and
`owned_by` ORs them together.
"""

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.models import User
from app.services import role_service


def scope_to_own(db: Session, user: User) -> bool:
    """Whether this user's list results must be narrowed to their own records."""
    return role_service.data_scope(db, user) == "own"


def owned_by(query, db: Session, user: User, *columns):  # noqa: ANN001, ANN002
    """Narrow `query` to rows where any of `columns` is this user, if their role
    says so. `columns` are the model columns that mean "belongs to this user"."""
    if not columns or not scope_to_own(db, user):
        return query
    return query.filter(or_(*[column == user.id for column in columns]))


def owns_record(db: Session, user: User, record, *attributes: str) -> bool:  # noqa: ANN001
    """The same test for a single record, so a detail / edit / delete route cannot
    reach past what the list would have shown. True when the scope is "all"."""
    if not scope_to_own(db, user):
        return True
    return any(getattr(record, attribute, None) == user.id for attribute in attributes)
