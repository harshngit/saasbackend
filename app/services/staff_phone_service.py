"""Staff mobile numbers: platform-wide uniqueness and the OTP-login account lookup.

Only staff numbers are unique -- admins and super admins may share a number with anyone.
`users.phone` is stored exactly as entered; `users.staff_phone_e164` holds its canonical
form for staff only, under a unique index. Rows written before that column existed may
not have it filled in, so every check here also compares the stored `phone` itself.
"""
from fastapi import HTTPException, status
from sqlalchemy import and_, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.phone import normalize_indian_mobile, stored_phone_matches
from app.models import SystemRole, User, UserRole

DUPLICATE_DETAIL = "This mobile number is already assigned to another staff account"


def _is_staff():
    """SQL twin of User.effective_system_role == "staff" (system_role, else the legacy role)."""
    return or_(
        User.system_role == SystemRole.STAFF.value,
        and_(
            or_(User.system_role.is_(None), User.system_role == ""),
            or_(User.role.is_(None), User.role.notin_([UserRole.ADMIN, UserRole.SUPER_ADMIN])),
        ),
    )


def _staff_with_number(db: Session, e164: str) -> list[User]:
    """Every staff account, in any firm and any status, holding this number."""
    return (
        db.query(User)
        .filter(_is_staff(), or_(User.staff_phone_e164 == e164, stored_phone_matches(User.phone, e164)))
        .all()
    )


def sync_staff_phone(db: Session, user: User) -> None:
    """Refresh `staff_phone_e164` from `user.phone`, refusing a number another staff
    account already holds (409). Call after writing `phone` and before committing.

    A value that is not a valid Indian mobile is still stored in `phone` as before, but
    gives no OTP login and takes part in no uniqueness check.
    """
    e164 = normalize_indian_mobile(user.phone) if user.effective_system_role == SystemRole.STAFF.value else None
    if e164 is not None and any(other.id != user.id for other in _staff_with_number(db, e164)):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=DUPLICATE_DETAIL)
    if e164 != user.staff_phone_e164:
        user.phone_verified = False  # a verification belongs to the number, not the account
    user.staff_phone_e164 = e164
    try:
        db.flush()
    except IntegrityError:  # a concurrent request took the same number first
        db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=DUPLICATE_DETAIL)


def resolve_otp_account(db: Session, e164: str) -> User | None:
    """The one account an OTP for this number may sign in, or None.

    Only an active staff account inside a firm qualifies -- never an admin or super
    admin. When the number matches more than one such account (legacy duplicates), no
    one is chosen.
    """
    eligible = [
        user for user in _staff_with_number(db, e164)
        if user.is_active
        and user.organization_id is not None
        and user.effective_system_role == SystemRole.STAFF.value
    ]
    return eligible[0] if len(eligible) == 1 else None
