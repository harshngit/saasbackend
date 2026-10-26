"""staff mobile OTP login: users.staff_phone_e164 (unique), phone_verified, last_login,
and auth_rate_limit_events

`users.staff_phone_e164` holds a staff account's `phone` in canonical +91XXXXXXXXXX
form, under a unique index: one staff mobile number identifies one account for OTP
login. Admins and super admins stay NULL, so they may share numbers. `users.phone` is
never rewritten.

Backfill: a staff number is copied in only when no other staff account holds the same
number. Duplicated numbers are left NULL on every account sharing them -- choosing one
would be a guess -- so none of them can sign in by OTP until an admin corrects them.
Counts are logged; numbers are not.

`users.phone_verified` (NOT NULL, default false) records a successful phone-OTP sign-in;
`users.last_login` is stamped on every sign-in. Existing rows get false / NULL.

`auth_rate_limit_events` counts OTP send / failed-verify and password-reset requests per
hashed key, shared by every app instance.

Guarded throughout (alembic/README.md), following d8e9f0a1b2c3: safe on a database
whose app startup already created the table (create_all) or added the column
(auto_add_missing_columns).

Revision ID: f5a6b7c8d9e0
Revises: d8e9f0a1b2c3
Create Date: 2026-10-26 18:00:00.000000
"""
import logging
from collections import defaultdict
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f5a6b7c8d9e0"
down_revision: Union[str, Sequence[str], None] = "d8e9f0a1b2c3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

logger = logging.getLogger("alembic.runtime.migration")

_USERS = "users"
_COLUMN = "staff_phone_e164"
_COLUMN_INDEX = "ix_users_staff_phone_e164"
_EVENTS = "auth_rate_limit_events"
_EVENT_INDEXES = (
    ("ix_auth_rate_limit_events_lookup", ["scope", "key_hash", "created_at"]),
    ("ix_auth_rate_limit_events_created_at", ["created_at"]),
)
# Legacy enum names (stored uppercase) that make a user not staff when system_role is unset.
_NON_STAFF_LEGACY_ROLES = {"ADMIN", "SUPER_ADMIN"}


def _normalize(raw):
    """Same rules as app.core.phone.normalize_indian_mobile (libphonenumber), so the
    backfilled value is exactly what an OTP lookup will search for."""
    import phonenumbers
    from phonenumbers import PhoneNumberFormat, PhoneNumberType

    if not raw or not raw.strip():
        return None
    try:
        number = phonenumbers.parse(raw.strip(), "IN")
    except phonenumbers.NumberParseException:
        return None
    if (
        phonenumbers.region_code_for_number(number) != "IN"
        or not phonenumbers.is_valid_number(number)
        or phonenumbers.number_type(number) not in (PhoneNumberType.MOBILE, PhoneNumberType.FIXED_LINE_OR_MOBILE)
    ):
        return None
    return phonenumbers.format_number(number, PhoneNumberFormat.E164)


def _is_staff(system_role, role) -> bool:
    if system_role:
        return system_role == "staff"
    return (str(role).upper() if role is not None else None) not in _NON_STAFF_LEGACY_ROLES


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = inspector.get_table_names()

    if _EVENTS not in tables:
        logger.info("Creating %s", _EVENTS)
        op.create_table(
            _EVENTS,
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("scope", sa.String(length=40), nullable=False),
            sa.Column("key_hash", sa.String(length=64), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )
    event_indexes = {ix["name"] for ix in sa.inspect(bind).get_indexes(_EVENTS)}
    for name, columns in _EVENT_INDEXES:
        if name not in event_indexes:
            op.create_index(name, _EVENTS, columns)

    if _USERS not in tables:
        logger.info("%s table does not exist yet -- skipping %s", _USERS, _COLUMN)
        return
    user_columns = {c["name"] for c in inspector.get_columns(_USERS)}
    if _COLUMN not in user_columns:
        logger.info("Adding %s.%s", _USERS, _COLUMN)
        op.add_column(_USERS, sa.Column(_COLUMN, sa.String(length=16), nullable=True))
    if "phone_verified" not in user_columns:
        logger.info("Adding %s.phone_verified", _USERS)
        op.add_column(_USERS, sa.Column("phone_verified", sa.Boolean(), nullable=False, server_default=sa.false()))
    if "last_login" not in user_columns:
        logger.info("Adding %s.last_login", _USERS)
        op.add_column(_USERS, sa.Column("last_login", sa.DateTime(timezone=True), nullable=True))

    rows = bind.execute(sa.text(
        f'SELECT "id", "phone", "system_role", "role", "{_COLUMN}" FROM "{_USERS}" WHERE "phone" IS NOT NULL'
    )).fetchall()
    taken = {row[4] for row in rows if row[4]}
    holders = defaultdict(list)
    for user_id, phone, system_role, role, current in rows:
        if not _is_staff(system_role, role):
            continue
        e164 = _normalize(phone)
        if e164:
            holders[e164].append((user_id, current))
    filled = shared = 0
    for e164, accounts in holders.items():
        if len(accounts) > 1:
            shared += len(accounts)
            continue
        user_id, current = accounts[0]
        if current is None and e164 not in taken:
            bind.execute(
                sa.text(f'UPDATE "{_USERS}" SET "{_COLUMN}" = :e164 WHERE "id" = :id'),
                {"e164": e164, "id": user_id},
            )
            taken.add(e164)
            filled += 1
    logger.info(
        "%s backfill: %d staff account(s) filled; %d staff account(s) share a number and were left NULL",
        _COLUMN, filled, shared,
    )

    if _COLUMN_INDEX not in {ix["name"] for ix in sa.inspect(bind).get_indexes(_USERS)}:
        op.create_index(_COLUMN_INDEX, _USERS, [_COLUMN], unique=True)


def downgrade() -> None:
    """Reverses what this revision is guaranteed to have added: the unique index and the
    auth_rate_limit_events table. The users columns (staff_phone_e164, phone_verified,
    last_login) are kept (alembic/README.md rule 5) -- app startup may have added them,
    and they may hold data."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = inspector.get_table_names()

    if _USERS in tables and _COLUMN_INDEX in {ix["name"] for ix in inspector.get_indexes(_USERS)}:
        op.drop_index(_COLUMN_INDEX, table_name=_USERS)
    if _EVENTS in tables:
        op.drop_table(_EVENTS)
