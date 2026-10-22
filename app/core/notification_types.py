"""The one notification contract shared by in-app notifications and push.

Every business event has an *event* name (e.g. ``delivery_assigned``). An in-app notification
keeps its long-standing ``type`` column as the event's broad *category* (``order``,
``delivery``, ``expense``, ``success`` …) -- the web app picks icons and navigation from those
values -- and stores the event name in ``data["event"]``. A push for the same notification
carries ``data.type = <event>`` plus the same ``notification_id``, so mobile clients route on the
event while the web app keeps working unchanged.

Data payloads hold identifiers only (strings), never amounts, names, tokens or documents.
"""

PUSH_TEST = "push_test"
ORDER_CREATED = "order_created"
ORDER_CONFIRMED = "order_confirmed"
ORDER_STATUS_CHANGED = "order_status_changed"
DELIVERY_ASSIGNED = "delivery_assigned"
DELIVERY_REJECTED = "delivery_rejected"
DELIVERY_STATUS_CHANGED = "delivery_status_changed"
PAYMENT_RECEIVED = "payment_received"
EXPENSE_CREATED = "expense_created"
EXPENSE_CLARIFICATION_REQUESTED = "expense_clarification_requested"
LEAVE_STATUS_CHANGED = "leave_status_changed"

# event -> in-app `type` (category). Existing categories are kept exactly as before.
EVENT_CATEGORY: dict[str, str] = {
    PUSH_TEST: "info",
    ORDER_CREATED: "order",
    ORDER_CONFIRMED: "order",
    ORDER_STATUS_CHANGED: "order",
    DELIVERY_ASSIGNED: "delivery",
    DELIVERY_REJECTED: "delivery",
    DELIVERY_STATUS_CHANGED: "delivery",
    PAYMENT_RECEIVED: "success",
    EXPENSE_CREATED: "expense",
    EXPENSE_CLARIFICATION_REQUESTED: "expense",
    LEAVE_STATUS_CHANGED: "leave",
}

# The identifier keys an event may carry in `data` (besides "event" and "status").
EVENT_DATA_KEYS: dict[str, tuple[str, ...]] = {
    ORDER_CREATED: ("order_id",),
    ORDER_CONFIRMED: ("order_id",),
    ORDER_STATUS_CHANGED: ("order_id", "status"),
    DELIVERY_ASSIGNED: ("delivery_id",),
    DELIVERY_REJECTED: ("delivery_id",),
    DELIVERY_STATUS_CHANGED: ("delivery_id", "status"),
    PAYMENT_RECEIVED: ("payment_id", "invoice_id"),
    EXPENSE_CREATED: ("expense_id",),
    EXPENSE_CLARIFICATION_REQUESTED: ("expense_id",),
    LEAVE_STATUS_CHANGED: ("leave_id", "status"),
}

ALL_EVENTS = frozenset(EVENT_CATEGORY)


def build_data(event: str, **ids) -> dict[str, str]:
    """The `data` dict for an event: {"event": ..., <allowed id keys as strings>}. Unknown
    keys are rejected so nothing beyond the documented identifiers can slip in."""
    if event not in ALL_EVENTS:
        raise ValueError(f"unknown notification event '{event}'")
    allowed = set(EVENT_DATA_KEYS.get(event, ()))
    unknown = set(ids) - allowed
    if unknown:
        raise ValueError(f"data keys {sorted(unknown)} are not part of the '{event}' contract")
    data = {"event": event}
    data.update({key: str(value) for key, value in ids.items() if value is not None and value != ""})
    return data
