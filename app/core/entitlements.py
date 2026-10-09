"""Canonical Plan Entitlements & Limits Registry.

Single source of truth for all entitlement keys, limit keys, default plan configurations,
and report type mappings across the CRM SaaS platform.
"""

from typing import Any

# ---------------------------------------------------------------------------
# Canonical Entitlement Keys & Categories
# ---------------------------------------------------------------------------

ENTITLEMENT_CATEGORIES: dict[str, str] = {
    "crm": "Customer Relationship Management",
    "erp": "ERP & Inventory Catalog",
    "finance": "Finance & Accounts",
    "invoice": "Invoicing",
    "sales": "Sales & Fulfillment",
    "employee": "Employees & Access Control",
    "admin": "Administration",
    "settings": "Settings & Customization",
    "notifications": "Notifications",
    "report": "Reports & Analytics",
}

CANONICAL_ENTITLEMENTS: dict[str, dict[str, str]] = {
    # CRM
    "crm.customers": {
        "category": "crm",
        "name": "Customers",
        "description": "Customer list, detail, contacts, and account profiles",
    },
    "crm.leads": {
        "category": "crm",
        "name": "Leads",
        "description": "Lead tracking, pipeline, and conversion to customer",
    },
    "crm.quotations": {
        "category": "crm",
        "name": "Quotations",
        "description": "Price estimations and sales quotes",
    },
    # ERP
    "erp.brands": {
        "category": "erp",
        "name": "Brands",
        "description": "Brand catalog management",
    },
    "erp.categories": {
        "category": "erp",
        "name": "Categories",
        "description": "Product category hierarchy",
    },
    "erp.products": {
        "category": "erp",
        "name": "Products",
        "description": "Product master, variants, and pricing tiers",
    },
    "erp.suppliers": {
        "category": "erp",
        "name": "Suppliers",
        "description": "Vendor profiles and contacts",
    },
    "erp.inventory": {
        "category": "erp",
        "name": "Inventory",
        "description": "Stock tracking, adjustments, and warehouse transfers",
    },
    "erp.warehouses": {
        "category": "erp",
        "name": "Warehouses",
        "description": "Multi-warehouse location management",
    },
    "erp.purchases": {
        "category": "erp",
        "name": "Purchases",
        "description": "Purchase orders and Goods Receipt Notes (GRN)",
    },
    "erp.purchase_returns": {
        "category": "erp",
        "name": "Purchase Returns",
        "description": "Debit notes and vendor returns",
    },
    # Finance
    "finance.receivables": {
        "category": "finance",
        "name": "Receivables",
        "description": "Customer payment receipts and accounts receivable ledgers",
    },
    "finance.accounts_payable": {
        "category": "finance",
        "name": "Accounts Payable",
        "description": "Supplier payables and aging analysis",
    },
    "finance.supplier_payments": {
        "category": "finance",
        "name": "Supplier Payments",
        "description": "Outgoing vendor payments and bill allocations",
    },
    "finance.collection_reconciliation": {
        "category": "finance",
        "name": "Collection Reconciliation",
        "description": "Field delivery collection audit and accounting reconciliation",
    },
    "finance.expenses": {
        "category": "finance",
        "name": "Expenses",
        "description": "Direct operational and business expense vouchers",
    },
    "finance.reports": {
        "category": "finance",
        "name": "Financial Reports",
        "description": "Access to financial and operational reporting suite",
    },
    # Invoices
    "invoice.sales_invoices": {
        "category": "invoice",
        "name": "Sales Invoices",
        "description": "Tax invoice generation, issue, and payment links",
    },
    "invoice.supplier_invoices": {
        "category": "invoice",
        "name": "Supplier Invoices",
        "description": "Vendor bill recording and approval",
    },
    # Sales
    "sales.orders": {
        "category": "sales",
        "name": "Sales Orders",
        "description": "Order placement, approval, and lifecycle tracking",
    },
    "sales.sales_returns": {
        "category": "sales",
        "name": "Sales Returns",
        "description": "Customer return notes and credit note management",
    },
    "sales.deliveries": {
        "category": "sales",
        "name": "Deliveries",
        "description": "Delivery planning, dispatch, proof-of-delivery, and route tracking",
    },
    "sales.sales": {
        "category": "sales",
        "name": "Sales Overview",
        "description": "Operational sales overview and transaction monitoring",
    },
    "sales.vehicles": {
        "category": "sales",
        "name": "Vehicles",
        "description": "Fleet vehicle registration and driver assignments",
    },
    "sales.vehicle_stock": {
        "category": "sales",
        "name": "Vehicle Stock",
        "description": "Van sales loading, stock audit, and end-of-day returns",
    },
    # Employees & HR
    "employee.staff": {
        "category": "employee",
        "name": "Staff Management",
        "description": "Employee directory, profiles, and user accounts",
    },
    "employee.attendance": {
        "category": "employee",
        "name": "Attendance",
        "description": "Staff clock-in/out and daily attendance logs",
    },
    "employee.leaves": {
        "category": "employee",
        "name": "Leaves",
        "description": "Leave requests, quotas, and approvals",
    },
    "employee.roles_permissions": {
        "category": "employee",
        "name": "Roles & Permissions",
        "description": "Custom role creation and granular permission matrix",
    },
    # Admin
    "admin.company_settings": {
        "category": "admin",
        "name": "Company Settings",
        "description": "Company profile, tax, and master business configuration",
    },
    "admin.plans": {
        "category": "admin",
        "name": "Plans & Subscription",
        "description": "Plan catalogue viewing and upgrade requests",
    },
    "admin.billing_history": {
        "category": "admin",
        "name": "Billing History",
        "description": "Subscription invoices and payment history",
    },
    "admin.audit_log": {
        "category": "admin",
        "name": "Audit Log",
        "description": "Organization change audit and recent activity logs",
    },
    "admin.profile": {
        "category": "admin",
        "name": "User Profile",
        "description": "Personal account security and preferences",
    },
    # Settings
    "settings.object_fields": {
        "category": "settings",
        "name": "Custom Fields",
        "description": "Custom schema fields per business object",
    },
    "settings.invoice_changes": {
        "category": "settings",
        "name": "Invoice Settings",
        "description": "Invoice numbering series and print template customization",
    },
    "settings.appearance": {
        "category": "settings",
        "name": "Theme & Appearance",
        "description": "Branding, colors, and layout customization",
    },
    "settings.legal_help": {
        "category": "settings",
        "name": "Legal & Help",
        "description": "Terms, privacy policies, and platform help center",
    },
    # Notifications
    "notifications.whatsapp_delivery": {
        "category": "notifications",
        "name": "WhatsApp Delivery Notifications",
        "description": "Automated WhatsApp delivery updates to customers",
    },
    # Reports (17 canonical types)
    "report.daily_transaction": {
        "category": "report",
        "name": "Daily Transaction Report",
        "description": "Daily ledger of transactions and cash movements",
    },
    "report.sales": {
        "category": "report",
        "name": "Sales Report",
        "description": "Sales invoice analysis by customer, product, or salesperson",
    },
    "report.purchase": {
        "category": "report",
        "name": "Purchase Report",
        "description": "Financially recognized purchases and bill items",
    },
    "report.customer_outstanding": {
        "category": "report",
        "name": "Customer Outstanding Report",
        "description": "Accounts receivable aging and open customer balances",
    },
    "report.supplier_outstanding": {
        "category": "report",
        "name": "Supplier Outstanding Report",
        "description": "Accounts payable aging and unpaid vendor balances",
    },
    "report.payment_collection": {
        "category": "report",
        "name": "Payment Collection Report",
        "description": "Customer payments received across payment modes",
    },
    "report.supplier_payment": {
        "category": "report",
        "name": "Supplier Payment Report",
        "description": "Vendor payments disbursed and bill settlement",
    },
    "report.expense": {
        "category": "report",
        "name": "Expense Report",
        "description": "Direct and operational expense breakdown",
    },
    "report.cash_collection": {
        "category": "report",
        "name": "Cash Collection Report",
        "description": "Cash receipts and counter collection movements",
    },
    "report.sales_return": {
        "category": "report",
        "name": "Sales Return Report",
        "description": "Customer returns and credit note history",
    },
    "report.purchase_return": {
        "category": "report",
        "name": "Purchase Return Report",
        "description": "Supplier returns and debit note history",
    },
    "report.inventory_summary": {
        "category": "report",
        "name": "Inventory Summary Report",
        "description": "Warehouse stock levels, valuation, and reorder status",
    },
    "report.stock_movement": {
        "category": "report",
        "name": "Stock Movement Report",
        "description": "Detailed ledger of goods receipts, dispatches, and adjustments",
    },
    "report.profit_and_loss": {
        "category": "report",
        "name": "Profit & Loss Sheet",
        "description": "Trading revenue, COGS, gross margin, and net profit report",
    },
    "report.cash_flow": {
        "category": "report",
        "name": "Cash Flow Sheet",
        "description": "Recorded cash-in vs cash-out operational cash flow",
    },
    "report.gst_filing": {
        "category": "report",
        "name": "GST Filing Sheet",
        "description": "Outward and inward GST tax summary for tax filing",
    },
    "report.sales_overview": {
        "category": "report",
        "name": "Sales Overview",
        "description": "Order-to-delivery fulfillment overview",
    },
    "report.balance_sheet": {
        "category": "report",
        "name": "Balance Sheet",
        "description": "Formal balance sheet statement (unimplemented without double-entry GL)",
    },
}

ALL_ENTITLEMENT_KEYS: set[str] = set(CANONICAL_ENTITLEMENTS.keys())

# ---------------------------------------------------------------------------
# Canonical Limit Keys
# ---------------------------------------------------------------------------

CANONICAL_LIMITS: dict[str, dict[str, Any]] = {
    "max_users": {
        "name": "Maximum Users",
        "description": "Total active user accounts allowed for the organization (Admin + staff)",
        "type": "integer",
    },
    "max_orders": {
        "name": "Maximum Monthly Orders",
        "description": "Maximum sales orders that can be placed per calendar month (None = unlimited)",
        "type": "integer",
    },
    "max_storage_gb": {
        "name": "Maximum Storage (GB)",
        "description": "Total file upload storage limit in gigabytes (None = unlimited)",
        "type": "float",
    },
    "max_warehouses": {
        "name": "Maximum Warehouses",
        "description": "Maximum active warehouse locations allowed (None = unlimited)",
        "type": "integer",
    },
}

ALL_LIMIT_KEYS: set[str] = set(CANONICAL_LIMITS.keys())

# ---------------------------------------------------------------------------
# Report Type to Entitlement Mapping
# ---------------------------------------------------------------------------

REPORT_TYPE_TO_ENTITLEMENT: dict[str, str] = {
    "daily-transaction": "report.daily_transaction",
    "sales": "report.sales",
    "purchase": "report.purchase",
    "customer-outstanding": "report.customer_outstanding",
    "supplier-outstanding": "report.supplier_outstanding",
    "payment-collection": "report.payment_collection",
    "supplier-payment": "report.supplier_payment",
    "expense": "report.expense",
    "cash-collection": "report.cash_collection",
    "sales-return": "report.sales_return",
    "purchase-return": "report.purchase_return",
    "inventory-summary": "report.inventory_summary",
    "stock-movement": "report.stock_movement",
    "profit-loss": "report.profit_and_loss",
    "cash-flow-sheet": "report.cash_flow",
    "gst-summary": "report.gst_filing",
    "sales-overview": "report.sales_overview",
    "balance-sheet": "report.balance_sheet",
}

# ---------------------------------------------------------------------------
# Standard Tier Defaults
# ---------------------------------------------------------------------------

_BASE_CORE_ENTITLEMENTS: dict[str, bool] = {
    # CRM Core
    "crm.customers": True,
    "crm.leads": False,
    "crm.quotations": False,
    # ERP Core
    "erp.brands": True,
    "erp.categories": True,
    "erp.products": True,
    "erp.suppliers": True,
    "erp.inventory": True,
    "erp.warehouses": True,
    "erp.purchases": True,
    "erp.purchase_returns": True,
    # Finance Core
    "finance.receivables": True,
    "finance.accounts_payable": True,
    "finance.supplier_payments": True,
    "finance.collection_reconciliation": True,
    "finance.expenses": True,
    "finance.reports": True,
    # Invoices
    "invoice.sales_invoices": True,
    "invoice.supplier_invoices": True,
    # Sales Core
    "sales.orders": True,
    "sales.sales_returns": True,
    "sales.deliveries": True,
    "sales.sales": True,
    "sales.vehicles": False,
    "sales.vehicle_stock": False,
    # Employees (Disabled in Basic)
    "employee.staff": False,
    "employee.attendance": False,
    "employee.leaves": False,
    "employee.roles_permissions": False,
    # Admin Core
    "admin.company_settings": True,
    "admin.plans": True,
    "admin.billing_history": True,
    "admin.audit_log": True,
    "admin.profile": True,
    # Settings
    "settings.object_fields": True,
    "settings.invoice_changes": True,
    "settings.appearance": True,
    "settings.legal_help": True,
    # Notifications
    "notifications.whatsapp_delivery": False,
    # Reports
    "report.daily_transaction": True,
    "report.sales": True,
    "report.purchase": True,
    "report.customer_outstanding": True,
    "report.supplier_outstanding": True,
    "report.payment_collection": True,
    "report.supplier_payment": True,
    "report.expense": True,
    "report.cash_collection": True,
    "report.sales_return": True,
    "report.purchase_return": True,
    "report.inventory_summary": True,
    "report.stock_movement": True,
    "report.sales_overview": True,
    "report.profit_and_loss": False,
    "report.cash_flow": False,
    "report.gst_filing": False,
    "report.balance_sheet": False,
}

_PRO_EXTENSIONS: dict[str, bool] = {
    "crm.leads": True,
    "crm.quotations": True,
    "sales.vehicles": True,
    "sales.vehicle_stock": True,
    "employee.staff": True,
    "employee.attendance": True,
    "employee.leaves": True,
    "employee.roles_permissions": True,
    "notifications.whatsapp_delivery": True,
    "report.profit_and_loss": True,
    "report.cash_flow": True,
    "report.gst_filing": True,
    "report.balance_sheet": False,
}


DEFAULT_FALLBACK_ENTITLEMENTS: dict[str, bool] = _BASE_CORE_ENTITLEMENTS

DEFAULT_FALLBACK_LIMITS: dict[str, Any] = {
    "max_users": 1,
    "max_warehouses": 1,
    "max_orders": 50,
    "max_storage_gb": None,
}


def default_entitlements_for_plan(plan_name: str | None) -> dict[str, bool]:
    """Resolve full default entitlement map for a plan tier name."""
    name = (plan_name or "").strip().lower()

    # Base dictionary for Free / Basic
    entitlements = dict(_BASE_CORE_ENTITLEMENTS)

    if name in ("pro", "enterprise"):
        entitlements.update(_PRO_EXTENSIONS)

    return entitlements


def default_limits_for_plan(plan_name: str | None) -> dict[str, Any]:
    """Resolve default limits for standard plans."""
    name = (plan_name or "").strip().lower()

    if name == "basic":
        return {
            "max_users": 1,
            "max_warehouses": 1,
            "max_orders": 1000,
            "max_storage_gb": None,
        }
    if name == "pro":
        return {
            "max_users": 50,
            "max_warehouses": None,
            "max_orders": None,
            "max_storage_gb": None,
        }
    if name == "enterprise":
        return {
            "max_users": None,
            "max_warehouses": None,
            "max_orders": None,
            "max_storage_gb": None,
        }
    # Free / Default
    return {
        "max_users": 1,
        "max_warehouses": 1,
        "max_orders": 50,
        "max_storage_gb": None,
    }


# Map report endpoint machine names to canonical entitlement keys
REPORT_TYPE_TO_ENTITLEMENT: dict[str, str] = {
    "daily-transaction": "report.daily_transaction",
    "sales": "report.sales",
    "purchase": "report.purchase",
    "customer-outstanding": "report.customer_outstanding",
    "supplier-outstanding": "report.supplier_outstanding",
    "payment-collection": "report.payment_collection",
    "expense": "report.expense",
    "cash-collection": "report.cash_collection",
    "gst-summary": "report.gst_filing",
    "sales-return": "report.sales_return",
    "purchase-return": "report.purchase_return",
    "profit-loss": "report.profit_and_loss",
    "supplier-payment": "report.supplier_payment",
    "inventory-summary": "report.inventory_summary",
    "stock-movement": "report.stock_movement",
    "sales-overview": "report.sales_overview",
    "cash-flow-sheet": "report.cash_flow",
    "balance-sheet": "report.balance_sheet",
}

