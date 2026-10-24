"""Which form fields each module has, and which ones a firm has chosen to hide.

The catalog lists the user-entered inputs of each module's create/update API, by their API
names (nested ones as `section.field`), each with the label forms show for it. Visibility
is a presentation preference only: the module APIs keep validating exactly as before, and
hiding a field never touches stored data.

  mandatory         required by the API; always shown, cannot be hidden
  conditional       required only in some situations (see `condition`); always shown
  non_configurable  a real input that a workflow owns (product variants); always shown
  optional          may be hidden; shown unless the firm saved `false` for it

System-managed values (generated numbers, workflow and approval statuses, calculated stock or
totals, read-only display values, aliases) are not form fields and are left out.

A field in DEPENDS_ON is only shown while the field it depends on is: hiding a product's
Category hides its Subcategory too, without touching the Subcategory preference itself.

Preferences live in Organization.field_settings as {module: {field: bool}}, holding only what
a firm explicitly saved. Older saves used other module names (`sales`, `expenses`) and some
other field names; those are read through LEGACY_MODULES / LEGACY_FIELDS, with a canonical
key always winning over a legacy one.
"""

from __future__ import annotations

MANDATORY, CONDITIONAL, NON_CONFIGURABLE, OPTIONAL = "mandatory", "conditional", "non_configurable", "optional"
LOCKED = (MANDATORY, CONDITIONAL, NON_CONFIGURABLE)

# module -> {mandatory: {key: label}, conditional: {key: (label, condition)},
#            non_configurable: {key: label}, optional: {key: label}}
CATALOG: dict[str, dict] = {
    # PUT /organizations/settings (CompanySettingsUpdate). `name` is required at registration
    # and cannot be blanked (min_length=1); nothing else is required -- the completeness
    # checklist (REQUIRED_COMPANY_FIELDS) is reported, never enforced. Left out: company_status
    # (active toggle), tax_configuration / invoice_settings / number_prefixes /
    # employee_id_prefix (configuration, not form inputs), doc_other_files (own endpoint), and
    # the legacy mirrors `address` (read only as registered_address's fallback) and
    # `branch_address` (set from branch_addresses on every save).
    "company": {
        MANDATORY: {"name": "Company Name"},
        CONDITIONAL: {},
        NON_CONFIGURABLE: {},
        OPTIONAL: {
            "legal_name": "Legal Name",
            "business_type": "Business Type",
            "industry": "Industry",
            "date_of_incorporation": "Date of Incorporation",
            "cin_number": "CIN Number",
            "gst_number": "GST Number",
            "pan_number": "PAN Number",
            "gstin_pan": "GSTIN / PAN",
            "description": "Business Description",
            "financial_year": "Financial Year",
            "phone": "Phone",
            "primary_mobile": "Primary Mobile",
            "alternate_mobile": "Alternate Mobile",
            "landline": "Landline",
            "email": "Email",
            "website": "Website",
            "customer_support_number": "Customer Support Number",
            "whatsapp_number": "WhatsApp Number",
            "registered_address": "Registered Address",
            "branch_addresses": "Branch Addresses",
            "city": "City",
            "state": "State",
            "country": "Country",
            "pin_code": "PIN Code",
            "maps_latitude": "Map Latitude",
            "maps_longitude": "Map Longitude",
            "logo_url": "Company Logo",
            "signature_url": "Authorized Signature",
            "stamp_url": "Company Stamp",
            "letterhead_url": "Letterhead",
            "banner_url": "Banner",
            "payment_qr_url": "Payment QR Code",
            "upi_id": "UPI ID",
            "bank_name": "Bank Name",
            "bank_account_details": "Bank Account Details",
            "bank_account_holder": "Account Holder Name",
            "bank_ifsc": "IFSC Code",
            "facebook_url": "Facebook",
            "instagram_url": "Instagram",
            "linkedin_url": "LinkedIn",
            "twitter_url": "Twitter / X",
            "youtube_url": "YouTube",
            "currency": "Currency",
            "timezone": "Time Zone",
            "language": "Language",
            "doc_gst_url": "GST Certificate",
            "doc_pan_url": "PAN Card",
            "doc_coi_url": "Certificate of Incorporation",
            "doc_trade_license_url": "Trade License",
            "doc_msme_url": "MSME Certificate",
            "doc_fssai_url": "FSSAI License",
            "doc_other_url": "Other Business Document",
            "auth_person_name": "Authorized Person Name",
            "auth_person_designation": "Authorized Person Designation",
            "auth_person_mobile": "Authorized Person Mobile",
            "auth_person_email": "Authorized Person Email",
            "auth_person_photo_url": "Authorized Person Photo",
            "auth_person_signature_url": "Authorized Person Signature",
            "employee_count": "Number of Employees",
            "business_hours": "Business Hours",
            "mission_vision": "Mission & Vision",
            "notes": "Notes",
        },
    },
    # LeadCreate / LeadUpdate. Left out: lead_id (generated), lead_status (always `new` on
    # create, then workflow), converted_customer_id / converted_at (conversion), and the
    # aliases mobile, source, status, assigned_sales_officer_id.
    "lead": {
        MANDATORY: {
            "name": "Lead / Business Name",
            "mobile_number": "Mobile Number",
            "lead_source": "Lead Source",
        },
        CONDITIONAL: {},
        NON_CONFIGURABLE: {},
        OPTIONAL: {
            "contact_person": "Contact Person",
            "email": "Email",
            "interested_product": "Interested Product (Free Text)",
            "interested_product_ids": "Interested Products",
            "lead_type": "Lead Type",
            "segment": "Segment",
            "customer_id": "Linked Customer",
            "assigned_salesperson_id": "Assigned Salesperson",
            "notes": "Notes",
        },
    },
    # CustomerProfileIn (nested sections). customer_name is enforced by POST /customers.
    # Left out: basic_information.status (account lifecycle) and
    # sales_crm_information.sales_representative (read-only, resolved from the id).
    "customer": {
        MANDATORY: {"basic_information.customer_name": "Customer Name"},
        CONDITIONAL: {},
        NON_CONFIGURABLE: {},
        OPTIONAL: {
            "basic_information.customer_type": "Customer Type",
            "basic_information.legal_business_name": "Legal Business Name",
            "basic_information.display_name": "Display Name",
            "basic_information.industry": "Industry",
            "basic_information.customer_category": "Customer Category",
            "basic_information.customer_since": "Customer Since",
            "basic_information.profile_image_id": "Profile Image",
            "contact_information.primary_contact_person": "Primary Contact Person",
            "contact_information.designation": "Designation",
            "contact_information.mobile_number": "Mobile Number",
            "contact_information.alternate_mobile_number": "Alternate Mobile Number",
            "contact_information.email_address": "Email Address",
            "contact_information.website": "Website",
            "address_information.billing_address": "Billing Address",
            "address_information.shipping_address": "Shipping Address",
            "address_information.city": "City",
            "address_information.state": "State",
            "address_information.country": "Country",
            "address_information.pin_zip_code": "PIN / ZIP Code",
            "address_information.google_maps_location": "Map Location",
            "business_tax_information.gstin_tax_id": "GSTIN / Tax ID",
            "business_tax_information.pan_business_registration_no": "PAN / Business Registration No.",
            "business_tax_information.tax_exempt": "Tax Exempt",
            "business_tax_information.tax_category": "Tax Category",
            "business_tax_information.currency": "Currency",
            "payment_information.payment_terms": "Payment Terms",
            "payment_information.credit_limit": "Credit Limit",
            "payment_information.opening_balance": "Opening Balance",
            "payment_information.preferred_payment_method": "Preferred Payment Method",
            "payment_information.upi_id": "UPI ID",
            "payment_information.bank_name": "Bank Name",
            "payment_information.account_number": "Bank Account Number",
            "payment_information.ifsc_swift_code": "IFSC / SWIFT Code",
            "sales_crm_information.sales_representative_id": "Sales Representative",
            "sales_crm_information.lead_source": "Lead Source",
            "sales_crm_information.territory": "Territory",
            "sales_crm_information.customer_priority": "Customer Priority",
            "sales_crm_information.preferred_communication": "Preferred Communication",
            "sales_crm_information.customer_tags": "Customer Tags",
            "social_media_online_presence.facebook_url": "Facebook",
            "social_media_online_presence.instagram_url": "Instagram",
            "social_media_online_presence.linkedin_url": "LinkedIn",
            "social_media_online_presence.x_twitter_url": "X / Twitter",
            "social_media_online_presence.youtube_url": "YouTube",
            "additional_information.date_of_birth": "Date of Birth",
            "additional_information.anniversary_date": "Anniversary Date",
            "additional_information.referral_customer_id": "Referred By (Customer)",
            "additional_information.loyalty_number": "Loyalty Number",
            "additional_information.notes": "Notes",
            "preferences.portal_access_enabled": "Customer Portal Access",
            "preferences.favorite_product_ids": "Favourite Products",
            "preferences.default_price_list_id": "Default Price List",
            "preferences.default_tax_profile_id": "Default Tax Profile",
            "preferences.default_warehouse_id": "Default Warehouse",
            "preferences.preferred_invoice_delivery": "Preferred Invoice Delivery",
            "documents.gst_certificate_id": "GST Certificate",
            "documents.pan_card_id": "PAN Card",
            "documents.business_registration_certificate_id": "Business Registration Certificate",
            "documents.address_proof_id": "Address Proof",
            "documents.purchase_agreement_id": "Purchase Agreement",
            "documents.other_document_ids": "Other Documents",
        },
    },
    # QuotationCreate / QuotationUpdate. Left out: quotation_number (generated), status
    # (always `draft` on create, then workflow).
    "quotation": {
        MANDATORY: {"items": "Line Items"},
        CONDITIONAL: {
            "customer_id": ("Customer", "Exactly one of customer_id or lead_id is required"),
            "lead_id": ("Lead", "Exactly one of customer_id or lead_id is required"),
        },
        NON_CONFIGURABLE: {},
        OPTIONAL: {
            "quotation_date": "Quotation Date",
            "valid_until": "Valid Until",
            "billing_address": "Billing Address",
            "shipping_address": "Shipping Address",
            "salesperson_id": "Salesperson",
            "currency": "Currency",
            "payment_terms": "Payment Terms",
            "delivery_terms": "Delivery Terms",
            "notes": "Notes",
            "terms_conditions": "Terms & Conditions",
        },
    },
    # ProductCreate / ProductUpdate; prices are only accepted under `pricing`. Left out:
    # product_id (generated), status / is_active (lifecycle), total_inventory (stock), and
    # the legacy free-text columns `sub_category`, `brand` and `product_type` (the model keeps
    # them only for backward compatibility; sub_category_id / brand_id / product_type_id are
    # the canonical inputs). Variants belong to the product/variant workflow, so they cannot
    # be hidden.
    "product": {
        MANDATORY: {"name": "Product Name"},
        CONDITIONAL: {},
        NON_CONFIGURABLE: {
            "has_variants": "Has Variants",
            "variant_attributes": "Variant Attributes",
            "variations": "Variants",
        },
        OPTIONAL: {
            "short_name": "Short Name",
            "description": "Description",
            "sku": "SKU",
            "barcode": "Barcode",
            "product_type_id": "Product Type",
            "category_id": "Category",
            "sub_category_id": "Subcategory",
            "brand_id": "Brand",
            "vendor": "Vendor",
            "manufacturer": "Manufacturer",
            "model_number": "Model Number",
            "pricing.purchase_price": "Purchase Price",
            "pricing.selling_price": "Selling Price",
            "pricing.mrp": "MRP",
            "pricing.wholesale_price": "Wholesale Price",
            "pricing.dealer_price": "Dealer Price",
            "pricing.discount_percent": "Discount (%)",
            "pricing.tax_inclusive": "Price Includes Tax",
            "pricing.currency": "Price Currency",
            "hsn_code": "HSN Code",
            "tax_rate": "Tax Rate (%)",
            "tax_category": "Tax Category",
            "cover_image": "Cover Image",
            "images": "Product Images",
            "product_video": "Product Video",
            "product_catalog_brochure": "Catalog / Brochure",
            "product_manual": "Product Manual",
            "product_datasheet": "Datasheet",
            "compliance_certificate": "Compliance Certificate",
            "warranty_document": "Warranty Document",
            "inventory_tracking": "Inventory Tracking",
            "uom": "Unit of Measure",
            "opening_stock": "Opening Stock",
            "minimum_stock_level": "Minimum Stock Level",
            "maximum_stock_level": "Maximum Stock Level",
            "reorder_level": "Reorder Level",
            "reorder_quantity": "Reorder Quantity",
            "bin_shelf_location": "Bin / Shelf Location",
            "batch_tracking": "Batch Tracking",
            "serial_number_tracking": "Serial Number Tracking",
            "expiry_tracking": "Expiry Tracking",
            "preferred_supplier_id": "Preferred Supplier",
            "supplier_product_code": "Supplier Product Code",
            "lead_time": "Lead Time",
            "minimum_order_quantity": "Minimum Order Quantity",
            "purchase_unit": "Purchase Unit",
            "sales_unit": "Sales Unit",
            "commission_eligible": "Commission Eligible",
            "commission": "Commission",
            "default_discount": "Default Discount",
            "weight": "Weight",
            "weight_unit": "Weight Unit",
            "length": "Length",
            "width": "Width",
            "height": "Height",
            "volume": "Volume",
            "color": "Colour",
            "material": "Material",
            "physical_size": "Size",
            "downloadable_product": "Downloadable Product",
            "download_file": "Download File",
            "license_key_required": "License Key Required",
            "download_limit": "Download Limit",
            "warranty_period": "Warranty Period",
            "warranty_period_unit": "Warranty Period Unit",
            "shelf_life": "Shelf Life",
            "shelf_life_unit": "Shelf Life Unit",
            "country_of_origin": "Country of Origin",
            "launch_date": "Launch Date",
            "end_of_life_date": "End-of-Life Date",
            "product_tags": "Tags",
            "notes": "Notes",
        },
    },
    # OrderCreate / OrderUpdate (POST /orders). Left out: order_status and payment_status
    # (workflow).
    "sales_order": {
        MANDATORY: {"customer_id": "Customer", "items": "Line Items"},
        CONDITIONAL: {
            "delivery_address": ("Delivery Address", "Required for home delivery when neither "
                                 "shipping_address nor the customer's saved delivery address is available"),
            "paid_amount": ("Amount Paid", "Required for a partial payment: more than zero and less "
                            "than the order total"),
        },
        NON_CONFIGURABLE: {},
        OPTIONAL: {
            "order_date": "Order Date",
            "delivery_date": "Delivery Date",
            "warehouse_id": "Warehouse",
            "quotation_id": "Source Quotation",
            "salesperson_id": "Salesperson",
            "source": "Order Source",
            "fulfilment_method": "Fulfilment Method",
            "delivery_method": "Delivery Method",
            "billing_address": "Billing Address",
            "shipping_address": "Shipping Address",
            "delivery_partner_id": "Delivery Partner",
            "vehicle_id": "Vehicle",
            "currency": "Currency",
            "discount": "Order Discount",
            "tax": "Order Tax",
            "payment_type": "Payment Type",
            "payment_method": "Payment Method",
            "payment_terms": "Payment Terms",
            "payment_terms_days": "Payment Terms (Days)",
            "delivery_terms": "Delivery Terms",
            "customer_po_number": "Customer PO Number",
            "payment_proof_url": "Payment Proof",
            "delivery_proof_url": "Delivery Proof",
            "notes": "Notes",
        },
    },
    # SupplierCreate / SupplierUpdate. Left out: supplier_code (generated).
    "supplier": {
        MANDATORY: {"name": "Supplier Name"},
        CONDITIONAL: {},
        NON_CONFIGURABLE: {},
        OPTIONAL: {
            "company_name": "Company Name",
            "contact_person": "Contact Person",
            "phone": "Phone",
            "email": "Email",
            "gst_number": "GST Number",
            "pan_number": "PAN Number",
            "category": "Category",
            "supplier_categories": "Supplier Categories",
            "supplier_type": "Supplier Type",
            "payment_terms": "Payment Terms",
            "credit_limit": "Credit Limit",
            "opening_balance": "Opening Balance",
            "address": "Address",
            "city": "City",
            "state": "State",
            "pincode": "PIN Code",
            "country": "Country",
            "notes": "Notes",
        },
    },
    # PurchaseCreate / PurchaseUpdate. Left out: grn_number (generated), purchase_status,
    # receiving_status, approval_status, requested_by, received_by (workflow).
    "purchase": {
        MANDATORY: {
            "invoice_number": "Supplier Invoice Number",
            "supplier_id": "Supplier",
            "items": "Line Items",
        },
        CONDITIONAL: {},
        NON_CONFIGURABLE: {},
        OPTIONAL: {
            "invoice_date": "Invoice Date",
            "purchase_date": "Purchase Date",
            "purchase_type": "Purchase Type",
            "financial_year": "Financial Year",
            "reference_number": "Reference Number",
            "contact_person": "Contact Person",
            "mobile_number": "Mobile Number",
            "email_address": "Email Address",
            "payee_gstin": "Supplier GSTIN",
            "billing_address": "Billing Address",
            "shipping_address": "Shipping Address",
            "warehouse_id": "Receiving Warehouse",
            "received_date": "Received Date",
            "discount": "Discount",
            "tax": "Tax",
            "freight_charges": "Freight Charges",
            "packing_charges": "Packing Charges",
            "insurance_charges": "Insurance Charges",
            "other_charges": "Other Charges",
            "round_off": "Round Off",
            "payment_method": "Payment Method",
            "payment_terms": "Payment Terms",
            "due_date": "Due Date",
            "amount_paid": "Amount Paid",
            "payment_reference": "Payment Reference",
            "purchase_account_id": "Purchase Account",
            "tax_category": "Tax Category",
            "cost_center_id": "Cost Center",
            "project_id": "Project",
            "attachment_url": "Attachment",
            "supplier_quotation_url": "Supplier Quotation",
            "purchase_order_url": "Purchase Order",
            "supplier_invoice_url": "Supplier Invoice",
            "delivery_challan_url": "Delivery Challan",
            "supporting_documents": "Supporting Documents",
            "terms_and_conditions": "Terms & Conditions",
            "internal_remarks": "Internal Remarks",
            "tags": "Tags",
            "notes": "Notes",
        },
    },
    # ExpenseCreate / ExpenseUpdate. Left out: expense_status, payment_status, approval_status
    # (workflow); the stored subtotal / total are calculated.
    "expense": {
        MANDATORY: {"category": "Category"},
        CONDITIONAL: {
            "amount": ("Amount", "Required (more than zero) when no line items are sent; "
                       "calculated from the items otherwise"),
        },
        NON_CONFIGURABLE: {},
        OPTIONAL: {
            "description": "Description",
            "expense_date": "Expense Date",
            "expense_type": "Expense Type",
            "items": "Line Items",
            "currency": "Currency",
            "financial_year": "Financial Year",
            "payment_mode": "Payment Mode",
            "payment_reference": "Payment Reference",
            "paid_from_account_id": "Paid From Account",
            "expense_account_id": "Expense Account",
            "vendor_id": "Vendor",
            "payee_name": "Payee Name",
            "contact_person": "Contact Person",
            "mobile_number": "Mobile Number",
            "email_address": "Email Address",
            "payee_gstin": "Payee GSTIN",
            "tax_rate": "Tax Rate (%)",
            "tax_amount": "Tax Amount",
            "tax_category": "Tax Category",
            "tds_applicable": "TDS Applicable",
            "tds_amount": "TDS Amount",
            "branch_id": "Branch",
            "department_id": "Department",
            "cost_center_id": "Cost Center",
            "project_id": "Project",
            "receipt_url": "Receipt",
            "vendor_invoice_url": "Vendor Invoice",
            "supporting_documents": "Supporting Documents",
            "is_recurring": "Recurring Expense",
            "recurrence_frequency": "Recurrence Frequency",
            "next_due_date": "Next Due Date",
            "tags": "Tags",
        },
    },
}

# field -> the field it needs: shown only while that one is (products.py rejects a
# sub_category_id sent without its category_id).
DEPENDS_ON: dict[str, dict[str, str]] = {"product": {"sub_category_id": "category_id"}}

LEGACY_MODULES = {"sales": "sales_order", "expenses": "expense"}

# Older keys whose meaning is unambiguous. Keys the old catalog shared with the API need no
# entry. Not mapped (dropped from the catalog): company.doc_other_files and
# company.employee_id_prefix.
LEGACY_FIELDS: dict[str, dict[str, str]] = {
    # `address` is only ever read as registered_address's fallback (invoice header, company
    # overview); `branch_address` is rewritten from branch_addresses on every save.
    "company": {"address": "registered_address", "branch_address": "branch_addresses"},
    # CustomerProfileIn's own column aliases (customer_profile.ALIASES), plus unaliased keys.
    "customer": {
        "name": "basic_information.customer_name",
        "business_name": "basic_information.legal_business_name",
        "category": "basic_information.customer_category",
        "phone": "contact_information.mobile_number",
        "email": "contact_information.email_address",
        "billing_address": "address_information.billing_address",
        "delivery_address": "address_information.shipping_address",
        "gst_number": "business_tax_information.gstin_tax_id",
        "credit_limit": "payment_information.credit_limit",
        "opening_balance": "payment_information.opening_balance",
        "notes": "additional_information.notes",
    },
    # Product.price is kept equal to pricing.selling_price on every write; `sub_category` and
    # `brand` are the free-text columns the model keeps beside sub_category_id / brand_id.
    "product": {
        "price": "pricing.selling_price",
        "min_stock": "minimum_stock_level",
        "sub_category": "sub_category_id",
        "brand": "brand_id",
        # The Product Type field: free text before the sub category's Product Types existed.
        "product_type": "product_type_id",
    },
    # POST /orders stores delivery_partner_id as assigned_delivery_partner_id.
    "sales_order": {"assigned_delivery_partner_id": "delivery_partner_id"},
}


def classification(module: str, field: str) -> str | None:
    spec = CATALOG[module]
    for kind in (MANDATORY, CONDITIONAL, NON_CONFIGURABLE, OPTIONAL):
        if field in spec[kind]:
            return kind
    return None


def _label_and_condition(spec: dict, kind: str, key: str) -> tuple[str, str | None]:
    value = spec[kind][key]
    return value if kind == CONDITIONAL else (value, None)


def available_fields() -> dict[str, dict]:
    """The catalog as GET /organizations/settings/fields returns it. `mandatory` and
    `optional` keep their old meaning; `conditional` and `non_configurable` list the other
    locked fields, and `fields` carries the per-field metadata."""
    result = {}
    for module, spec in CATALOG.items():
        depends = DEPENDS_ON.get(module, {})
        fields = []
        for kind in (MANDATORY, CONDITIONAL, NON_CONFIGURABLE, OPTIONAL):
            for key in spec[kind]:
                label, condition = _label_and_condition(spec, kind, key)
                configurable = kind == OPTIONAL
                fields.append({
                    "key": key,
                    "label": label,
                    "classification": kind,
                    "configurable": configurable,
                    "locked": not configurable,
                    "default_visible": True,
                    "condition": condition,
                    "depends_on": depends.get(key),
                })
        result[module] = {
            MANDATORY: list(spec[MANDATORY]),
            CONDITIONAL: list(spec[CONDITIONAL]),
            NON_CONFIGURABLE: list(spec[NON_CONFIGURABLE]),
            OPTIONAL: list(spec[OPTIONAL]),
            "fields": fields,
        }
    return result


def _canonical(raw_module: str, raw_field: str) -> tuple[str | None, str | None, int]:
    """(module, field, priority) for a stored or submitted key; priority ranks canonical
    module and field names above legacy ones."""
    module = LEGACY_MODULES.get(raw_module, raw_module)
    if module not in CATALOG:
        return None, None, 0
    if classification(module, raw_field):
        field, field_rank = raw_field, 1
    else:
        field, field_rank = LEGACY_FIELDS.get(module, {}).get(raw_field), 0
    if field is None:
        return module, None, 0
    return module, field, (2 if module == raw_module else 0) + field_rank


def saved_preferences(raw: object) -> dict[str, dict[str, bool]]:
    """The optional-field preferences a firm explicitly saved, by canonical key. Unknown,
    ambiguous or non-boolean entries, and anything saved for a locked field, are ignored --
    never applied to another field."""
    chosen: dict[tuple[str, str], tuple[int, bool]] = {}
    for raw_module, values in (raw.items() if isinstance(raw, dict) else ()):
        if not isinstance(values, dict):
            continue
        for raw_field, value in values.items():
            module, field, rank = _canonical(raw_module, raw_field)
            if field is None or not isinstance(value, bool) or classification(module, field) != OPTIONAL:
                continue
            if (module, field) not in chosen or rank > chosen[(module, field)][0]:
                chosen[(module, field)] = (rank, value)
    result: dict[str, dict[str, bool]] = {}
    for (module, field), (_, value) in chosen.items():
        result.setdefault(module, {})[field] = value
    return result


def _effective(saved: dict[str, dict[str, bool]]) -> dict[str, dict[str, bool]]:
    result = {}
    for module, spec in CATALOG.items():
        module_saved = saved.get(module, {})
        visible = {key: True for kind in LOCKED for key in spec[kind]}
        visible.update({key: module_saved.get(key, True) for key in spec[OPTIONAL]})
        # A dependent field is shown only while the field it needs is -- whatever was saved
        # for it, so no stored combination can show a Subcategory without its Category.
        for field, parent in DEPENDS_ON.get(module, {}).items():
            if not visible[parent]:
                visible[field] = False
        result[module] = visible
    return result


def effective_settings(raw: object) -> dict[str, dict[str, bool]]:
    """Visibility of every catalog field: locked fields are always shown; an optional field
    is shown unless the firm explicitly saved `false`, or the field it depends on is hidden."""
    return _effective(saved_preferences(raw))


def parse_update(payload: dict) -> tuple[dict[str, dict[str, bool]], list[dict]]:
    """Validate a PUT body's `field_settings`; returns (canonical updates, errors). Every
    key must be known, every value a JSON boolean, and a locked field may only be sent as
    `true` (a no-op)."""
    updates: dict[tuple[str, str], tuple[int, bool]] = {}
    errors: list[dict] = []
    for raw_module, values in payload.items():
        loc = ["body", "field_settings", raw_module]
        if LEGACY_MODULES.get(raw_module, raw_module) not in CATALOG:
            errors.append({"loc": loc, "msg": f"Unknown module '{raw_module}'"})
            continue
        for raw_field, value in values.items():
            module, field, rank = _canonical(raw_module, raw_field)
            if field is None:
                errors.append({"loc": loc + [raw_field], "msg": f"Unknown field '{raw_field}' in module '{raw_module}'"})
                continue
            if not isinstance(value, bool):
                errors.append({"loc": loc + [raw_field], "msg": "Must be true or false"})
                continue
            kind = classification(module, field)
            if kind != OPTIONAL:
                if value is False:
                    errors.append({"loc": loc + [raw_field], "msg": f"'{field}' is {kind} and cannot be hidden"})
                continue
            if (module, field) not in updates or rank > updates[(module, field)][0]:
                updates[(module, field)] = (rank, value)
    result: dict[str, dict[str, bool]] = {}
    for (module, field), (_, value) in updates.items():
        result.setdefault(module, {})[field] = value
    return result, errors


def dependency_errors(raw: object, updates: dict[str, dict[str, bool]]) -> list[dict]:
    """A request that explicitly shows a dependent field while the field it needs ends up
    hidden asks for something that cannot be shown. Hiding the parent alone is fine -- the
    dependent field simply follows it."""
    saved = saved_preferences(raw)
    errors = []
    for module, fields in DEPENDS_ON.items():
        sent = updates.get(module, {})
        for field, parent in fields.items():
            parent_visible = sent.get(parent, saved.get(module, {}).get(parent, True))
            if sent.get(field) is True and not parent_visible:
                label = CATALOG[module][OPTIONAL][field]
                parent_label = CATALOG[module][OPTIONAL][parent]
                errors.append({
                    "loc": ["body", "field_settings", module, field],
                    "msg": f"{label} can only be shown while {parent_label} ('{parent}') is shown",
                })
    return errors


def merged(raw: object, updates: dict[str, dict[str, bool]]) -> dict:
    """The stored JSON after a partial update: everything already stored is kept (legacy
    keys included), and each updated module gets its saved preferences written under their
    canonical keys, the update on top."""
    stored = dict(raw) if isinstance(raw, dict) else {}
    saved = saved_preferences(stored)
    for module, values in updates.items():
        existing = stored.get(module) if isinstance(stored.get(module), dict) else {}
        stored[module] = {**existing, **saved.get(module, {}), **values}
    return stored
