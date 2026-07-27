"""Permission catalog + default role matrices.

Permissions are stored on each role as a JSON object:
    { "<module>": { "view": bool, "create": bool, ... }, ... }

Only granted modules are stored (deny-by-default): a module absent from the
object means no access to it. Adding a new module later needs no data migration —
existing roles simply don't have it (= no access) until an Admin grants it.
"""

# Business modules a permission can apply to. Add to this list as modules ship.
MODULES: list[str] = [
    "dashboard",
    "products",
    "inventory",
    "vehicle_stock",
    "customers",
    "suppliers",
    "sales_orders",
    "purchases",
    "deliveries",
    "invoices",
    "payments",
    "expenses",
    "attendance",
    "reports",
    "gst",
    "users",
    "settings",
]

# Actions available per module.
ACTIONS: list[str] = ["view", "create", "edit", "delete", "approve", "export", "download"]

# Human-friendly labels for the frontend's permission-matrix UI.
MODULE_LABELS: dict[str, str] = {
    "dashboard": "Dashboard",
    "products": "Products",
    "inventory": "Inventory",
    "vehicle_stock": "Vehicle Stock",
    "customers": "Customers",
    "suppliers": "Suppliers",
    "sales_orders": "Sales Orders",
    "purchases": "Purchases",
    "deliveries": "Deliveries",
    "invoices": "Invoices",
    "payments": "Payments",
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
def default_role_matrices() -> dict[str, dict[str, dict[str, bool]]]:
    return {
        "Sales Officer": {
            "dashboard": _view_only(),
            "customers": _full(),
            "sales_orders": _full(),
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
            "customers": _view_only(),
            "suppliers": _view_only(),
            "inventory": _view_only(),
        },
    }


def normalize_permissions(permissions: dict | None) -> dict[str, dict[str, bool]]:
    """Clean incoming permissions: keep only known modules, coerce all 7 actions
    to booleans, and drop modules with no granted action (deny-by-default)."""
    result: dict[str, dict[str, bool]] = {}
    for module, actions in (permissions or {}).items():
        if module not in MODULES or not isinstance(actions, dict):
            continue
        row = {action: bool(actions.get(action, False)) for action in ACTIONS}
        if any(row.values()):  # skip all-false modules
            result[module] = row
    return result


def catalog() -> dict:
    """Full module/action catalog for the frontend to render the matrix UI."""
    return {
        "modules": [{"key": m, "label": MODULE_LABELS[m]} for m in MODULES],
        "actions": [{"key": a, "label": ACTION_LABELS[a]} for a in ACTIONS],
    }
