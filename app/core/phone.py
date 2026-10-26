"""Indian mobile numbers: one canonical form for comparing what users type with what
staff records hold.

Stored `users.phone` values are never rewritten -- older rows hold plain 10-digit
numbers, others `+91…`, `91…` or `0…`, sometimes with spaces, hyphens or brackets.
Everything here compares through the canonical E.164 form `+91XXXXXXXXXX` instead.
Parsing and validation are Google's libphonenumber rules (`phonenumbers`).
"""
import phonenumbers
from phonenumbers import PhoneNumberFormat, PhoneNumberType
from sqlalchemy import func

_MOBILE_TYPES = {PhoneNumberType.MOBILE, PhoneNumberType.FIXED_LINE_OR_MOBILE}


def normalize_indian_mobile(raw: str | None) -> str | None:
    """`+91XXXXXXXXXX` for a valid Indian mobile number, else None.

    Accepts the national form (`9876543210`, `09876543210`) or the international one
    (`+91 98765-43210`, `919876543210`). Landlines, other countries' numbers and
    anything libphonenumber does not consider a valid Indian mobile are not guessed at.
    """
    if not raw or not raw.strip():
        return None
    try:
        number = phonenumbers.parse(raw.strip(), "IN")
    except phonenumbers.NumberParseException:
        return None
    if (
        phonenumbers.region_code_for_number(number) != "IN"
        or not phonenumbers.is_valid_number(number)
        or phonenumbers.number_type(number) not in _MOBILE_TYPES
    ):
        return None
    return phonenumbers.format_number(number, PhoneNumberFormat.E164)


def mask(e164: str | None) -> str:
    """Log-safe form of a number: only its last two digits."""
    return f"+91******{e164[-2:]}" if e164 and len(e164) >= 2 else "<none>"


def stored_phone_matches(column, e164: str):  # noqa: ANN001
    """SQL condition: a stored phone column holds `e164` in any accepted spelling.

    Spaces, hyphens, brackets and `+` are stripped in SQL (same on SQLite and
    PostgreSQL), then compared against the bare, `91`- and `0`-prefixed ten digits --
    so `+91 (98765) 43210` matches while nothing is rewritten.
    """
    digits = e164[3:]
    bare = column
    for char in (" ", "-", "(", ")", "+"):
        bare = func.replace(bare, char, "")
    return bare.in_([digits, f"91{digits}", f"0{digits}"])
