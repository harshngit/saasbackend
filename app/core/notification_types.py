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
# Sales officer
LEAD_ASSIGNED = "lead_assigned"
FOLLOW_UP_DUE = "follow_up_due"
FOLLOW_UP_OVERDUE = "follow_up_overdue"
QUOTATION_STATUS_CHANGED = "quotation_status_changed"
# Expense lifecycle (status: approved / rejected / reimbursed)
EXPENSE_STATUS_CHANGED = "expense_status_changed"
EXPENSE_READY_FOR_REIMBURSEMENT = "expense_ready_for_reimbursement"
# Delivery Day-End
DAY_END_SUBMITTED = "day_end_submitted"
DAY_END_STATUS_CHANGED = "day_end_status_changed"
# Inventory (status: low_stock / out_of_stock)
STOCK_ALERT = "stock_alert"
# Platform scope: addressed to platform users (no organization), never to a tenant feed.
ORGANIZATION_CREATED = "organization_created"
UPGRADE_REQUESTED = "upgrade_requested"

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
    LEAD_ASSIGNED: "lead",
    FOLLOW_UP_DUE: "follow_up",
    FOLLOW_UP_OVERDUE: "follow_up",
    QUOTATION_STATUS_CHANGED: "quotation",
    EXPENSE_STATUS_CHANGED: "expense",
    EXPENSE_READY_FOR_REIMBURSEMENT: "expense",
    DAY_END_SUBMITTED: "day_end",
    DAY_END_STATUS_CHANGED: "day_end",
    STOCK_ALERT: "inventory",
    ORGANIZATION_CREATED: "platform",
    UPGRADE_REQUESTED: "platform",
}

# Events addressed to platform users. Their notifications carry organization_id = NULL,
# so they can never appear in -- or be counted against -- a tenant's notifications.
PLATFORM_EVENTS = frozenset({ORGANIZATION_CREATED, UPGRADE_REQUESTED})

# Keys any event may carry: who acted (a user id), and -- for an event whose subject can
# vary -- nothing else. Everything else is per event, below.
COMMON_DATA_KEYS: tuple[str, ...] = ("actor_id",)

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
    LEAD_ASSIGNED: ("lead_id",),
    FOLLOW_UP_DUE: ("follow_up_id", "lead_id", "customer_id"),
    FOLLOW_UP_OVERDUE: ("follow_up_id", "lead_id", "customer_id"),
    QUOTATION_STATUS_CHANGED: ("quotation_id", "status"),
    EXPENSE_STATUS_CHANGED: ("expense_id", "status"),
    EXPENSE_READY_FOR_REIMBURSEMENT: ("expense_id",),
    # has_variance: "true" when the handed-over total differs from the expected total.
    DAY_END_SUBMITTED: ("day_end_id", "delivery_partner_id", "has_variance"),
    DAY_END_STATUS_CHANGED: ("day_end_id", "status"),
    STOCK_ALERT: ("product_id", "variant_id", "warehouse_id", "status"),
    ORGANIZATION_CREATED: ("target_organization_id",),
    UPGRADE_REQUESTED: ("target_organization_id", "upgrade_request_id"),
}

ALL_EVENTS = frozenset(EVENT_CATEGORY)


# What a notification is about, and where it opens. Derived from the event and its stored
# identifiers when a notification is read (never stored), so rows written before these
# fields existed get them too. action_url is the REST resource path of the subject; each
# client maps it to its own screen.
#   event -> (entity_type, id key in data, action_url template)
_ENTITY: dict[str, tuple[str, str, str]] = {
    ORDER_CREATED: ("sales_order", "order_id", "/sales-orders/{id}"),
    ORDER_CONFIRMED: ("sales_order", "order_id", "/sales-orders/{id}"),
    ORDER_STATUS_CHANGED: ("sales_order", "order_id", "/sales-orders/{id}"),
    DELIVERY_ASSIGNED: ("delivery", "delivery_id", "/deliveries/{id}"),
    DELIVERY_REJECTED: ("delivery", "delivery_id", "/deliveries/{id}"),
    DELIVERY_STATUS_CHANGED: ("delivery", "delivery_id", "/deliveries/{id}"),
    EXPENSE_CREATED: ("expense", "expense_id", "/expenses/{id}"),
    EXPENSE_CLARIFICATION_REQUESTED: ("expense", "expense_id", "/expenses/{id}"),
    EXPENSE_STATUS_CHANGED: ("expense", "expense_id", "/expenses/{id}"),
    EXPENSE_READY_FOR_REIMBURSEMENT: ("expense", "expense_id", "/expenses/{id}"),
    LEAVE_STATUS_CHANGED: ("leave", "leave_id", "/leaves/{id}"),
    LEAD_ASSIGNED: ("lead", "lead_id", "/leads/{id}"),
    FOLLOW_UP_DUE: ("follow_up", "follow_up_id", "/follow-ups/{id}"),
    FOLLOW_UP_OVERDUE: ("follow_up", "follow_up_id", "/follow-ups/{id}"),
    QUOTATION_STATUS_CHANGED: ("quotation", "quotation_id", "/quotations/{id}"),
    DAY_END_SUBMITTED: ("day_end", "day_end_id", "/delivery-day-ends/{id}"),
    DAY_END_STATUS_CHANGED: ("day_end", "day_end_id", "/delivery-day-ends/{id}"),
    STOCK_ALERT: ("product", "product_id", "/warehouses/stock?warehouse_id={warehouse_id}&product_id={id}"),
    ORGANIZATION_CREATED: ("organization", "target_organization_id", "/superadmin/organizations/{id}"),
}


def describe(data: dict | None) -> dict[str, str | None]:
    """{entity_type, entity_id, action_url} for a notification's stored data (all None
    when the event has no subject, e.g. push_test)."""
    data = data or {}
    event = data.get("event")
    out: dict[str, str | None] = {"entity_type": None, "entity_id": None, "action_url": None}
    if event == PAYMENT_RECEIVED:
        # The invoice when the payment settled one, otherwise the receipt itself.
        if data.get("invoice_id"):
            return {"entity_type": "invoice", "entity_id": data["invoice_id"],
                    "action_url": f"/invoices/{data['invoice_id']}"}
        if data.get("payment_id"):
            return {"entity_type": "payment_receipt", "entity_id": data["payment_id"],
                    "action_url": f"/payment-receipts/{data['payment_id']}"}
        return out
    if event == UPGRADE_REQUESTED:
        # A platform-staff request has its own record; an org admin's request lives on
        # the organization (approved from /superadmin/organizations/{id}/approve-upgrade).
        if data.get("upgrade_request_id"):
            return {"entity_type": "upgrade_request", "entity_id": data["upgrade_request_id"],
                    "action_url": f"/platform/upgrade-requests/{data['upgrade_request_id']}"}
        if data.get("target_organization_id"):
            return {"entity_type": "organization", "entity_id": data["target_organization_id"],
                    "action_url": f"/superadmin/organizations/{data['target_organization_id']}"}
        return out
    if event == DAY_END_STATUS_CHANGED and data.get("status") == "rejected":
        # The partner goes back to Close My Day to correct and resubmit.
        return {"entity_type": "day_end", "entity_id": data.get("day_end_id"),
                "action_url": "/delivery-day-ends/current"}
    spec = _ENTITY.get(event or "")
    if spec is None or not data.get(spec[1]):
        return out
    entity_type, key, template = spec
    url = template.replace("{id}", data[key]).replace("{warehouse_id}", data.get("warehouse_id", ""))
    return {"entity_type": entity_type, "entity_id": data[key], "action_url": url}


def build_data(event: str, **ids) -> dict[str, str]:
    """The `data` dict for an event: {"event": ..., <allowed id keys as strings>}. Unknown
    keys are rejected so nothing beyond the documented identifiers can slip in."""
    if event not in ALL_EVENTS:
        raise ValueError(f"unknown notification event '{event}'")
    allowed = set(EVENT_DATA_KEYS.get(event, ())) | set(COMMON_DATA_KEYS)
    unknown = set(ids) - allowed
    if unknown:
        raise ValueError(f"data keys {sorted(unknown)} are not part of the '{event}' contract")
    data = {"event": event}
    data.update({key: str(value) for key, value in ids.items() if value is not None and value != ""})
    return data


# Which notifications are also pushed to phones/browsers. Every notification is stored
# in-app (and sent over the websocket); only the ones someone should act on now are
# pushed, so routine updates don't train people to ignore the phone.
#   event -> None (always pushed) or the set of `data["status"]` values that are pushed.
_PUSHED: dict[str, frozenset[str] | None] = {
    PUSH_TEST: None,
    ORDER_CREATED: None,
    ORDER_CONFIRMED: None,
    DELIVERY_ASSIGNED: None,
    DELIVERY_REJECTED: None,
    DELIVERY_STATUS_CHANGED: frozenset({"failed", "cancelled"}),
    LEAD_ASSIGNED: None,
    FOLLOW_UP_DUE: None,
    QUOTATION_STATUS_CHANGED: None,
    EXPENSE_STATUS_CHANGED: frozenset({"approved", "rejected"}),
    EXPENSE_CLARIFICATION_REQUESTED: None,
    EXPENSE_READY_FOR_REIMBURSEMENT: None,
    LEAVE_STATUS_CHANGED: None,
    DAY_END_STATUS_CHANGED: frozenset({"rejected"}),
    STOCK_ALERT: frozenset({"out_of_stock"}),
    ORGANIZATION_CREATED: None,
    UPGRADE_REQUESTED: None,
}


def should_push(event: str | None, data: dict | None = None) -> bool:
    """Whether a notification for `event` (with its stored `data`) is also pushed. In-app
    only: order_status_changed, delivery delivered / partially delivered, Day-End approved
    or submitted without variance, follow_up_overdue, low stock, payment_received,
    expense_created and expense reimbursed -- and anything without a known event."""
    data = data or {}
    if event == DAY_END_SUBMITTED:
        return data.get("has_variance") == "true"
    if event not in _PUSHED:
        return False
    statuses = _PUSHED[event]
    return statuses is None or data.get("status") in statuses
