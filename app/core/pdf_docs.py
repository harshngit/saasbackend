"""On-the-fly PDF generation for receipts, invoices, and delivery challans."""

from datetime import datetime
from io import BytesIO

from fpdf import FPDF
from fpdf.enums import XPos, YPos


def _s(text) -> str:
    """Make text safe for fpdf's latin-1 core fonts (replace unencodable chars)."""
    return str("" if text is None else text).encode("latin-1", "replace").decode("latin-1")


def _org_header(pdf: FPDF, org) -> None:
    pdf.set_font("Helvetica", "B", 16)
    pdf.cell(0, 9, _s(org.name if org else "Company"), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_font("Helvetica", size=9)
    if org and org.gst_number:
        pdf.cell(0, 5, _s(f"GSTIN: {org.gst_number}"), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    if org and org.address:
        pdf.cell(0, 5, _s(str(org.address)[:90]), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    if org and org.phone:
        pdf.cell(0, 5, _s(f"Phone: {org.phone}"), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
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

    pdf.cell(0, 6, _s(f"Received from: {customer.name}"
             + (f" ({customer.business_name})" if customer.business_name else "")),
             new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(2)

    pdf.set_font("Helvetica", "B", 11)
    pdf.cell(60, 8, "Amount Received", border=1)
    pdf.set_font("Helvetica", size=11)
    pdf.cell(60, 8, _s(f"Rs {payment.amount:,.2f}"), border=1, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_font("Helvetica", size=10)
    pdf.cell(60, 7, "Payment Mode", border=1)
    pdf.cell(60, 7, _s(payment.payment_mode), border=1, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    if payment.reference:
        pdf.cell(60, 7, "Reference", border=1)
        pdf.cell(60, 7, _s(payment.reference), border=1, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.cell(60, 7, "Outstanding After", border=1)
    pdf.cell(60, 7, _s(f"Rs {(customer.outstanding_balance or 0):,.2f}"), border=1, new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    pdf.ln(8)
    pdf.set_font("Helvetica", "I", 8)
    pdf.cell(0, 5, f"Generated on {datetime.utcnow().date().isoformat()} - computer-generated receipt.",
             new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    return bytes(pdf.output())


def delivery_receipt_pdf(org, customer, order) -> bytes:
    """A clean PDF delivery receipt / challan for the delivery partner."""
    pdf = FPDF()
    pdf.add_page()
    _org_header(pdf, org)

    # Title
    pdf.set_font("Helvetica", "B", 14)
    pdf.cell(0, 8, "DELIVERY RECEIPT / CHALLAN", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(2)

    # Delivery & Order details
    pdf.set_font("Helvetica", size=9)
    col_width = 90
    
    pdf.set_font("Helvetica", "B", 10)
    pdf.cell(col_width, 5, "DELIVER TO:", new_x=XPos.RIGHT, new_y=YPos.LAST)
    pdf.set_x(col_width + 10)
    pdf.cell(col_width, 5, "DELIVERY DETAILS:", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    
    pdf.set_font("Helvetica", size=9)
    # Row 1
    pdf.cell(col_width, 5, _s(customer.business_name or customer.name), new_x=XPos.RIGHT, new_y=YPos.LAST)
    pdf.set_x(col_width + 10)
    pdf.cell(col_width, 5, _s(f"Order No: {order.order_number}"), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    
    # Row 2
    addr = (customer.delivery_address or customer.billing_address or "No Address Provided")[:45]
    pdf.cell(col_width, 5, _s(f"Address: {addr}"), new_x=XPos.RIGHT, new_y=YPos.LAST)
    pdf.set_x(col_width + 10)
    pdf.cell(col_width, 5, _s(f"Date: {datetime.utcnow().date().isoformat()}"), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    
    # Row 3
    phone = f"Phone: {customer.phone}" if customer.phone else "Phone: N/A"
    pdf.cell(col_width, 5, _s(phone), new_x=XPos.RIGHT, new_y=YPos.LAST)
    pdf.set_x(col_width + 10)
    pdf.cell(col_width, 5, _s(f"Outstanding Balance: Rs {(customer.outstanding_balance or 0):,.2f}"), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    
    pdf.ln(5)

    # Line Items Table
    pdf.set_font("Helvetica", "B", 9)
    widths = [120, 60]
    headers = ["Item Name", "Quantity"]
    for w, h in zip(widths, headers):
        pdf.cell(w, 7, h, border=1, align="C" if w == 60 else "L")
    pdf.ln(7)
    
    pdf.set_font("Helvetica", size=9)
    for item in order.items:
        pdf.cell(widths[0], 7, _s(item.product_name[:60]), border=1)
        pdf.cell(widths[1], 7, _s(str(item.quantity)), border=1, align="C", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        
    pdf.ln(10)
    
    # Signature fields
    pdf.set_font("Helvetica", size=9)
    pdf.cell(90, 5, "Received By: __________________________", new_x=XPos.RIGHT, new_y=YPos.LAST)
    pdf.set_x(110)
    pdf.cell(90, 5, "Delivered By: __________________________", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    
    pdf.ln(8)
    pdf.set_font("Helvetica", "I", 8)
    pdf.cell(0, 5, f"Generated on {datetime.utcnow().date().isoformat()} - Delivery Challan.",
             new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    return bytes(pdf.output())


def quotation_pdf(org, customer, quotation) -> bytes:
    """The quotation as the customer receives it: quoted lines, terms and validity.

    A quotation is an offer, not a bill — there is no payment or balance on it.
    """
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
    rows = [
        (
            _s(customer.business_name or customer.name) if customer else "-",
            _s(f"Quotation No: {quotation.quotation_number}"),
        ),
        (
            _s((quotation.billing_address or (customer.billing_address if customer else "") or "")[:45]),
            _s(f"Date: {quotation.quotation_date.date().isoformat() if quotation.quotation_date else '-'}"),
        ),
        (
            _s(f"Phone: {customer.phone}" if customer and customer.phone else "Phone: N/A"),
            _s(f"Valid until: {quotation.valid_until.date().isoformat() if quotation.valid_until else '-'}"),
        ),
        (
            _s(f"GSTIN: {customer.gst_number}" if customer and customer.gst_number else ""),
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
    """The delivery challan / goods-movement note that travels with the vehicle.

    A challan documents goods leaving — it creates no revenue, no receivable and no
    payment, so no totals or balances appear on it.
    """
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
# One invoice record, two printed formats. Both are driven by the firm's invoice
# settings (GET/PATCH /invoice-settings): its paper size, brand colour, logo and the
# fifteen show/hide toggles decide what gets drawn. A field the firm has switched off
# simply does not appear, and no column is hardcoded on.

_PAPER_FORMATS = {"a4": "A4", "a5": "A5", "thermal": (80, 250)}


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


def _invoice_pdf_page(settings: dict) -> FPDF:
    size = _PAPER_FORMATS.get(str(settings.get("paper_size") or "A4").lower(), "A4")
    pdf = FPDF(format=size)
    pdf.add_page()
    return pdf


def _usable_width(pdf: FPDF) -> float:
    return pdf.w - pdf.l_margin - pdf.r_margin


def _logo(pdf: FPDF, logo: bytes | None) -> None:
    """Draw the firm's uploaded logo, if there is one it can read.

    A logo the image library cannot decode must never cost the firm its invoice, so
    a failure here just leaves the letterhead plain.
    """
    if not logo:
        return
    try:
        pdf.image(BytesIO(logo), x=pdf.l_margin, y=pdf.t_margin, h=16)
        pdf.ln(18)
    except Exception:  # noqa: BLE001 - unreadable upload, print without it
        pass


def _branded_title(pdf: FPDF, settings: dict, title: str) -> None:
    rgb = _hex_rgb((settings.get("branding") or {}).get("primary_color"))
    if rgb:
        pdf.set_text_color(*rgb)
    pdf.set_font("Helvetica", "B", 14)
    pdf.cell(0, 8, _s(title), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_text_color(0, 0, 0)


def _two_column_rows(pdf: FPDF, rows: list[tuple[str, str]], column: float) -> None:
    pdf.set_font("Helvetica", size=9)
    for left, right in rows:
        pdf.cell(column, 5, _s(left), new_x=XPos.RIGHT, new_y=YPos.LAST)
        pdf.set_x(pdf.l_margin + column + 4)
        pdf.cell(column, 5, _s(right), new_x=XPos.LMARGIN, new_y=YPos.NEXT)


def _amount_row(pdf: FPDF, label: str, value, width: float, height: float = 5) -> None:
    pdf.cell(width - 30, height, _s(f"{label}:"), align="R")
    pdf.cell(30, height, _money(value), align="R", new_x=XPos.LMARGIN, new_y=YPos.NEXT)


def _outstanding(invoice) -> float:
    return round((invoice.total or 0) - (invoice.amount_paid or 0), 2)


def _invoice_footer(pdf: FPDF, org, settings: dict, fields: dict) -> None:
    """Bank, UPI, terms, notes, footer line and signature — each one a toggle."""
    pdf.ln(3)
    pdf.set_font("Helvetica", size=8)
    if fields.get("show_bank_details") and org is not None:
        bank = " | ".join(part for part in (
            org.bank_name,
            f"A/c {org.bank_account_holder}" if org.bank_account_holder else None,
            f"IFSC {org.bank_ifsc}" if org.bank_ifsc else None,
            org.bank_account_details,
        ) if part)
        if bank:
            pdf.multi_cell(0, 4, _s(f"Bank: {bank}"), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    if fields.get("show_upi_qr") and org is not None and org.upi_id:
        pdf.multi_cell(0, 4, _s(f"UPI: {org.upi_id}"), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    if fields.get("show_terms") and settings.get("terms"):
        pdf.multi_cell(0, 4, _s(f"Terms: {settings['terms']}"), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    if settings.get("notes"):
        pdf.multi_cell(0, 4, _s(f"Note: {settings['notes']}"), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    if settings.get("footer_text"):
        pdf.ln(1)
        pdf.multi_cell(0, 4, _s(settings["footer_text"]), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    if fields.get("show_signature"):
        pdf.ln(8)
        pdf.cell(0, 5, "Authorised signatory", border="T", align="R")


def invoice_simple_pdf(org, customer, invoice, settings: dict, logo: bytes | None = None) -> bytes:
    """The short customer copy: what was bought, what is owed, when it is due.

    No tax breakdown, no GSTINs, no addresses — this is the one that goes out over
    WhatsApp, so it stays to invoice number, order reference, items, total, payment
    status and due date.
    """
    fields = settings.get("fields") or {}
    pdf = _invoice_pdf_page(settings)
    _logo(pdf, logo)
    _org_header(pdf, org)
    _branded_title(pdf, settings, "INVOICE")
    pdf.ln(1)

    width = _usable_width(pdf)
    column = (width - 4) / 2
    due = invoice.due_date.date().isoformat() if invoice.due_date else "-"
    name = (customer.business_name or customer.name) if customer else "Cash Customer"
    _two_column_rows(pdf, [
        (name, f"Invoice No: {invoice.invoice_number}"),
        (
            f"Phone: {customer.phone}" if customer is not None and customer.phone else "",
            f"Date: {invoice.invoice_date.date().isoformat()}",
        ),
        ("", f"Order Ref: {invoice.order.order_number}" if invoice.order else "Direct Sale"),
        ("", f"Due Date: {due}"),
        ("", f"Payment Status: {(invoice.status or 'unpaid').replace('_', ' ').title()}"),
    ], column)
    pdf.ln(4)

    shares = [0.52, 0.12, 0.16, 0.20]
    widths = [round(width * share, 2) for share in shares]
    pdf.set_font("Helvetica", "B", 9)
    for w, header, align in zip(widths, ["Item", "Qty", "Rate", "Amount"], "LRRR"):
        pdf.cell(w, 7, header, border=1, align=align)
    pdf.ln(7)

    pdf.set_font("Helvetica", size=9)
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

    pdf.ln(2)
    pdf.set_font("Helvetica", size=9)
    _amount_row(pdf, "Subtotal", invoice.subtotal, width)
    if invoice.discount:
        _amount_row(pdf, "Discount", -(invoice.discount or 0), width)
    if invoice.tax:
        _amount_row(pdf, "Tax", invoice.tax, width)
    pdf.set_font("Helvetica", "B", 11)
    _amount_row(pdf, "Total", invoice.total, width, height=7)
    pdf.set_font("Helvetica", size=9)
    _amount_row(pdf, "Paid", invoice.amount_paid, width)
    _amount_row(pdf, "Balance Due", _outstanding(invoice), width)

    # The short copy never carries bank details; everything else is the firm's choice.
    _invoice_footer(pdf, org, settings, {**fields, "show_bank_details": False})
    return bytes(pdf.output())


def invoice_detailed_pdf(org, customer, invoice, settings: dict, logo: bytes | None = None) -> bytes:
    """The full tax invoice: GSTINs, both addresses, HSN/SAC and the tax split.

    Columns are assembled from the firm's toggles and then scaled to the paper, so a
    firm that does not print HSN or MRP gets a table without those columns rather
    than empty ones.
    """
    fields = settings.get("fields") or {}
    pdf = _invoice_pdf_page(settings)
    _logo(pdf, logo)
    _org_header(pdf, org)
    if fields.get("show_company_gstin") and org is not None and (org.gst_number or org.gstin_pan):
        pdf.set_font("Helvetica", size=9)
        pdf.cell(
            0, 5, _s(f"GSTIN / PAN: {org.gst_number or org.gstin_pan}"),
            new_x=XPos.LMARGIN, new_y=YPos.NEXT,
        )
    _branded_title(pdf, settings, "TAX INVOICE")
    pdf.ln(1)

    width = _usable_width(pdf)
    column = (width - 4) / 2

    left = [(customer.business_name or customer.name) if customer else "Cash Customer"]
    if fields.get("show_customer_gstin"):
        gstin = customer.gst_number if customer is not None else None
        left.append(f"GSTIN: {gstin}" if gstin else "GSTIN: Not Provided")
    if fields.get("show_billing_address"):
        billing = invoice.billing_address or (customer.billing_address if customer else None)
        left.append(f"Bill to: {str(billing or '-')[:44]}")
    if fields.get("show_shipping_address"):
        shipping = customer.delivery_address if customer is not None else None
        left.append(f"Ship to: {str(shipping or '-')[:44]}")

    right = [
        f"Invoice No: {invoice.invoice_number}",
        f"Date: {invoice.invoice_date.date().isoformat()}",
        f"Order Ref: {invoice.order.order_number}" if invoice.order else "Direct Sale",
        f"Due Date: {invoice.due_date.date().isoformat() if invoice.due_date else '-'}",
        f"Payment Status: {(invoice.status or 'unpaid').replace('_', ' ').title()}",
    ]
    pad = [""] * abs(len(left) - len(right))
    rows = list(zip(left + pad if len(left) < len(right) else left,
                    right + pad if len(right) < len(left) else right))

    pdf.set_font("Helvetica", "B", 10)
    pdf.cell(column, 5, "BILLED TO:", new_x=XPos.RIGHT, new_y=YPos.LAST)
    pdf.set_x(pdf.l_margin + column + 4)
    pdf.cell(column, 5, "INVOICE DETAILS:", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    _two_column_rows(pdf, rows, column)
    pdf.ln(4)

    columns: list[tuple[str, float, str]] = [("Item", 44, "L")]
    if fields.get("show_hsn_sac"):
        columns.append(("HSN/SAC", 18, "C"))
    if fields.get("show_batch_number"):
        columns.append(("Batch", 16, "C"))
    if fields.get("show_expiry_date"):
        columns.append(("Expiry", 18, "C"))
    if fields.get("show_mrp"):
        columns.append(("MRP", 18, "R"))
    columns.append(("Qty", 13, "R"))
    columns.append(("Rate", 20, "R"))
    if fields.get("show_discount"):
        columns.append(("Disc", 16, "R"))
    if fields.get("show_tax_rate"):
        columns.append(("Tax %", 14, "R"))
    if fields.get("show_tax_amount"):
        columns.append(("Tax", 18, "R"))
    columns.append(("Amount", 24, "R"))

    scale = width / sum(w for _, w, _ in columns)
    widths = [round(w * scale, 2) for _, w, _ in columns]

    pdf.set_font("Helvetica", "B", 8)
    for w, (header, _, align) in zip(widths, columns):
        pdf.cell(w, 7, header, border=1, align=align)
    pdf.ln(7)

    pdf.set_font("Helvetica", size=8)
    for item in invoice.items:
        # Batch and expiry come off the lot the goods actually left the shelf in.
        values = {
            "Item": _s(item.product_name)[:30],
            "HSN/SAC": _s(item.hsn_code or "-"),
            "Batch": _s(item.batch_number or "-"),
            "Expiry": item.expiry_date.date().isoformat() if item.expiry_date else "-",
            "MRP": _money(item.unit_price),
            "Qty": f"{item.quantity:g}",
            "Rate": _money(item.unit_price),
            "Disc": _money(item.discount),
            "Tax %": f"{(item.tax_rate or 0):g}",
            "Tax": _money(item.tax),
            "Amount": _money(item.line_total),
        }
        for w, (header, _, align) in zip(widths, columns):
            pdf.cell(w, 6, values[header], border=1, align=align)
        pdf.ln(6)

    pdf.ln(2)
    pdf.set_font("Helvetica", size=9)
    _amount_row(pdf, "Subtotal", invoice.subtotal, width)
    if fields.get("show_discount") and invoice.discount:
        _amount_row(pdf, "Discount", -(invoice.discount or 0), width)
    if fields.get("show_tax_amount"):
        _amount_row(pdf, "Tax", invoice.tax, width)
    if invoice.additional_charges:
        _amount_row(pdf, "Additional Charges", invoice.additional_charges, width)
    if invoice.round_off:
        _amount_row(pdf, "Round Off", invoice.round_off, width)
    pdf.set_font("Helvetica", "B", 11)
    _amount_row(pdf, "Grand Total", invoice.total, width, height=7)
    pdf.set_font("Helvetica", size=9)
    _amount_row(pdf, "Paid", invoice.amount_paid, width)
    _amount_row(pdf, "Balance Due", _outstanding(invoice), width)

    _invoice_footer(pdf, org, settings, fields)
    return bytes(pdf.output())


INVOICE_PDF_FORMATS = {"simple": invoice_simple_pdf, "detailed": invoice_detailed_pdf}
