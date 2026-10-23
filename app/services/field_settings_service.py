"""Which form fields each module has, and which ones a firm has chosen to hide.

The catalog lists the user-entered inputs of each module's create/update API, by their API
names (nested ones as `section.field`). Visibility is a presentation preference only: the
module APIs keep validating exactly as before, and hiding a field never touches stored data.

  mandatory    required by the API; always shown, cannot be hidden
  conditional  required only in some situations (see `condition`); always shown, cannot be hidden
  optional     may be hidden; shown unless the firm saved `false` for it

System-managed values (generated numbers, workflow and approval statuses, calculated stock or
totals, read-only display values, aliases) are not form fields and are left out.

Preferences live in Organization.field_settings as {module: {field: bool}}, holding only what
a firm explicitly saved. Older saves used other module names (`sales`, `expenses`) and some
other field names; those are read through LEGACY_MODULES / LEGACY_FIELDS, with a canonical
key always winning over a legacy one.
"""

from __future__ import annotations

MANDATORY, CONDITIONAL, OPTIONAL = "mandatory", "conditional", "optional"

# module -> {"mandatory": [...], "conditional": {field: condition}, "optional": [...]}
CATALOG: dict[str, dict] = {
    # PUT /organizations/settings (CompanySettingsUpdate). `name` is required at registration
    # and cannot be blanked (min_length=1); nothing else is required -- the completeness
    # checklist (REQUIRED_COMPANY_FIELDS) is reported, never enforced. Left out: company_status
    # (active toggle), tax_configuration / invoice_settings / number_prefixes /
    # employee_id_prefix (configuration, not form inputs) and doc_other_files (own endpoint).
    "company": {
        MANDATORY: ["name"],
        CONDITIONAL: {},
        OPTIONAL: [
            "legal_name", "business_type", "industry", "date_of_incorporation", "cin_number",
            "gst_number", "pan_number", "gstin_pan", "description", "financial_year",
            "phone", "primary_mobile", "alternate_mobile", "landline", "email", "website",
            "customer_support_number", "whatsapp_number",
            "address", "registered_address", "branch_address", "branch_addresses",
            "city", "state", "country", "pin_code", "maps_latitude", "maps_longitude",
            "logo_url", "signature_url", "stamp_url", "letterhead_url", "banner_url", "payment_qr_url",
            "upi_id", "bank_name", "bank_account_details", "bank_account_holder", "bank_ifsc",
            "facebook_url", "instagram_url", "linkedin_url", "twitter_url", "youtube_url",
            "currency", "timezone", "language",
            "doc_gst_url", "doc_pan_url", "doc_coi_url", "doc_trade_license_url", "doc_msme_url",
            "doc_fssai_url", "doc_other_url",
            "auth_person_name", "auth_person_designation", "auth_person_mobile", "auth_person_email",
            "auth_person_photo_url", "auth_person_signature_url",
            "employee_count", "business_hours", "mission_vision", "notes",
        ],
    },
    # LeadCreate / LeadUpdate. Left out: lead_id (generated), lead_status (always `new` on
    # create, then workflow), converted_customer_id / converted_at (conversion), and the
    # aliases mobile, source, status, assigned_sales_officer_id.
    "lead": {
        MANDATORY: ["name", "mobile_number", "lead_source"],
        CONDITIONAL: {},
        OPTIONAL: [
            "contact_person", "email", "interested_product", "interested_product_ids",
            "lead_type", "segment", "customer_id", "assigned_salesperson_id", "notes",
        ],
    },
    # CustomerProfileIn (nested sections). customer_name is enforced by POST /customers.
    # Left out: basic_information.status (account lifecycle) and
    # sales_crm_information.sales_representative (read-only, resolved from the id).
    "customer": {
        MANDATORY: ["basic_information.customer_name"],
        CONDITIONAL: {},
        OPTIONAL: [
            "basic_information.customer_type", "basic_information.legal_business_name",
            "basic_information.display_name", "basic_information.industry",
            "basic_information.customer_category", "basic_information.customer_since",
            "basic_information.profile_image_id",
            "contact_information.primary_contact_person", "contact_information.designation",
            "contact_information.mobile_number", "contact_information.alternate_mobile_number",
            "contact_information.email_address", "contact_information.website",
            "address_information.billing_address", "address_information.shipping_address",
            "address_information.city", "address_information.state", "address_information.country",
            "address_information.pin_zip_code", "address_information.google_maps_location",
            "business_tax_information.gstin_tax_id", "business_tax_information.pan_business_registration_no",
            "business_tax_information.tax_exempt", "business_tax_information.tax_category",
            "business_tax_information.currency",
            "payment_information.payment_terms", "payment_information.credit_limit",
            "payment_information.opening_balance", "payment_information.preferred_payment_method",
            "payment_information.upi_id", "payment_information.bank_name",
            "payment_information.account_number", "payment_information.ifsc_swift_code",
            "sales_crm_information.sales_representative_id", "sales_crm_information.lead_source",
            "sales_crm_information.territory", "sales_crm_information.customer_priority",
            "sales_crm_information.preferred_communication", "sales_crm_information.customer_tags",
            "social_media_online_presence.facebook_url", "social_media_online_presence.instagram_url",
            "social_media_online_presence.linkedin_url", "social_media_online_presence.x_twitter_url",
            "social_media_online_presence.youtube_url",
            "additional_information.date_of_birth", "additional_information.anniversary_date",
            "additional_information.referral_customer_id", "additional_information.loyalty_number",
            "additional_information.notes",
            "preferences.portal_access_enabled", "preferences.favorite_product_ids",
            "preferences.default_price_list_id", "preferences.default_tax_profile_id",
            "preferences.default_warehouse_id", "preferences.preferred_invoice_delivery",
            "documents.gst_certificate_id", "documents.pan_card_id",
            "documents.business_registration_certificate_id", "documents.address_proof_id",
            "documents.purchase_agreement_id", "documents.other_document_ids",
        ],
    },
    # QuotationCreate / QuotationUpdate. Left out: quotation_number (generated), status
    # (always `draft` on create, then workflow).
    "quotation": {
        MANDATORY: ["items"],
        CONDITIONAL: {
            "customer_id": "Exactly one of customer_id or lead_id is required",
            "lead_id": "Exactly one of customer_id or lead_id is required",
        },
        OPTIONAL: [
            "quotation_date", "valid_until", "billing_address", "shipping_address", "salesperson_id",
            "currency", "payment_terms", "delivery_terms", "notes", "terms_conditions",
        ],
    },
    # ProductCreate / ProductUpdate; prices are only accepted under `pricing`. Left out:
    # product_id (generated), status / is_active (lifecycle), total_inventory (stock).
    "product": {
        MANDATORY: ["name"],
        CONDITIONAL: {},
        OPTIONAL: [
            "short_name", "description", "sku", "barcode", "product_type", "category_id",
            "sub_category", "sub_category_id", "brand", "brand_id", "vendor", "manufacturer",
            "model_number",
            "pricing.purchase_price", "pricing.selling_price", "pricing.mrp", "pricing.wholesale_price",
            "pricing.dealer_price", "pricing.discount_percent", "pricing.tax_inclusive", "pricing.currency",
            "hsn_code", "tax_rate", "tax_category",
            "cover_image", "images", "product_video", "product_catalog_brochure", "product_manual",
            "product_datasheet", "compliance_certificate", "warranty_document",
            "inventory_tracking", "uom", "opening_stock", "minimum_stock_level", "maximum_stock_level",
            "reorder_level", "reorder_quantity", "bin_shelf_location", "batch_tracking",
            "serial_number_tracking", "expiry_tracking",
            "preferred_supplier_id", "supplier_product_code", "lead_time", "minimum_order_quantity",
            "purchase_unit", "sales_unit",
            "commission_eligible", "commission", "default_discount",
            "weight", "weight_unit", "length", "width", "height", "volume", "color", "material",
            "physical_size",
            "has_variants", "variant_attributes", "variations",
            "downloadable_product", "download_file", "license_key_required", "download_limit",
            "warranty_period", "warranty_period_unit", "shelf_life", "shelf_life_unit",
            "country_of_origin", "launch_date", "end_of_life_date", "product_tags", "notes",
        ],
    },
    # OrderCreate / OrderUpdate (POST /sales-orders). Left out: order_status and
    # payment_status (workflow).
    "sales_order": {
        MANDATORY: ["customer_id", "items"],
        CONDITIONAL: {
            "delivery_address": "Required for home delivery when neither shipping_address nor the "
                                "customer's saved delivery address is available",
            "paid_amount": "Required for a partial payment: more than zero and less than the order total",
        },
        OPTIONAL: [
            "order_date", "delivery_date", "warehouse_id", "quotation_id", "salesperson_id", "source",
            "fulfilment_method", "delivery_method", "billing_address", "shipping_address",
            "delivery_partner_id", "vehicle_id", "currency", "discount", "tax",
            "payment_type", "payment_method", "payment_terms", "payment_terms_days", "delivery_terms",
            "customer_po_number", "payment_proof_url", "delivery_proof_url", "notes",
        ],
    },
    # SupplierCreate / SupplierUpdate. Left out: supplier_code (generated).
    "supplier": {
        MANDATORY: ["name"],
        CONDITIONAL: {},
        OPTIONAL: [
            "company_name", "contact_person", "phone", "email", "gst_number", "pan_number",
            "category", "supplier_categories", "supplier_type", "payment_terms", "credit_limit",
            "opening_balance", "address", "city", "state", "pincode", "country", "notes",
        ],
    },
    # PurchaseCreate / PurchaseUpdate. Left out: grn_number (generated), purchase_status,
    # receiving_status, approval_status, requested_by, received_by (workflow).
    "purchase": {
        MANDATORY: ["invoice_number", "supplier_id", "items"],
        CONDITIONAL: {},
        OPTIONAL: [
            "invoice_date", "purchase_date", "purchase_type", "financial_year", "reference_number",
            "contact_person", "mobile_number", "email_address", "payee_gstin",
            "billing_address", "shipping_address", "warehouse_id", "received_date",
            "discount", "tax", "freight_charges", "packing_charges", "insurance_charges",
            "other_charges", "round_off",
            "payment_method", "payment_terms", "due_date", "amount_paid", "payment_reference",
            "purchase_account_id", "tax_category", "cost_center_id", "project_id",
            "attachment_url", "supplier_quotation_url", "purchase_order_url", "supplier_invoice_url",
            "delivery_challan_url", "supporting_documents",
            "terms_and_conditions", "internal_remarks", "tags", "notes",
        ],
    },
    # ExpenseCreate / ExpenseUpdate. Left out: expense_status, payment_status, approval_status
    # (workflow); the stored subtotal / total are calculated.
    "expense": {
        MANDATORY: ["category"],
        CONDITIONAL: {
            "amount": "Required (more than zero) when no line items are sent; calculated from the items otherwise",
        },
        OPTIONAL: [
            "description", "expense_date", "expense_type", "items", "currency", "financial_year",
            "payment_mode", "payment_reference", "paid_from_account_id", "expense_account_id",
            "vendor_id", "payee_name", "contact_person", "mobile_number", "email_address", "payee_gstin",
            "tax_rate", "tax_amount", "tax_category", "tds_applicable", "tds_amount",
            "branch_id", "department_id", "cost_center_id", "project_id",
            "receipt_url", "vendor_invoice_url", "supporting_documents",
            "is_recurring", "recurrence_frequency", "next_due_date", "tags",
        ],
    },
}

LEGACY_MODULES = {"sales": "sales_order", "expenses": "expense"}

# Old catalog keys whose meaning is unambiguous. Keys the old catalog shared with the API
# need no entry. Not mapped (dropped from the catalog): company.doc_other_files and
# company.employee_id_prefix.
LEGACY_FIELDS: dict[str, dict[str, str]] = {
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
    # Product.price is kept equal to pricing.selling_price on every write.
    "product": {"price": "pricing.selling_price", "min_stock": "minimum_stock_level"},
    # POST /sales-orders stores delivery_partner_id as assigned_delivery_partner_id.
    "sales_order": {"assigned_delivery_partner_id": "delivery_partner_id"},
}

_ACRONYMS = {
    "id": "ID", "ids": "IDs", "gst": "GST", "gstin": "GSTIN", "pan": "PAN", "upi": "UPI",
    "ifsc": "IFSC", "hsn": "HSN", "url": "URL", "cin": "CIN", "tds": "TDS", "uom": "UOM",
    "po": "PO", "msme": "MSME", "fssai": "FSSAI", "coi": "COI", "qr": "QR", "mrp": "MRP",
    "sku": "SKU",
}


def label(key: str) -> str:
    """`contact_information.email_address` -> "Email Address", `pricing.mrp` -> "MRP"."""
    return " ".join(_ACRONYMS.get(word, word.capitalize()) for word in key.rsplit(".", 1)[-1].split("_"))


def classification(module: str, field: str) -> str | None:
    spec = CATALOG[module]
    if field in spec[MANDATORY]:
        return MANDATORY
    if field in spec[CONDITIONAL]:
        return CONDITIONAL
    if field in spec[OPTIONAL]:
        return OPTIONAL
    return None


def available_fields() -> dict[str, dict]:
    """The catalog as GET /organizations/settings/fields returns it. `mandatory` and
    `optional` keep their old meaning; `conditional` lists the fields required only in some
    situations, and `fields` carries the per-field metadata."""
    result = {}
    for module, spec in CATALOG.items():
        fields = []
        for kind, keys in ((MANDATORY, spec[MANDATORY]), (CONDITIONAL, list(spec[CONDITIONAL])),
                           (OPTIONAL, spec[OPTIONAL])):
            for key in keys:
                configurable = kind == OPTIONAL
                fields.append({
                    "key": key,
                    "label": label(key),
                    "classification": kind,
                    "configurable": configurable,
                    "locked": not configurable,
                    "default_visible": True,
                    "condition": spec[CONDITIONAL].get(key),
                })
        result[module] = {
            MANDATORY: list(spec[MANDATORY]),
            CONDITIONAL: list(spec[CONDITIONAL]),
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
    ambiguous or non-boolean entries are ignored, never applied to another field."""
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


def effective_settings(raw: object) -> dict[str, dict[str, bool]]:
    """Visibility of every catalog field: locked fields are always shown; an optional field
    is shown unless the firm explicitly saved `false`."""
    saved = saved_preferences(raw)
    result = {}
    for module, spec in CATALOG.items():
        module_saved = saved.get(module, {})
        visible = {key: True for key in spec[MANDATORY]}
        visible.update({key: True for key in spec[CONDITIONAL]})
        visible.update({key: module_saved.get(key, True) for key in spec[OPTIONAL]})
        result[module] = visible
    return result


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
