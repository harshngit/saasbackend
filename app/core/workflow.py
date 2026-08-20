"""Per-organization workflow settings, and the order status vocabulary.

Every firm on the platform works slightly differently, so the transaction flow is
driven by settings on the organization rather than by role names or a hardcoded
policy. `settings_for()` is the only way to read them: it fills in the defaults, so
a firm that has never touched its settings behaves like the documented default and
a setting added later needs no data migration.

The important default is `order_requires_approval = false`. An order is validated,
reserved and placed on creation — an Admin is for organization control and
exceptions, not a mandatory step in every sale.
"""

# ------------------------------ order statuses ------------------------------
# Two independent axes. The order's own lifecycle:
ORDER_STATUSES = (
    "draft",
    "placed",
    "awaiting_approval",   # only when the firm turns approval on
    "processing",
    "completed",
    "cancelled",
)

# …and how far the goods have got. Payment and invoicing are deliberately not in
# either: a delivered, invoiced, unpaid order is perfectly valid for a credit
# customer, and forcing that into one status is what made the old model ambiguous.
FULFILMENT_STATUSES = (
    "not_started",
    "reserved",
    "planned",
    "loaded",
    "in_transit",
    "partially_delivered",
    "delivered",
    "failed",
)

# Where the old single-status column maps to. Rows written before the split are
# migrated through this, and a client still filtering by an old value is served
# through it too, so nothing breaks on the day of the change.
LEGACY_ORDER_STATUS = {
    "pending": ("awaiting_approval", "not_started"),
    "confirmed": ("processing", "reserved"),
    "processing": ("processing", "reserved"),
    "out_for_delivery": ("processing", "in_transit"),
    "partially_delivered": ("processing", "partially_delivered"),
    "delivered": ("completed", "delivered"),
    "failed": ("processing", "failed"),
    "returned": ("completed", "delivered"),
    "cancelled": ("cancelled", "not_started"),
    "rejected": ("cancelled", "not_started"),
}

# An order past this point counts as a realised sale for reporting.
SALE_ORDER_STATUSES = ("placed", "processing", "completed")

# Fulfilment states where goods have physically left the warehouse, so a plain
# cancel is no longer the right move — the return-to-warehouse flow is.
DISPATCHED_FULFILMENT = ("loaded", "in_transit", "partially_delivered", "delivered")


# --------------------------- workflow settings ----------------------------

SALES_WORKFLOW_DEFAULTS: dict[str, object] = {
    # Orders are placed straight away. Turn this on and an order lands in
    # awaiting_approval and the /approve and /reject routes come into play.
    "order_requires_approval": False,
    # Reserve stock when the order is placed rather than deducting it. On-hand is
    # only reduced when goods are actually loaded onto a vehicle.
    "reserve_stock_on_order": True,
    "allow_partial_delivery": True,
    # Refuse to place an order the warehouse cannot cover.
    "allow_backorder": False,
    "allow_direct_invoice": True,
    # What to do when an order would take a customer past their credit limit.
    "credit_limit_action": "warn",             # warn | block | ignore
    "delivery_collection_allowed": True,
    # Whether each delivery is billed as it happens, or the whole order once.
    "partial_delivery_invoice_mode": "per_delivery",  # per_delivery | after_full_order
}

# When True, `POST /orders` creates a `draft` order that does not reserve stock.
# A separate `POST /orders/{id}/confirm` call performs stock checks and reservation.
# Default False for backward compatibility.
SALES_WORKFLOW_DEFAULTS["draft_orders_enabled"] = False

SALES_WORKFLOW_CHOICES: dict[str, tuple[str, ...]] = {
    "credit_limit_action": ("warn", "block", "ignore"),
    "partial_delivery_invoice_mode": ("per_delivery", "after_full_order"),
}


# ----------------------------- invoice settings ----------------------------

INVOICE_TEMPLATES = ("classic", "modern", "compact", "thermal")
PAPER_SIZES = ("A4", "A5", "thermal")

INVOICE_FIELD_DEFAULTS: dict[str, bool] = {
    "show_company_gstin": True,
    "show_customer_gstin": True,
    "show_billing_address": True,
    "show_shipping_address": True,
    "show_hsn_sac": True,
    "show_mrp": False,
    "show_discount": True,
    "show_tax_rate": True,
    "show_tax_amount": True,
    "show_batch_number": False,
    "show_expiry_date": False,
    "show_bank_details": True,
    "show_upi_qr": True,
    "show_terms": True,
    "show_signature": True,
}

INVOICE_SETTINGS_DEFAULTS: dict[str, object] = {
    "template": "classic",
    "paper_size": "A4",
    "branding": {"logo_file_id": None, "signature_file_id": None, "primary_color": None},
    "fields": dict(INVOICE_FIELD_DEFAULTS),
    "terms": None,
    "footer_text": None,
    "notes": None,
}


def _merged(stored: object, defaults: dict) -> dict:
    """Stored values on top of the defaults, one level deep for the nested blocks."""
    result = {k: (dict(v) if isinstance(v, dict) else v) for k, v in defaults.items()}
    if isinstance(stored, dict):
        for key, value in stored.items():
            if key not in defaults:
                continue
            if isinstance(defaults[key], dict) and isinstance(value, dict):
                result[key].update({k: v for k, v in value.items() if k in defaults[key]})
            else:
                result[key] = value
    return result


def sales_settings(org) -> dict:  # noqa: ANN001
    """The firm's sales workflow settings, defaults filled in."""
    return _merged(getattr(org, "sales_workflow_settings", None), SALES_WORKFLOW_DEFAULTS)


def invoice_settings(org) -> dict:  # noqa: ANN001
    """The firm's invoice template settings, defaults filled in."""
    return _merged(getattr(org, "invoice_template_settings", None), INVOICE_SETTINGS_DEFAULTS)


# ------------------------------ deliveries --------------------------------
# A Delivery is the record the fulfilment half of the flow turns on, and its own id
# is the identifier every delivery endpoint takes. Its status tracks the goods:
DELIVERY_STATUSES = (
    "planned",              # partner and vehicle named, quantities planned
    "accepted",             # accepted by assigned partner
    "rejected",             # rejected by assigned partner
    "ready",                # picked and ready for loading
    "loaded",               # goods physically on the vehicle
    "in_transit",           # dispatched — only now is it live for the partner
    "partially_delivered",
    "delivered",
    "failed",
    "cancelled",
)

# The order's fulfilment_status each delivery status implies, so the two never drift.
ORDER_FULFILMENT_FOR_DELIVERY = {
    "planned": "planned",
    "accepted": "planned",
    "rejected": "planned",
    "ready": "planned",
    "loaded": "loaded",
    "in_transit": "in_transit",
    "partially_delivered": "partially_delivered",
    "delivered": "delivered",
    "failed": "failed",
}

# Deliveries whose goods are still out with the partner.
OPEN_DELIVERY_STATUSES = ("planned", "accepted", "ready", "loaded", "in_transit", "partially_delivered")


# ------------------------------ sales returns ------------------------------
# Goods coming back are a request first, not a stock movement. Nothing re-enters the
# warehouse until someone has physically received the goods, looked at them, and said
# they are fit to sell again.
RETURN_STATUSES = (
    "requested",   # the customer has asked to return goods
    "received",    # the goods are physically back with the firm
    "approved",    # checked and accepted — restocked if saleable, credit note raised
    "rejected",    # refused: nothing restocked, nothing credited
    "cancelled",   # withdrawn before it was decided
)

# A return can still be edited or withdrawn while it is in one of these.
OPEN_RETURN_STATUSES = ("requested", "received")

# The one condition that lets goods go back on the shelf. Anything else — damaged,
# expired, opened, whatever the firm writes — is accepted as a note on the line but
# never becomes saleable stock again.
SALEABLE_CONDITION = "saleable"


def is_saleable(condition: str | None) -> bool:
    """Whether goods in this condition may re-enter sellable stock."""
    return (condition or "").strip().lower() == SALEABLE_CONDITION
