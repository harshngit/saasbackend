"""On-the-fly PDF generation for receipts, invoices, and delivery challans."""

from datetime import datetime, timezone
from io import BytesIO

from fpdf import FPDF
from fpdf.enums import XPos, YPos


def _s(text) -> str:
    """Make text safe for fpdf's latin-1 core fonts (replace unencodable chars)."""
    return str("" if text is None else text).encode("latin-1", "replace").decode("latin-1")


def _org_header(pdf: FPDF, org, settings: dict | None = None) -> None:
    """Draw company header respecting business_details settings."""
    biz = (settings.get("business_details") or {}) if settings else {}
    show_name = biz.get("show_business_name", True)
    show_gstin = biz.get("show_gstin", True)
    show_pan = biz.get("show_pan", False)
    show_address = biz.get("show_address", True)
    show_phone = biz.get("show_phone", True)
    show_email = biz.get("show_email", True)

    _font(pdf, settings, "B", "heading")
    if show_name:
        pdf.cell(0, 9, _s(org.name if org else "Company"), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    
    _font(pdf, settings, "", "body")
    if org and show_gstin and org.gst_number:
        pdf.cell(0, 5, _s(f"GSTIN: {org.gst_number}"), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    if org and show_pan:
        pan = getattr(org, "pan_number", None) or getattr(org, "gstin_pan", None)
        if pan:
            pdf.cell(0, 5, _s(f"PAN: {pan}"), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    if org and show_address and org.address:
        pdf.cell(0, 5, _s(str(org.address)[:90]), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    if org and show_phone and org.phone:
        pdf.cell(0, 5, _s(f"Phone: {org.phone}"), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    if org and show_email and getattr(org, "email", None):
        pdf.cell(0, 5, _s(f"Email: {org.email}"), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(3)


def payment_receipt_pdf(org, customer, payment) -> bytes:
    """A simple payment receipt for money received from a customer."""
    pdf = FPDF()
    pdf.add_page()
    _org_header(pdf, org)

    pdf.set_font("Helvetica", "B", 13)
    pdf.cell(0, 8, "PAYMENT RECEIPT", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_font("Helvetica", size=10)
    pdf.cell(0, 6, _s(f"Receipt No: RCPT-{str(payment.id)[:8].upper()}"), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.cell(0, 6, _s(f"Date: {payment.received_on.date().isoformat()}"), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(2)

    cust_name = customer.name if customer else "Customer"
    cust_biz = f" ({customer.business_name})" if customer and customer.business_name else ""
    pdf.cell(0, 6, _s(f"Received from: {cust_name}{cust_biz}"),
             new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(2)

    pdf.set_font("Helvetica", size=10)
    if payment.order_amount is not None:
        pdf.cell(60, 7, "Order Amount", border=1)
        pdf.cell(60, 7, _s(f"Rs {payment.order_amount:,.2f}"), border=1, new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    if payment.previous_pending is not None:
        pdf.cell(60, 7, "Previous Pending", border=1)
        pdf.cell(60, 7, _s(f"Rs {payment.previous_pending:,.2f}"), border=1, new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    pdf.set_font("Helvetica", "B", 11)
    pdf.cell(60, 8, "Amount Collected", border=1)
    pdf.set_font("Helvetica", size=11)
    pdf.cell(60, 8, _s(f"Rs {payment.amount:,.2f}"), border=1, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_font("Helvetica", size=10)
    pdf.cell(60, 7, "Payment Method", border=1)
    pdf.cell(60, 7, _s(payment.payment_mode), border=1, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    if payment.reference:
        pdf.cell(60, 7, "Reference", border=1)
        pdf.cell(60, 7, _s(payment.reference), border=1, new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    rem_bal = payment.remaining_receivable if payment.remaining_receivable is not None else ((customer.outstanding_balance or 0) if customer else 0)
    pdf.cell(60, 7, "Remaining Receivable", border=1)
    pdf.cell(60, 7, _s(f"Rs {rem_bal:,.2f}"), border=1, new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    pdf.ln(8)
    pdf.set_font("Helvetica", "I", 8)
    pdf.cell(0, 5, f"Generated on {datetime.now(timezone.utc).date().isoformat()} - computer-generated receipt.",
             new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    return bytes(pdf.output())


def delivery_receipt_pdf(
    org,
    customer,
    delivery,
    order=None,
    partner=None,
    vehicle=None,
) -> bytes:
    """A clean PDF delivery receipt for delivered goods based on Delivery and delivered_quantity."""
    pdf = FPDF()
    pdf.add_page()
    _org_header(pdf, org)

    # Title
    pdf.set_font("Helvetica", "B", 14)
    pdf.cell(0, 8, "DELIVERY RECEIPT", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_font("Helvetica", size=8)
    pdf.cell(0, 4, "Delivery acknowledgement document. Not a tax invoice or payment receipt.", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(3)

    # Delivery & Order details
    pdf.set_font("Helvetica", size=9)
    col_width = 90
    
    pdf.set_font("Helvetica", "B", 10)
    pdf.cell(col_width, 5, "DELIVERED TO:", new_x=XPos.RIGHT, new_y=YPos.LAST)
    pdf.set_x(col_width + 10)
    pdf.cell(col_width, 5, "DELIVERY DETAILS:", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    
    pdf.set_font("Helvetica", size=9)
    
    cust_name = "-"
    if customer:
        cust_name = customer.business_name or customer.name
    elif order and getattr(order, "customer", None):
        cust_name = order.customer.business_name or order.customer.name

    addr = "-"
    if hasattr(delivery, "delivery_address") and delivery.delivery_address:
        addr = delivery.delivery_address
    elif customer:
        addr = customer.delivery_address or customer.billing_address or "-"
    elif order and getattr(order, "delivery_address", None):
        addr = order.delivery_address

    deliv_no = getattr(delivery, "delivery_note_number", None) or getattr(delivery, "delivery_number", "DELIV")
    order_no = getattr(order, "order_number", None) if order else "-"
    
    date_val = getattr(delivery, "confirmed_at", None) or getattr(delivery, "dispatched_at", None) or getattr(delivery, "delivery_date", None) or datetime.now(timezone.utc)
    date_str = date_val.date().isoformat() if hasattr(date_val, "date") else str(date_val)[:10]

    phone_str = f"Phone: {customer.phone}" if customer and customer.phone else "Phone: N/A"
    partner_name = partner.name if partner else "-"
    vehicle_num = vehicle.vehicle_number if vehicle else "-"

    rows = [
        (_s(cust_name), _s(f"Delivery No: {deliv_no}")),
        (_s(f"Address: {addr[:45]}"), _s(f"Order No: {order_no}")),
        (_s(phone_str), _s(f"Delivery Date: {date_str}")),
        ("", _s(f"Partner: {partner_name} | Vehicle: {vehicle_num}")),
    ]
    if hasattr(delivery, "receiver_name") and delivery.receiver_name:
        rows.append(("", _s(f"Receiver: {delivery.receiver_name}")))

    for left, right in rows:
        pdf.cell(col_width, 5, left, new_x=XPos.RIGHT, new_y=YPos.LAST)
        pdf.set_x(col_width + 10)
        pdf.cell(col_width, 5, right, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    
    pdf.ln(4)

    # Line Items Table
    widths = [130, 50]
    headers = ["Item Name", "Delivered Quantity"]
    pdf.set_font("Helvetica", "B", 9)
    for w, h in zip(widths, headers):
        pdf.cell(w, 7, h, border=1, align="L" if w == 130 else "R")
    pdf.ln(7)
    
    pdf.set_font("Helvetica", size=9)
    items = getattr(delivery, "items", []) or (getattr(order, "items", []) if order else [])
    total_delivered = 0.0
    for item in items:
        qty = getattr(item, "delivered_quantity", None)
        if qty is None:
            qty = getattr(item, "quantity", 0)
        total_delivered += float(qty or 0)
        pdf.cell(widths[0], 6, _s(item.product_name[:60]), border=1)
        pdf.cell(widths[1], 6, f"{qty:g}", border=1, align="R", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        
    pdf.set_font("Helvetica", "B", 9)
    pdf.cell(widths[0], 6, "Total Delivered Units", border=1)
    pdf.cell(widths[1], 6, f"{total_delivered:g}", border=1, align="R", new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    if hasattr(delivery, "notes") and delivery.notes:
        pdf.ln(3)
        pdf.set_font("Helvetica", size=8)
        pdf.multi_cell(0, 4, _s(f"Notes: {delivery.notes}"), new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    pdf.ln(8)
    
    # Signature fields
    pdf.set_font("Helvetica", size=9)
    pdf.cell(90, 5, "Received By: __________________________", new_x=XPos.RIGHT, new_y=YPos.LAST)
    pdf.set_x(110)
    pdf.cell(90, 5, "Delivered By: __________________________", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    
    pdf.ln(8)
    pdf.set_font("Helvetica", "I", 8)
    pdf.cell(0, 5, f"Generated on {datetime.now(timezone.utc).date().isoformat()} - Delivery Receipt.",
             new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    return bytes(pdf.output())


def quotation_pdf(org, customer, quotation, lead=None) -> bytes:
    """The quotation as the customer receives it: quoted lines, terms and validity."""
    pdf = FPDF()
    pdf.add_page()
    _org_header(pdf, org)

    pdf.set_font("Helvetica", "B", 14)
    pdf.cell(0, 8, "QUOTATION", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(2)

    col = 90
    pdf.set_font("Helvetica", "B", 10)
    pdf.cell(col, 5, "QUOTED TO:", new_x=XPos.RIGHT, new_y=YPos.LAST)
    pdf.set_x(col + 10)
    pdf.cell(col, 5, "DETAILS:", new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    pdf.set_font("Helvetica", size=9)
    if customer:
        party_name = customer.business_name or customer.name
        party_phone = f"Phone: {customer.phone}" if customer.phone else "Phone: N/A"
        party_fourth_line = f"GSTIN: {customer.gst_number}" if customer.gst_number else ""
    elif lead:
        party_name = lead.name or lead.contact_person or "-"
        party_phone = f"Phone: {lead.mobile_number}" if lead.mobile_number else "Phone: N/A"
        party_fourth_line = f"Email: {lead.email}" if lead.email else ""
    else:
        party_name = "-"
        party_phone = "Phone: N/A"
        party_fourth_line = ""

    rows = [
        (
            _s(party_name),
            _s(f"Quotation No: {quotation.quotation_number}"),
        ),
        (
            _s((quotation.billing_address or (customer.billing_address if customer else "") or "")[:45]),
            _s(f"Date: {quotation.quotation_date.date().isoformat() if quotation.quotation_date else '-'}"),
        ),
        (
            _s(party_phone),
            _s(f"Valid until: {quotation.valid_until.date().isoformat() if quotation.valid_until else '-'}"),
        ),
        (
            _s(party_fourth_line),
            _s(f"Status: {(quotation.status or '').title()}"),
        ),
    ]
    for left, right in rows:
        pdf.cell(col, 5, left, new_x=XPos.RIGHT, new_y=YPos.LAST)
        pdf.set_x(col + 10)
        pdf.cell(col, 5, right, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(4)

    widths = [78, 18, 26, 22, 16, 30]
    headers = ["Item", "Qty", "Rate", "Discount", "Tax %", "Amount"]
    pdf.set_font("Helvetica", "B", 9)
    for w, header in zip(widths, headers):
        pdf.cell(w, 7, header, border=1, align="L" if w == 78 else "R")
    pdf.ln(7)

    pdf.set_font("Helvetica", size=9)
    for item in quotation.items:
        cells = [
            (_s(item.product_name)[:44], "L"),
            (f"{item.quantity:g}", "R"),
            (f"{item.unit_price:,.2f}", "R"),
            (f"{(item.discount or 0):,.2f}", "R"),
            (f"{(item.tax_rate or 0):g}", "R"),
            (f"{item.line_total:,.2f}", "R"),
        ]
        for w, (text, align) in zip(widths, cells):
            pdf.cell(w, 6, text, border=1, align=align)
        pdf.ln(6)

    pdf.ln(2)
    pdf.set_font("Helvetica", size=10)
    for label, value in (
        ("Subtotal", quotation.subtotal),
        ("Tax", quotation.tax_total),
    ):
        pdf.cell(sum(widths) - 30, 6, f"{label}:", align="R")
        pdf.cell(30, 6, f"{value:,.2f}", align="R", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_font("Helvetica", "B", 11)
    pdf.cell(sum(widths) - 30, 7, "Total:", align="R")
    pdf.cell(30, 7, f"{quotation.total:,.2f}", align="R", new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    pdf.ln(4)
    pdf.set_font("Helvetica", size=8)
    for label, value in (
        ("Payment terms", quotation.payment_terms),
        ("Delivery terms", quotation.delivery_terms),
        ("Notes", quotation.notes),
        ("Terms & conditions", quotation.terms_conditions),
    ):
        if value:
            pdf.multi_cell(0, 4, _s(f"{label}: {value}"), new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    return bytes(pdf.output())


def delivery_challan_pdf(org, delivery, order, customer, partner, vehicle) -> bytes:
    """The delivery challan / goods-movement note that travels with the vehicle."""
    pdf = FPDF()
    pdf.add_page()
    _org_header(pdf, org)

    pdf.set_font("Helvetica", "B", 14)
    pdf.cell(0, 8, "DELIVERY CHALLAN", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_font("Helvetica", size=8)
    pdf.cell(
        0, 5, "Goods movement document. Not a tax invoice.",
        new_x=XPos.LMARGIN, new_y=YPos.NEXT,
    )
    pdf.ln(2)

    col = 90
    pdf.set_font("Helvetica", "B", 10)
    pdf.cell(col, 5, "DELIVER TO:", new_x=XPos.RIGHT, new_y=YPos.LAST)
    pdf.set_x(col + 10)
    pdf.cell(col, 5, "DISPATCH DETAILS:", new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    dispatched = (
        delivery.dispatched_at.date().isoformat() if delivery.dispatched_at else "not dispatched"
    )
    pdf.set_font("Helvetica", size=9)
    rows = [
        (
            _s(customer.business_name or customer.name) if customer else "-",
            _s(f"Challan No: {delivery.delivery_note_number}"),
        ),
        (
            _s((delivery.delivery_address or "")[:45]),
            _s(f"Order No: {order.order_number}") if order else "",
        ),
        (
            _s(f"Phone: {customer.phone}" if customer and customer.phone else "Phone: N/A"),
            _s(f"Dispatch date: {dispatched}"),
        ),
        (
            _s(f"GSTIN: {customer.gst_number}" if customer and customer.gst_number else ""),
            _s(f"Vehicle: {vehicle.vehicle_number}" if vehicle else "Vehicle: -"),
        ),
        ("", _s(f"Delivery partner: {partner.name}" if partner else "Delivery partner: -")),
        ("", _s(f"Status: {delivery.status.replace('_', ' ').title()}")),
    ]
    for left, right in rows:
        pdf.cell(col, 5, left, new_x=XPos.RIGHT, new_y=YPos.LAST)
        pdf.set_x(col + 10)
        pdf.cell(col, 5, right, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(4)

    widths = [96, 32, 32, 30]
    headers = ["Item", "Planned", "Loaded", "Delivered"]
    pdf.set_font("Helvetica", "B", 9)
    for w, header in zip(widths, headers):
        pdf.cell(w, 7, header, border=1, align="L" if w == 96 else "R")
    pdf.ln(7)

    pdf.set_font("Helvetica", size=9)
    for item in delivery.items:
        cells = [
            (_s(item.product_name)[:54], "L"),
            (f"{item.planned_quantity:g}", "R"),
            (f"{item.loaded_quantity:g}", "R"),
            (f"{item.delivered_quantity:g}", "R"),
        ]
        for w, (text, align) in zip(widths, cells):
            pdf.cell(w, 6, text, border=1, align=align)
        pdf.ln(6)

    pdf.set_font("Helvetica", "B", 9)
    pdf.cell(widths[0], 6, "Total", border=1)
    totals = (delivery.planned_total, delivery.loaded_total, delivery.delivered_total)
    for width, value in zip(widths[1:], totals):
        pdf.cell(width, 6, f"{value:g}", border=1, align="R")
    pdf.ln(10)

    pdf.set_font("Helvetica", size=8)
    if delivery.notes:
        pdf.multi_cell(0, 4, _s(f"Notes: {delivery.notes}"), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(8)
    pdf.cell(90, 5, "Receiver's signature", border="T", align="C")
    pdf.set_x(110)
    pdf.cell(80, 5, "Dispatched by", border="T", align="C")

    return bytes(pdf.output())


# --------------------- invoice: simple and detailed formats ---------------------

_THERMAL_WIDTHS = {"58mm": (58, 250), "80mm": (80, 250), "110mm": (110, 250)}

_TEMPLATE_STYLES = {
    "classic": {
        "title_size": 14, "body_size": 9, "table_size": 8, "row_height": 6,
        "header_band": False, "border": 1, "gap": 4, "margin": None,
    },
    "modern": {
        "title_size": 17, "body_size": 9, "table_size": 8, "row_height": 7,
        "header_band": True, "border": "B", "gap": 5, "margin": None,
    },
    "compact": {
        "title_size": 11, "body_size": 7, "table_size": 6.5, "row_height": 4.5,
        "header_band": False, "border": "B", "gap": 2, "margin": 8,
    },
    "thermal": {
        "title_size": 11, "body_size": 7, "table_size": 6.5, "row_height": 4,
        "header_band": False, "border": 0, "gap": 2, "margin": 4,
    },
}

COLUMN_WEIGHTS = {
    "product": 42.0,
    "description": 30.0,
    "product_image": 16.0,
    "hsn_sac": 18.0,
    "quantity": 14.0,
    "uom": 12.0,
    "rate": 20.0,
    "mrp": 18.0,
    "discount": 16.0,
    "tax_rate": 14.0,
    "tax_amount": 18.0,
    "batch_number": 16.0,
    "expiry_date": 18.0,
    "amount": 24.0,
}

COLUMN_HEADERS = {
    "product": "Item",
    "description": "Description",
    "product_image": "Image",
    "hsn_sac": "HSN/SAC",
    "quantity": "Qty",
    "uom": "UOM",
    "rate": "Rate",
    "mrp": "MRP",
    "discount": "Disc",
    "tax_rate": "Tax %",
    "tax_amount": "Tax",
    "batch_number": "Batch",
    "expiry_date": "Expiry",
    "amount": "Amount",
}

COLUMN_ALIGNS = {
    "product": "L",
    "description": "L",
    "product_image": "C",
    "hsn_sac": "C",
    "quantity": "R",
    "uom": "C",
    "rate": "R",
    "mrp": "R",
    "discount": "R",
    "tax_rate": "R",
    "tax_amount": "R",
    "batch_number": "C",
    "expiry_date": "C",
    "amount": "R",
}


def _style(settings: dict) -> dict:
    """The look this firm has chosen, defaulting to classic."""
    name = str(settings.get("template") or "classic").strip().lower()
    return _TEMPLATE_STYLES.get(name, _TEMPLATE_STYLES["classic"])


def _hex_rgb(value) -> tuple[int, int, int] | None:
    """A #rrggbb brand colour as an RGB triple, or None when it is not usable."""
    text = str(value or "").strip().lstrip("#")
    if len(text) != 6:
        return None
    try:
        return int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16)
    except ValueError:
        return None


def _money(value) -> str:
    return f"{(value or 0):,.2f}"


def _font(
    pdf: FPDF,
    settings: dict | None,
    style_weight: str = "",
    size_type: str = "body",
    custom_size: float | None = None,
    offset: float = 0.0,
) -> None:
    """Centralized font setter that respects typography settings and font family."""
    settings = settings or {}
    typography = settings.get("typography") or {}
    family = str(typography.get("font_family") or "Helvetica").strip().title()
    if family not in ("Helvetica", "Times", "Courier"):
        family = "Helvetica"

    template = str(settings.get("template") or "classic").strip().lower()
    thermal_cfg = settings.get("thermal_print") or {}
    if template == "thermal" and thermal_cfg.get("bold_text") is False and style_weight == "B":
        style_weight = ""

    if custom_size is not None:
        size = float(custom_size)
    elif size_type == "heading":
        size = float(typography.get("heading_size", 16))
    elif size_type == "table":
        size = float(typography.get("table_size", 8))
    else:
        size = float(typography.get("body_size", 9))

    pdf.set_font(family, style_weight, max(size + offset, 4.0))


def _invoice_pdf_page(settings: dict) -> FPDF:
    """A page configured with regular_print or thermal_print settings."""
    template = str(settings.get("template") or "").strip().lower()
    paper_size_setting = str(settings.get("paper_size") or "").strip().lower()

    if template == "thermal" or paper_size_setting == "thermal":
        thermal_cfg = settings.get("thermal_print") or {}
        paper_width_key = str(thermal_cfg.get("paper_width") or "80mm").lower()
        size = _THERMAL_WIDTHS.get(paper_width_key, _THERMAL_WIDTHS["80mm"])
        pdf = FPDF(format=size)
        pdf.set_margins(4, 4, 4)
        pdf.set_auto_page_break(True, margin=4)
        pdf.add_page()
        return pdf

    reg_cfg = settings.get("regular_print") or {}
    paper_size = str(reg_cfg.get("paper_size") or settings.get("paper_size") or "A4").upper()
    if paper_size not in ("A4", "A5"):
        paper_size = "A4"
    orientation = str(reg_cfg.get("orientation") or "portrait").lower()
    if orientation not in ("portrait", "landscape"):
        orientation = "portrait"

    margin_top = float(reg_cfg.get("margin_top", 10.0))
    margin_right = float(reg_cfg.get("margin_right", 10.0))
    margin_bottom = float(reg_cfg.get("margin_bottom", 10.0))
    margin_left = float(reg_cfg.get("margin_left", 10.0))

    pdf = FPDF(orientation=orientation, format=paper_size)
    pdf.set_left_margin(margin_left)
    pdf.set_right_margin(margin_right)
    pdf.set_top_margin(margin_top)
    pdf.set_auto_page_break(True, margin=margin_bottom)
    pdf.add_page()
    return pdf


def _usable_width(pdf: FPDF) -> float:
    return pdf.w - pdf.l_margin - pdf.r_margin


def _letterhead(pdf: FPDF, letterhead: bytes | None, settings: dict) -> None:
    if not letterhead:
        return
    template = str(settings.get("template") or "classic").strip().lower()
    if template == "thermal":
        return
    try:
        pdf.image(BytesIO(letterhead), x=0, y=0, w=pdf.w)
    except Exception:  # noqa: BLE001
        pass


def _logo(pdf: FPDF, logo: bytes | None, settings: dict | None = None) -> None:
    biz = (settings.get("business_details") or {}) if settings else {}
    if not biz.get("show_logo", True):
        return
    if not logo:
        return
    try:
        pdf.image(BytesIO(logo), x=pdf.l_margin, y=pdf.t_margin, h=16)
        pdf.ln(18)
    except Exception:  # noqa: BLE001
        pass


def _branded_title(pdf: FPDF, settings: dict, title: str) -> None:
    """The document title, in the firm's colour — or reversed out of a band of it.
    No payment status badge is rendered.
    """
    style = _style(settings)
    rgb = _hex_rgb((settings.get("branding") or {}).get("primary_color"))
    heading_size = float((settings.get("typography") or {}).get("heading_size", 16))
    
    _font(pdf, settings, "B", "heading")
    if style["header_band"]:
        band = rgb or (33, 37, 41)
        pdf.set_fill_color(*band)
        pdf.set_text_color(255, 255, 255)
        pdf.cell(
            0, heading_size * 0.75, _s(f"  {title}"), fill=True,
            new_x=XPos.LMARGIN, new_y=YPos.NEXT,
        )
    else:
        if rgb:
            pdf.set_text_color(*rgb)
        pdf.cell(0, 8, _s(title), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_text_color(0, 0, 0)


def _two_column_rows(
    pdf: FPDF, rows: list[tuple[str, str]], column: float, settings: dict | None = None, style: dict | None = None
) -> None:
    """Who it is for on the left, the invoice's own details on the right."""
    style = style or _TEMPLATE_STYLES["classic"]
    _font(pdf, settings, "", "body")
    height = max(style["row_height"] - 1, 4)
    if column < 60:
        for left, right in rows:
            for text in (left, right):
                if text:
                    pdf.cell(0, height, _s(text), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        return
    for left, right in rows:
        pdf.cell(column, height, _s(left), new_x=XPos.RIGHT, new_y=YPos.LAST)
        pdf.set_x(pdf.l_margin + column + 4)
        pdf.cell(column, height, _s(right), new_x=XPos.LMARGIN, new_y=YPos.NEXT)


def _amount_row(
    pdf: FPDF, label: str, value, width: float, height: float = 5, brand_rgb: tuple[int, int, int] | None = None,
    settings: dict | None = None
) -> None:
    if brand_rgb and label in ("Total", "Grand Total"):
        pdf.set_text_color(*brand_rgb)
    pdf.cell(width - 30, height, _s(f"{label}:"), align="R")
    pdf.cell(30, height, _money(value), align="R", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_text_color(0, 0, 0)


def _invoice_footer(
    pdf: FPDF,
    org,
    settings: dict,
    fields: dict,
    signature: bytes | None = None,
    qr: bytes | None = None,
    stamp: bytes | None = None,
) -> None:
    """Bank, UPI, terms, notes, footer line, stamp and signature."""
    pdf.ln(3)
    _font(pdf, settings, "", "body", custom_size=8)
    
    payment_cfg = settings.get("payment_details") or {}
    show_bank = payment_cfg.get("show_bank_details", fields.get("show_bank_details", True))
    show_upi = payment_cfg.get("show_upi_qr", fields.get("show_upi_qr", True))

    footer_cfg = settings.get("footer") or {}
    show_terms = footer_cfg.get("show_terms", fields.get("show_terms", True))
    show_sig = footer_cfg.get("show_signature", fields.get("show_signature", True))
    show_stamp = footer_cfg.get("show_stamp", True)
    terms_text = footer_cfg.get("terms") or settings.get("terms")
    notes_text = footer_cfg.get("notes") or settings.get("notes")
    footer_str = footer_cfg.get("footer_text") or settings.get("footer_text")

    if show_bank and org is not None:
        bank = " | ".join(part for part in (
            org.bank_name,
            f"A/c {org.bank_account_holder}" if org.bank_account_holder else None,
            f"IFSC {org.bank_ifsc}" if org.bank_ifsc else None,
            org.bank_account_details,
        ) if part)
        if bank:
            pdf.multi_cell(0, 4, _s(f"Bank: {bank}"), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    if show_upi and org is not None and (org.upi_id or qr):
        if org.upi_id:
            pdf.multi_cell(0, 4, _s(f"UPI: {org.upi_id}"), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        qr_x = pdf.get_x()
        qr_y = pdf.get_y()
        drawn_qr = False
        if qr:
            try:
                pdf.image(BytesIO(qr), x=qr_x, y=qr_y, w=16, h=16)
                drawn_qr = True
            except Exception:  # noqa: BLE001
                drawn_qr = False
        if drawn_qr:
            pdf.set_xy(qr_x + 19, qr_y + 6)
            _font(pdf, settings, "I", "body", custom_size=8)
            pdf.cell(0, 4, _s("Scan to pay via UPI"), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            pdf.set_xy(qr_x, qr_y + 18)
        else:
            try:
                pdf.rect(qr_x, qr_y, 12, 12, style="D", round_corners=True, corner_radius=1.5)
            except Exception:
                pdf.rect(qr_x, qr_y, 12, 12, style="D")
            pdf.set_xy(qr_x + 15, qr_y + 4)
            _font(pdf, settings, "I", "body", custom_size=8)
            pdf.cell(0, 4, _s("Scan to pay via UPI"), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            pdf.set_xy(qr_x, qr_y + 14)
    if show_terms and terms_text:
        pdf.multi_cell(0, 4, _s(f"Terms: {terms_text}"), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    if notes_text:
        pdf.multi_cell(0, 4, _s(f"Note: {notes_text}"), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    if footer_str:
        pdf.ln(1)
        pdf.multi_cell(0, 4, _s(footer_str), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    if show_sig:
        pdf.ln(4)
        drawn = False
        drawn_stamp = False
        if signature:
            try:
                pdf.image(
                    BytesIO(signature), x=pdf.w - pdf.r_margin - 40, y=pdf.get_y(), h=12
                )
                drawn = True
            except Exception:  # noqa: BLE001
                drawn = False
        if stamp and show_stamp:
            try:
                stamp_h = 13 if pdf.w >= 100 else 10
                if pdf.w >= 100:
                    stamp_x = (pdf.w - pdf.r_margin - 68) if signature else (pdf.w - pdf.r_margin - 40)
                else:
                    stamp_x = (pdf.l_margin + 2) if signature else (pdf.w - pdf.r_margin - 30)
                pdf.image(BytesIO(stamp), x=stamp_x, y=pdf.get_y(), h=stamp_h)
                drawn_stamp = True
            except Exception:  # noqa: BLE001
                drawn_stamp = False
        pdf.ln(13 if (drawn or drawn_stamp) else 8)
        pdf.cell(0, 5, "Authorised signatory", border="T", align="R")


def _get_item_cell_value(item, col_key: str) -> str:
    """Format the text for a given column cell."""
    if col_key == "product":
        return _s(item.product_name)[:30]
    elif col_key == "description":
        desc = ""
        if hasattr(item, "product") and item.product and getattr(item.product, "description", None):
            desc = item.product.description
        return _s(desc)[:30]
    elif col_key == "product_image":
        return ""
    elif col_key == "hsn_sac":
        return _s(item.hsn_code or "-")
    elif col_key == "quantity":
        return f"{item.quantity:g}"
    elif col_key == "uom":
        uom = getattr(item, "uom", None) or (getattr(item.product, "uom", None) if hasattr(item, "product") and item.product else None) or "-"
        return _s(uom)
    elif col_key == "rate":
        return _money(item.unit_price)
    elif col_key == "mrp":
        mrp = getattr(item, "unit_price", 0)
        return _money(mrp)
    elif col_key == "discount":
        return _money(item.discount)
    elif col_key == "tax_rate":
        return f"{(item.tax_rate or 0):g}"
    elif col_key == "tax_amount":
        return _money(item.tax)
    elif col_key == "batch_number":
        return _s(item.batch_number or "-")
    elif col_key == "expiry_date":
        return item.expiry_date.date().isoformat() if item.expiry_date else "-"
    elif col_key == "amount":
        return _money(item.line_total)
    return "-"


def invoice_simple_pdf(
    org,
    customer,
    invoice,
    settings: dict,
    logo: bytes | None = None,
    signature: bytes | None = None,
    qr: bytes | None = None,
    stamp: bytes | None = None,
    letterhead: bytes | None = None,
    item_images: dict[str, bytes] | None = None,
) -> bytes:
    """The short customer copy.
    No payment status badge, row, or balance due rows are printed.
    """
    fields = settings.get("fields") or {}
    style = _style(settings)
    pdf = _invoice_pdf_page(settings)
    _letterhead(pdf, letterhead, settings)
    _logo(pdf, logo, settings)
    _org_header(pdf, org, settings)
    _branded_title(pdf, settings, "INVOICE")
    pdf.ln(1)

    width = _usable_width(pdf)
    column = (width - 4) / 2
    
    party_cfg = settings.get("party_details") or {}
    inv_cfg = settings.get("invoice_details") or {}

    name = (customer.business_name or customer.name) if customer else "Cash Customer"
    left_rows = []
    if party_cfg.get("show_customer_name", True):
        left_rows.append(name)
    if party_cfg.get("show_customer_phone", True) and customer and customer.phone:
        left_rows.append(f"Phone: {customer.phone}")

    right_rows = []
    if inv_cfg.get("show_invoice_number", True):
        right_rows.append(f"Invoice No: {invoice.invoice_number}")
    if inv_cfg.get("show_invoice_date", True):
        right_rows.append(f"Date: {invoice.invoice_date.date().isoformat()}")
    if inv_cfg.get("show_order_reference", True):
        right_rows.append(f"Order Ref: {invoice.order.order_number}" if invoice.order else "Direct Sale")
    if inv_cfg.get("show_due_date", True) and invoice.due_date:
        right_rows.append(f"Due Date: {invoice.due_date.date().isoformat()}")

    max_len = max(len(left_rows), len(right_rows))
    left_rows += [""] * (max_len - len(left_rows))
    right_rows += [""] * (max_len - len(right_rows))
    combined_rows = list(zip(left_rows, right_rows))

    _two_column_rows(pdf, combined_rows, column, settings=settings, style=style)
    pdf.ln(4)

    shares = [0.52, 0.12, 0.16, 0.20]
    widths = [round(width * share, 2) for share in shares]
    _font(pdf, settings, "B", "table")
    for w, header, align in zip(widths, ["Item", "Qty", "Rate", "Amount"], "LRRR"):
        pdf.cell(w, 7, header, border=1, align=align)
    pdf.ln(7)

    _font(pdf, settings, "", "table")
    for item in invoice.items:
        cells = [
            _s(item.product_name)[:44],
            f"{item.quantity:g}",
            _money(item.unit_price),
            _money(item.line_total),
        ]
        for w, text, align in zip(widths, cells, "LRRR"):
            pdf.cell(w, 6, text, border=1, align=align)
        pdf.ln(6)

    brand_rgb = _hex_rgb((settings.get("branding") or {}).get("primary_color"))
    pdf.ln(2)
    _font(pdf, settings, "", "body")
    _amount_row(pdf, "Subtotal", invoice.subtotal, width, settings=settings)
    if invoice.discount:
        _amount_row(pdf, "Discount", -(invoice.discount or 0), width, settings=settings)
    if invoice.tax:
        _amount_row(pdf, "Tax", invoice.tax, width, settings=settings)
    _font(pdf, settings, "B", "body", offset=2)
    _amount_row(pdf, "Total", invoice.total, width, height=7, brand_rgb=brand_rgb, settings=settings)

    # The short copy never carries bank details; everything else is the firm's choice.
    _invoice_footer(
        pdf, org, settings, {**fields, "show_bank_details": False},
        signature=signature, qr=qr, stamp=stamp,
    )

    # Extra lines for thermal
    template = str(settings.get("template") or "").strip().lower()
    if template == "thermal":
        thermal_cfg = settings.get("thermal_print") or {}
        extra = int(thermal_cfg.get("extra_lines") or 0)
        if extra > 0:
            pdf.ln(extra * 4)

    return bytes(pdf.output())


def invoice_detailed_pdf(
    org,
    customer,
    invoice,
    settings: dict,
    logo: bytes | None = None,
    signature: bytes | None = None,
    qr: bytes | None = None,
    stamp: bytes | None = None,
    letterhead: bytes | None = None,
    item_images: dict[str, bytes] | None = None,
) -> bytes:
    """The full tax invoice: dynamic typography, item table columns, visibility toggles,
    custom paper and orientation, with all payment status/paid/balance elements omitted.
    """
    fields = settings.get("fields") or {}
    style = _style(settings)
    pdf = _invoice_pdf_page(settings)
    _letterhead(pdf, letterhead, settings)
    _logo(pdf, logo, settings)
    _org_header(pdf, org, settings)

    biz_cfg = settings.get("business_details") or {}
    if fields.get("show_company_gstin", True) and biz_cfg.get("show_gstin", True) and org is not None and (org.gst_number or getattr(org, "gstin_pan", None)):
        _font(pdf, settings, "", "body", custom_size=9)
        pdf.cell(
            0, 5, _s(f"GSTIN / PAN: {org.gst_number or org.gstin_pan}"),
            new_x=XPos.LMARGIN, new_y=YPos.NEXT,
        )
    _branded_title(pdf, settings, "TAX INVOICE")

    pdf.ln(1)

    width = _usable_width(pdf)
    column = (width - 4) / 2

    party_cfg = settings.get("party_details") or {}
    inv_cfg = settings.get("invoice_details") or {}

    left = []
    if party_cfg.get("show_customer_name", True):
        left.append((customer.business_name or customer.name) if customer else "Cash Customer")
    if party_cfg.get("show_customer_gstin", fields.get("show_customer_gstin", True)):
        gstin = customer.gst_number if customer is not None else None
        left.append(f"GSTIN: {gstin}" if gstin else "GSTIN: Not Provided")
    if party_cfg.get("show_billing_address", fields.get("show_billing_address", True)):
        billing = invoice.billing_address or (customer.billing_address if customer else None)
        left.append(f"Bill to: {str(billing or '-')[:44]}")
    if party_cfg.get("show_shipping_address", fields.get("show_shipping_address", True)):
        shipping = customer.delivery_address if customer is not None else None
        left.append(f"Ship to: {str(shipping or '-')[:44]}")
    if party_cfg.get("show_customer_phone", True) and customer and customer.phone:
        left.append(f"Phone: {customer.phone}")

    right = []
    if inv_cfg.get("show_invoice_number", True):
        right.append(f"Invoice No: {invoice.invoice_number}")
    if inv_cfg.get("show_invoice_date", True):
        right.append(f"Date: {invoice.invoice_date.date().isoformat()}")
    if inv_cfg.get("show_order_reference", True):
        right.append(f"Order Ref: {invoice.order.order_number}" if invoice.order else "Direct Sale")
    if inv_cfg.get("show_due_date", True):
        right.append(f"Due Date: {invoice.due_date.date().isoformat() if invoice.due_date else '-'}")
    if inv_cfg.get("show_po_number", False) and getattr(invoice, "order", None) and getattr(invoice.order, "customer_po_number", None):
        right.append(f"PO No: {invoice.order.customer_po_number}")
    if inv_cfg.get("show_eway_bill_number", False) and getattr(invoice, "eway_bill_number", None):
        right.append(f"E-Way Bill: {invoice.eway_bill_number}")
    if inv_cfg.get("show_vehicle_number", False) and getattr(invoice, "vehicle_number", None):
        right.append(f"Vehicle: {invoice.vehicle_number}")

    pad = [""] * abs(len(left) - len(right))
    rows = list(zip(left + pad if len(left) < len(right) else left,
                    right + pad if len(right) < len(left) else right))

    _font(pdf, settings, "B", "body", offset=1)
    if column < 60:
        pdf.cell(0, 5, "BILLED TO:", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    else:
        pdf.cell(column, 5, "BILLED TO:", new_x=XPos.RIGHT, new_y=YPos.LAST)
        pdf.set_x(pdf.l_margin + column + 4)
        pdf.cell(column, 5, "INVOICE DETAILS:", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    _two_column_rows(pdf, rows, column, settings=settings, style=style)
    pdf.ln(style["gap"])

    # Item Table configuration
    item_table_cfg = settings.get("item_table") or {}
    configured_cols = item_table_cfg.get("columns")
    if not configured_cols:
        configured_cols = ["product", "hsn_sac", "quantity", "rate", "discount", "tax_rate", "tax_amount", "amount"]

    show_images = item_table_cfg.get("show_product_image", False)
    active_cols = []
    for c in configured_cols:
        if c == "product_image" and not show_images:
            continue
        if c in COLUMN_WEIGHTS:
            active_cols.append(c)

    # Ensure product and amount are present
    if "product" not in active_cols:
        active_cols.insert(0, "product")
    if "amount" not in active_cols:
        active_cols.append("amount")

    total_weight = sum(COLUMN_WEIGHTS[c] for c in active_cols)
    scale = width / total_weight
    col_widths = [round(COLUMN_WEIGHTS[c] * scale, 2) for c in active_cols]

    # Adjust rounding discrepancy
    diff = width - sum(col_widths)
    if col_widths:
        col_widths[0] += round(diff, 2)

    has_img_col = ("product_image" in active_cols) and show_images
    row_height = max(style["row_height"], 12) if has_img_col else style["row_height"]

    _font(pdf, settings, "B", "table")
    for w, c in zip(col_widths, active_cols):
        pdf.cell(w, style["row_height"] + 1, COLUMN_HEADERS[c], border=style["border"], align=COLUMN_ALIGNS[c])
    pdf.ln(style["row_height"] + 1)

    _font(pdf, settings, "", "table")
    for item in invoice.items:
        cell_y = pdf.get_y()
        for w, c in zip(col_widths, active_cols):
            cell_x = pdf.get_x()
            if c == "product_image":
                pdf.cell(w, row_height, "", border=style["border"])
                if item_images and item.id in item_images:
                    try:
                        img_bytes = item_images[item.id]
                        thumb = min(w - 2, row_height - 2, 10)
                        if thumb > 2:
                            ix = cell_x + (w - thumb) / 2
                            iy = cell_y + (row_height - thumb) / 2
                            pdf.image(BytesIO(img_bytes), x=ix, y=iy, w=thumb, h=thumb)
                    except Exception:  # noqa: BLE001
                        pass
                pdf.set_xy(cell_x + w, cell_y)
            else:
                val = _get_item_cell_value(item, c)
                pdf.cell(w, row_height, val, border=style["border"], align=COLUMN_ALIGNS[c])
        pdf.ln(row_height)

    brand_rgb = _hex_rgb((settings.get("branding") or {}).get("primary_color"))
    pdf.ln(2)
    _font(pdf, settings, "", "body")
    _amount_row(pdf, "Subtotal", invoice.subtotal, width, settings=settings)
    if fields.get("show_discount", True) and invoice.discount:
        _amount_row(pdf, "Discount", -(invoice.discount or 0), width, settings=settings)
    if fields.get("show_tax_amount", True) and invoice.tax:
        _amount_row(pdf, "Tax", invoice.tax, width, settings=settings)
    if invoice.additional_charges:
        _amount_row(pdf, "Additional Charges", invoice.additional_charges, width, settings=settings)
    if invoice.round_off:
        _amount_row(pdf, "Round Off", invoice.round_off, width, settings=settings)
    _font(pdf, settings, "B", "body", offset=2)
    _amount_row(pdf, "Grand Total", invoice.total, width, height=7, brand_rgb=brand_rgb, settings=settings)

    _invoice_footer(pdf, org, settings, fields, signature=signature, qr=qr, stamp=stamp)

    # Extra lines for thermal
    template = str(settings.get("template") or "").strip().lower()
    if template == "thermal":
        thermal_cfg = settings.get("thermal_print") or {}
        extra = int(thermal_cfg.get("extra_lines") or 0)
        if extra > 0:
            pdf.ln(extra * 4)

    return bytes(pdf.output())


INVOICE_PDF_FORMATS = {"simple": invoice_simple_pdf, "detailed": invoice_detailed_pdf}
