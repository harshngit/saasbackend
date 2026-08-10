"""Record and read a firm's activity feed.

`record()` never commits — it joins whatever transaction the calling route is
already in, so an activity entry can never outlive the change it describes.
"""

from sqlalchemy.orm import Session

from app.models import ActivityLog, User

# Which company-profile columns belong to which part of the page, so an update to
# "Billing" reads as "Billing information changed" rather than a generic edit.
# Order matters only for readability; a field in none of these is company_profile.
FIELD_GROUPS: list[tuple[str, str, str, tuple[str, ...]]] = [
    (
        "authorized_person", "Authorized person updated", "Contact information updated",
        ("auth_person_name", "auth_person_designation", "auth_person_mobile",
         "auth_person_email", "auth_person_photo_url", "auth_person_signature_url",
         "owner_director_name", "designation", "mobile_number"),
    ),
    (
        "billing", "Billing information changed", "Payment terms updated",
        ("bank_name", "bank_ifsc", "bank_account_details", "bank_account_holder", "upi_id",
         "payment_qr_url", "google_pay_phonepe_paytm_qr_code", "currency",
         "tax_configuration", "invoice_settings", "financial_year"),
    ),
    (
        "address", "Company addresses updated", "Address information updated",
        ("registered_address", "branch_address", "branch_addresses", "address", "city",
         "state", "country", "pin_code", "pin_zip_code", "maps_latitude", "maps_longitude"),
    ),
    (
        "branding", "Branding updated", "Logo / identity assets changed",
        ("logo_url", "company_logo", "signature_url", "authorized_signature", "stamp_url",
         "letterhead_url", "banner_url"),
    ),
    (
        "online_presence", "Online presence updated", "Social links changed",
        ("website", "facebook_url", "instagram_url", "linkedin_url", "twitter_url",
         "youtube_url", "whatsapp_number"),
    ),
    (
        "document", "Business documents updated", "Document fields changed",
        ("doc_gst_url", "doc_pan_url", "doc_coi_url", "doc_trade_license_url",
         "doc_msme_url", "doc_fssai_url", "doc_other_url", "doc_other_files"),
    ),
]


def record(
    db: Session,
    organization_id: str | None,
    actor: User | None,
    type: str,
    title: str,
    description: str | None = None,
) -> ActivityLog:
    """Add one entry to the firm's feed. Does not commit."""
    entry = ActivityLog(
        organization_id=organization_id,
        user_id=actor.id if actor is not None else None,
        actor_name=actor.name if actor is not None else None,
        type=type,
        title=title,
        description=description,
    )
    db.add(entry)
    return entry


def record_field_changes(
    db: Session, organization_id: str | None, actor: User | None, changed: list[str]
) -> None:
    """Turn the set of columns a company-profile update touched into one entry per
    section of the page, the way the Recent Activity list reads them."""
    remaining = set(changed)
    for type_, title, description, fields in FIELD_GROUPS:
        touched = remaining.intersection(fields)
        if touched:
            remaining -= touched
            record(db, organization_id, actor, type_, title, description)
    if remaining:
        record(
            db, organization_id, actor, "company_profile", "Company profile updated",
            "General information has been updated",
        )


def recent(db: Session, organization_id: str | None, limit: int = 10) -> list[ActivityLog]:
    return (
        db.query(ActivityLog)
        .filter(ActivityLog.organization_id == organization_id)
        .order_by(ActivityLog.created_at.desc(), ActivityLog.id.desc())
        .limit(limit)
        .all()
    )
