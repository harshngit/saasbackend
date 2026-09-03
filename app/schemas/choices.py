"""Validated choice fields for the values the field sheet spells out in full.

Only lists the sheet closes are enforced. Where it ends with "etc." the value
stays free text and the list is served as a dropdown suggestion instead —
rejecting a value the client legitimately needs is worse than allowing an odd one.

Matching ignores case and separators, so "bank transfer", "Bank Transfer" and
"BANK_TRANSFER" all land on the sheet's own spelling.
"""

from typing import Annotated

from pydantic import BeforeValidator

from app.core.reference_data import (
    APPROVAL_STATUSES,
    CUSTOMER_STATUSES,
    CUSTOMER_TYPES,
    EXPENSE_PAYMENT_STATUSES,
    EXPENSE_STATUSES_SHEET,
    INVOICE_PAYMENT_STATUSES,
    LEAD_SOURCES,
    PURCHASE_PAYMENT_STATUSES,
    PURCHASE_STATUSES,
    PURCHASE_TYPES,
    RECEIVING_STATUSES,
    RETURN_STATUSES,
    RETURN_TYPES,
    SALES_STATUSES,
    SALES_TYPES,
)


def _key(value: str) -> str:
    return value.strip().lower().replace("-", " ").replace("_", " ")


def one_of(allowed: list[str], label: str):
    """Validator that normalises to the sheet's spelling, or explains the options."""

    lookup = {_key(a): a for a in allowed}

    def check(value: object) -> object:
        if value is None or value == "":
            return None
        if not isinstance(value, str):
            return value
        match = lookup.get(_key(value))
        if match is None:
            raise ValueError(f"{label} must be one of: {', '.join(allowed)}")
        return match

    return BeforeValidator(check)


CustomerStatus = Annotated[str, one_of(CUSTOMER_STATUSES, "status")]
CustomerType = Annotated[str, one_of(CUSTOMER_TYPES, "customer_type")]
SalesType = Annotated[str, one_of(SALES_TYPES, "sales_type")]
SalesStatus = Annotated[str, one_of(SALES_STATUSES, "sales_status")]
InvoicePaymentStatus = Annotated[str, one_of(INVOICE_PAYMENT_STATUSES, "payment_status")]
PurchaseType = Annotated[str, one_of(PURCHASE_TYPES, "purchase_type")]
PurchaseStatus = Annotated[str, one_of(PURCHASE_STATUSES, "purchase_status")]
ReceivingStatus = Annotated[str, one_of(RECEIVING_STATUSES, "receiving_status")]
PurchasePaymentStatus = Annotated[str, one_of(PURCHASE_PAYMENT_STATUSES, "payment_status")]
ExpenseStatus = Annotated[str, one_of(EXPENSE_STATUSES_SHEET, "expense_status")]
ExpensePaymentStatus = Annotated[str, one_of(EXPENSE_PAYMENT_STATUSES, "payment_status")]
ApprovalStatus = Annotated[str, one_of(APPROVAL_STATUSES, "approval_status")]
ReturnType = Annotated[str, one_of(RETURN_TYPES, "return_type")]
ReturnStatus = Annotated[str, one_of(RETURN_STATUSES, "return_status")]
LeadSource = Annotated[str, one_of(LEAD_SOURCES, "lead_source")]
