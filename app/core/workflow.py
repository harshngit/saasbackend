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

# ------------------------- public (client-facing) Order status -------------------------
# The internal lifecycle above (draft / awaiting_approval / processing / …) is richer
# than what the client should ever see — draft/awaiting_approval/placed all mean "not
# yet confirmed" to a customer, and "processing" must never leak out as anything but
# "confirmed". This maps ONLY at the API response boundary: the stored `status` value,
# every transition above and every internal comparison against it are unaffected.
# Centralized here — the one place every Order response (list/detail/create/update)
# reads from — so no router or schema keeps its own copy that could drift.
PUBLIC_ORDER_STATUS: dict[str, str] = {
    "draft": "placed",
    "awaiting_approval": "placed",
    "placed": "placed",
    "processing": "confirmed",
    "completed": "completed",
    "cancelled": "cancelled",
}


def public_order_status(value: str | None) -> str | None:
    """The client-facing Order status for an internal `SalesOrder.status` value.

    Never touches the stored value. An internal status this map does not recognise
    (should not happen — migrate_order_statuses backfills any row still on the old
    vocabulary) is passed through unchanged rather than raising, so a stray value
    never turns into a 500.
    """
    if value is None:
        return None
    return PUBLIC_ORDER_STATUS.get(value, value)

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

# ------------------------- public (client-facing) Delivery status -------------------------
# Boundary-only mapping for the Delivery's own `status` column — the internal
# warehouse vocabulary above is unchanged; this only shapes what a client sees.
#
# `planned` has no partner-acceptance guarantee yet (a delivery can be planned with
# no partner named), so it reads as `pending`, not `accepted`/`assigned` — matching
# the canonical public lifecycle's first state.
#
# `ready` is still prep (nothing has moved) so it stays folded into `accepted`.
# `loaded` means physical stock has already left the warehouse onto the vehicle —
# that is the transport phase, so it is reported as `in_transit`, not `accepted`,
# even though the formal `dispatch()` stamp (and the partner's own visibility) only
# happens one step later. A client should never be told less progress has been made
# than actually has.
#
# `rejected` is its own public value, not folded into `pending`: a client should be
# able to see that a rejection genuinely happened. It is not terminal in this
# architecture (a rejected delivery can be reassigned to a new partner, see
# DELIVERY_TRANSITIONS below), so it is not folded into `cancelled` either — that
# would misrepresent it as dead when the firm is actively re-routing it.
#
# `failed` means nothing was handed over on an attempt — goods are still on the
# vehicle, not back in the warehouse — reported as `returned`.
PUBLIC_DELIVERY_STATUS: dict[str, str] = {
    "planned": "pending",
    "rejected": "rejected",
    "accepted": "accepted",
    "ready": "accepted",
    "loaded": "in_transit",
    "in_transit": "in_transit",
    "partially_delivered": "partially_delivered",
    "delivered": "delivered",
    "failed": "returned",
    "cancelled": "cancelled",
}


def public_delivery_status(value: str | None) -> str | None:
    """The client-facing Delivery status for an internal `Delivery.status` value.

    Same non-destructive, defensive behaviour as public_order_status: the stored
    value is untouched, and an unrecognised value passes through unchanged.
    """
    if value is None:
        return None
    return PUBLIC_DELIVERY_STATUS.get(value, value)


# --------------------- centralized Delivery transition validation ---------------------
# The exact set of `Delivery.status` transitions the existing workflow already
# enforces — extracted from delivery_service.py's own guard clauses (accept/reject/
# load/dispatch/confirm) and the reassignment/cancel branches in
# routers/deliveries.py::update_delivery — collected into one table so every caller
# validates against the same rules instead of each keeping its own ad-hoc check.
#
# `rejected -> planned` is the existing reassign-to-a-new-partner flow.
# `rejected -> cancelled` and `planned/accepted/ready -> cancelled` are allowed only
# while nothing has been loaded yet (loaded_total == 0) — callers check that
# separately, since it is a quantity check, not a status-transition rule.
# `X -> X` (no-op) is always allowed and is not listed explicitly.
DELIVERY_TRANSITIONS: dict[str, set[str]] = {
    "planned": {"accepted", "rejected", "cancelled"},
    "rejected": {"planned", "cancelled"},
    "accepted": {"ready", "cancelled"},
    "ready": {"loaded", "cancelled"},
    "loaded": {"in_transit"},
    "in_transit": {"partially_delivered", "delivered", "failed"},
    "partially_delivered": {"partially_delivered", "delivered", "failed"},
    "delivered": set(),           # terminal
    "failed": set(),              # terminal (goods stay on the vehicle; handled by the return-to-warehouse flow)
    "cancelled": set(),           # terminal
}


class DeliveryTransitionError(Exception):
    """Raised by validate_delivery_transition for a disallowed status change."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def validate_delivery_transition(current: str, new: str) -> None:
    """Raise DeliveryTransitionError unless `current -> new` is an allowed Delivery
    status transition. The single source of truth for delivery workflow rules —
    every service function or router that changes `Delivery.status` calls this
    instead of duplicating its own inline check.
    """
    if new == current:
        return
    allowed = DELIVERY_TRANSITIONS.get(current, set())
    if new not in allowed:
        raise DeliveryTransitionError(f"Cannot move a delivery from '{current}' to '{new}'")


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


# --------------------------------- leads ------------------------------------
LEAD_STATUSES = ("new", "contacted", "qualified", "won", "lost")

# Manual (PATCH) transitions only. `won` is deliberately absent from every
# allowed set below — it is reachable exclusively through a successful
# POST /leads/{id}/convert-to-customer, never by a direct status write.
# `lost` is treated as terminal: no product requirement defines a "reopen a
# lost lead" flow, so the safest default (no transitions out) is chosen here
# rather than inventing one.
# `X -> X` (no-op) is always allowed and is not listed explicitly.
LEAD_TRANSITIONS: dict[str, set[str]] = {
    "new": {"contacted", "qualified", "lost"},
    "contacted": {"qualified", "lost"},
    "qualified": {"lost"},
    "won": set(),    # terminal — only convert-to-customer may set this
    "lost": set(),   # terminal — no reopen flow defined by product spec
}


class LeadTransitionError(Exception):
    """Raised by validate_lead_transition for a disallowed status change."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def validate_lead_transition(current: str, new: str) -> None:
    """Raise LeadTransitionError unless `current -> new` is an allowed manual
    Lead status change. The single source of truth for Lead workflow rules —
    every caller that changes `Lead.lead_status` via PATCH validates against
    this instead of duplicating its own inline check.

    `won` is never a valid destination here on purpose: it is set only by a
    successful conversion (see lead_service.convert_lead_to_customer), which
    writes `lead_status` directly rather than going through this function.
    """
    if new == current:
        return
    if new == "won":
        raise LeadTransitionError(
            "A Lead's status cannot be set to 'won' directly — "
            "use POST /leads/{id}/convert-to-customer"
        )
    if new not in LEAD_STATUSES:
        raise LeadTransitionError(f"'{new}' is not a valid Lead status")
    allowed = LEAD_TRANSITIONS.get(current, set())
    if new not in allowed:
        raise LeadTransitionError(f"Cannot move a Lead from '{current}' to '{new}'")
