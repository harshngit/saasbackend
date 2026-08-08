"""Assemble the sectioned customer profile from the flat row plus its history."""

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.schemas.customer_profile import (
    SECTION_FIELDS,
    AdditionalInformation,
    AddressInformation,
    BasicInformation,
    BusinessTaxInformation,
    ContactInformation,
    CustomerProfileOut,
    DocumentsSection,
    FinancialSummary,
    GeoLocation,
    NamedRef,
    Preferences,
    PaymentInformation,
    SalesCrmInformation,
    SalesSummary,
    SocialMediaOnlinePresence,
)
from app.schemas.customer_profile import COLUMN_FOR

_SECTION_MODELS = {
    "basic_information": BasicInformation,
    "contact_information": ContactInformation,
    "address_information": AddressInformation,
    "business_tax_information": BusinessTaxInformation,
    "payment_information": PaymentInformation,
    "sales_crm_information": SalesCrmInformation,
    "social_media_online_presence": SocialMediaOnlinePresence,
    "additional_information": AdditionalInformation,
    "preferences": Preferences,
}

# The named single-file document slots, and the profile key each fills.
NAMED_DOCUMENT_TYPES = {
    "gst_certificate": "gst_certificate_id",
    "pan_card": "pan_card_id",
    "business_registration_certificate": "business_registration_certificate_id",
    "address_proof": "address_proof_id",
    "purchase_agreement": "purchase_agreement_id",
}
OTHER_DOCUMENT_TYPE = "other"
DOCUMENT_TYPES = list(NAMED_DOCUMENT_TYPES) + [OTHER_DOCUMENT_TYPE]


def _section(customer, name: str):
    model = _SECTION_MODELS[name]
    values = {}
    for field in SECTION_FIELDS[name]:
        values[field] = getattr(customer, COLUMN_FOR.get(field, field), None)
    # Nullable JSON columns are coerced to [] by the schema's StringList type,
    # so nothing to do here.
    block = model(**values)
    if name == "sales_crm_information":
        officer = customer.assigned_sales_officer
        block.sales_representative = (
            NamedRef(id=officer.id, name=officer.name) if officer is not None else None
        )
    if name == "address_information":
        block.google_maps_location = GeoLocation(
            latitude=customer.maps_latitude, longitude=customer.maps_longitude
        )
    return block


def documents_section(customer) -> DocumentsSection:
    """The named slots hold the most recent upload of that type; everything of
    type `other` is listed."""
    section = DocumentsSection()
    for document in sorted(customer.documents or [], key=lambda d: d.uploaded_at):
        key = NAMED_DOCUMENT_TYPES.get(document.document_type)
        if key:
            setattr(section, key, document.id)
        elif document.document_type == OTHER_DOCUMENT_TYPE:
            section.other_document_ids.append(document.id)
    return section


def financial_summary(customer) -> FinancialSummary:
    credit_limit = customer.credit_limit or 0
    outstanding = customer.outstanding_balance or 0
    return FinancialSummary(
        opening_balance=customer.opening_balance or 0,
        total_billed=customer.total_billed or 0,
        total_received=customer.total_received or 0,
        outstanding_balance=outstanding,
        credit_limit=credit_limit,
        # Never negative: a customer over their limit has no credit left, not
        # "minus credit".
        available_credit=round(max(credit_limit - outstanding, 0), 2),
    )


def sales_summary(db: Session, customer) -> SalesSummary:
    """Order counts and lifetime value, read from the customer's invoices.

    Credit notes are excluded — they are reversals, not purchases."""
    from app.models import Invoice

    row = (
        db.query(
            func.count(Invoice.id),
            func.coalesce(func.sum(Invoice.total), 0.0),
            func.max(Invoice.invoice_date),
        )
        .filter(
            Invoice.customer_id == customer.id,
            Invoice.organization_id == customer.organization_id,
            Invoice.is_credit_note.is_(False),
        )
        .one()
    )
    count, total, last_date = row
    return SalesSummary(
        total_orders=count or 0,
        total_purchases=round(total or 0, 2),
        last_purchase_date=last_date,
        customer_lifetime_value=round(total or 0, 2),
    )


def build_profile(db: Session, customer) -> CustomerProfileOut:
    return CustomerProfileOut(
        id=customer.id,
        customer_id=customer.customer_id,
        organization_id=customer.organization_id,
        **{name: _section(customer, name) for name in _SECTION_MODELS},
        documents=documents_section(customer),
        financial_summary=financial_summary(customer),
        sales_summary=sales_summary(db, customer),
        is_active=customer.is_active,
        created_at=customer.created_at,
        updated_at=customer.updated_at,
    )
