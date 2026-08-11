"""Permission catalog + default role matrices.

Permissions are stored on each role as a JSON object:
    { "<module>": { "view": bool, "create": bool, ... }, ... }

Only granted modules are stored (deny-by-default): a module absent from the
object means no access to it. Adding a new module later needs no data migration —
existing roles simply don't have it (= no access) until an Admin grants it.
"""

# Business modules a permission can apply to. Every module a router gates on must
# be in here — a module missing from this list is silently dropped by
# normalize_permissions, which would look like "the permission I granted vanished".
MODULES: list[str] = [
    "dashboard",
    "products",
    "inventory",
    "vehicle_stock",
    "customers",
    "leads",
    "quotations",
    "suppliers",
    "sales_orders",
    "sales_returns",
    "purchases",
    "deliveries",
    "invoices",
    "payments",
    "payment_receipts",
    "expenses",
    "attendance",
    "reports",
    "gst",
    "users",
    "settings",
]

# Friendlier names callers may send for a module, mapped to its canonical key. The
# Roles screen can post "orders" and still be granting sales_orders.
MODULE_ALIASES: dict[str, str] = {
    "orders": "sales_orders",
    "sales": "sales_orders",
    "sales_order": "sales_orders",
    "customer": "customers",
    "product": "products",
    "lead": "leads",
    "quotation": "quotations",
    "supplier": "suppliers",
    "invoice": "invoices",
    "payment": "payments",
    "receipts": "payment_receipts",
    "expense": "expenses",
    "delivery": "deliveries",
    "purchase": "purchases",
    "returns": "sales_returns",
    "stock": "inventory",
    "staff": "users",
    "roles": "users",
}

# Actions available per module.
ACTIONS: list[str] = ["view", "create", "edit", "delete", "approve", "export", "download"]

# Human-friendly labels for the frontend's permission-matrix UI.
MODULE_LABELS: dict[str, str] = {
    "dashboard": "Dashboard",
    "products": "Products",
    "inventory": "Inventory",
    "vehicle_stock": "Vehicle Stock",
    "customers": "Customers",
    "leads": "Leads",
    "quotations": "Quotations",
    "suppliers": "Suppliers",
    "sales_orders": "Sales Orders",
    "sales_returns": "Sales Returns",
    "purchases": "Purchases",
    "deliveries": "Deliveries",
    "invoices": "Invoices",
    "payments": "Payments",
    "payment_receipts": "Payment Receipts",
    "expenses": "Expenses",
    "attendance": "Attendance",
    "reports": "Reports",
    "gst": "GST",
    "users": "Users & Roles",
    "settings": "Settings",
}
ACTION_LABELS: dict[str, str] = {
    "view": "View",
    "create": "Create",
    "edit": "Edit",
    "delete": "Delete",
    "approve": "Approve",
    "export": "Export",
    "download": "Download",
}


def _perm(
    view: bool = False,
    create: bool = False,
    edit: bool = False,
    delete: bool = False,
    approve: bool = False,
    export: bool = False,
    download: bool = False,
) -> dict[str, bool]:
    return {
        "view": view,
        "create": create,
        "edit": edit,
        "delete": delete,
        "approve": approve,
        "export": export,
        "download": download,
    }


def _full() -> dict[str, bool]:
    return _perm(True, True, True, True, True, True, True)


def _view_only() -> dict[str, bool]:
    return _perm(view=True)


def _create_only() -> dict[str, bool]:
    # Field staff: can see and add, but not edit/delete existing records.
    return _perm(view=True, create=True)


# Starting permission matrices for the 3 auto-seeded default roles.
# (Approximate per BRD — the PM can fine-tune later via the Roles UI.)
# Workspace + data scope for the auto-seeded roles: which dashboard the frontend
# opens, and whether the role sees the whole firm's records or only its own.
DEFAULT_ROLE_SETTINGS: dict[str, dict[str, str]] = {
    "Sales Officer": {"workspace": "sales", "data_scope": "own"},
    "Delivery Partner": {"workspace": "delivery", "data_scope": "own"},
    "Accountant": {"workspace": "accounts", "data_scope": "all"},
}

DATA_SCOPES = ("all", "own")


def default_role_matrices() -> dict[str, dict[str, dict[str, bool]]]:
    return {
        "Sales Officer": {
            "dashboard": _view_only(),
            "customers": _perm(view=True, create=True, edit=True),
            "leads": _full(),
            "quotations": _full(),
            "sales_orders": _perm(view=True, create=True, edit=True),
            "attendance": _full(),
            "products": _view_only(),
            "inventory": _view_only(),
        },
        "Delivery Partner": {
            "dashboard": _view_only(),
            "deliveries": _full(),
            "vehicle_stock": _full(),
            "attendance": _full(),
            "customers": _create_only(),
            "sales_orders": _create_only(),
            "products": _view_only(),
        },
        "Accountant": {
            "dashboard": _view_only(),
            "invoices": _full(),
            "payments": _full(),
            "expenses": _full(),
            "gst": _full(),
            "reports": _full(),
            "payment_receipts": _full(),
            "purchases": _full(),
            "customers": _view_only(),
            "suppliers": _view_only(),
            "inventory": _view_only(),
        },
    }


def normalize_permissions(permissions: dict | None) -> dict[str, dict[str, bool]]:
    """Clean incoming permissions: resolve aliases to canonical module keys, coerce
    all 7 actions to booleans, and drop modules with no granted action
    (deny-by-default). Unknown modules are ignored."""
    result: dict[str, dict[str, bool]] = {}
    for module, actions in (permissions or {}).items():
        key = MODULE_ALIASES.get(module, module)
        if key not in MODULES or not isinstance(actions, dict):
            continue
        row = {action: bool(actions.get(action, False)) for action in ACTIONS}
        if any(row.values()):  # skip all-false modules
            # An alias and its canonical key in the same body merge rather than
            # one overwriting the other.
            merged = result.get(key, {a: False for a in ACTIONS})
            result[key] = {a: merged[a] or row[a] for a in ACTIONS}
    return result


def full_access_matrix() -> dict[str, dict[str, bool]]:
    """Every module, every action — what an Admin effectively has. Returned by
    /auth/me so the frontend can read `permissions` the same way for any user."""
    return {module: _full() for module in MODULES}


def catalog() -> dict:
    """Full module/action catalog for the frontend to render the matrix UI."""
    return {
        "modules": [{"key": m, "label": MODULE_LABELS[m]} for m in MODULES],
        "actions": [{"key": a, "label": ACTION_LABELS[a]} for a in ACTIONS],
    }
