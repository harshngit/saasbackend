"""On-the-fly PDF generation for receipts, invoices, and delivery challans."""

from datetime import datetime

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


def invoice_pdf(org, customer, invoice) -> bytes:
    """A clean, professional GST Tax Invoice using FPDF."""
    pdf = FPDF()
    pdf.add_page()
    _org_header(pdf, org)

    # Title
    pdf.set_font("Helvetica", "B", 14)
    pdf.cell(0, 8, "TAX INVOICE", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(2)

    # Billing & Invoice Info columns
    pdf.set_font("Helvetica", size=9)
    col_width = 90
    
    # Save Y position
    start_y = pdf.get_y()
    
    # Left Column: Billed To
    pdf.set_font("Helvetica", "B", 10)
    pdf.cell(col_width, 5, "BILLED TO:", new_x=XPos.RIGHT, new_y=YPos.LAST)
    
    # Right Column: Invoice Details
    pdf.set_x(col_width + 10)
    pdf.cell(col_width, 5, "INVOICE DETAILS:", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    
    # Content rows
    pdf.set_font("Helvetica", size=9)
    # Row 1
    pdf.cell(col_width, 5, _s(customer.business_name or customer.name), new_x=XPos.RIGHT, new_y=YPos.LAST)
    pdf.set_x(col_width + 10)
    pdf.cell(col_width, 5, _s(f"Invoice No: {invoice.invoice_number}"), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    
    # Row 2
    cust_gst = f"GSTIN: {customer.gst_number}" if customer.gst_number else "GSTIN: Not Provided"
    pdf.cell(col_width, 5, _s(cust_gst), new_x=XPos.RIGHT, new_y=YPos.LAST)
    pdf.set_x(col_width + 10)
    pdf.cell(col_width, 5, _s(f"Date: {invoice.invoice_date.date().isoformat()}"), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    
    # Row 3
    addr = (customer.billing_address or "")[:45]
    pdf.cell(col_width, 5, _s(addr), new_x=XPos.RIGHT, new_y=YPos.LAST)
    pdf.set_x(col_width + 10)
    order_num = invoice.order.order_number if invoice.order else "Direct Sale"
    pdf.cell(col_width, 5, _s(f"Order Ref: {order_num}"), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    
    pdf.ln(5)

    # Line Items Table. HSN/SAC is a statutory column on a GST invoice, so it sits
    # right after the description — the product column gives up the width for it.
    pdf.set_font("Helvetica", "B", 9)
    widths = [58, 22, 25, 15, 20, 15, 35]
    headers = ["Product / Service", "HSN/SAC", "Unit Price", "Qty", "Discount", "GST %", "Total"]

    for w, h in zip(widths, headers):
        pdf.cell(w, 7, h, border=1, align="C" if w != 58 else "L")
    pdf.ln(7)
    
    pdf.set_font("Helvetica", size=9)
    for item in invoice.items:
        # Product Name
        pdf.cell(widths[0], 7, _s(item.product_name[:30]), border=1)
        # HSN / SAC — copied from the product when the invoice was raised
        pdf.cell(widths[1], 7, _s(item.hsn_code or "-"), border=1, align="C")
        # Unit Price
        pdf.cell(widths[2], 7, _s(f"Rs {item.unit_price:,.2f}"), border=1, align="R")
        # Qty
        pdf.cell(widths[3], 7, _s(str(item.quantity)), border=1, align="C")
        # Discount
        pdf.cell(widths[4], 7, _s(f"Rs {item.discount:,.2f}"), border=1, align="R")
        # GST Rate (derived from the line's tax; falls back to 18%)
        gst_pct = f"{int(item.tax / (item.line_total - item.tax) * 100)}%" if (item.line_total - item.tax) > 0 and item.tax > 0 else "18%"
        pdf.cell(widths[5], 7, _s(gst_pct), border=1, align="C")
        # Total
        pdf.cell(widths[6], 7, _s(f"Rs {item.line_total:,.2f}"), border=1, align="R", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        
    pdf.ln(3)

    # Determine CGST/SGST/IGST breakdown from GSTIN state codes (first 2 digits)
    state_org = org.gst_number[:2] if org and org.gst_number and len(org.gst_number) >= 2 else None
    state_cust = customer.gst_number[:2] if customer and customer.gst_number and len(customer.gst_number) >= 2 else None
    
    # Default to intra-state (CGST + SGST) if either GSTIN is missing or state codes match
    is_interstate = state_org and state_cust and state_org != state_cust
    
    total_tax = invoice.tax
    cgst, sgst, igst = 0.0, 0.0, 0.0
    if is_interstate:
        igst = total_tax
    else:
        cgst = round(total_tax / 2, 2)
        sgst = round(total_tax / 2, 2)
        
    # Totals section
    pdf.set_font("Helvetica", size=9)
    totals_col = 135
    val_col = 55
    
    # Subtotal
    pdf.set_x(totals_col)
    pdf.cell(30, 5, "Subtotal:", align="R")
    pdf.cell(val_col, 5, _s(f"Rs {invoice.subtotal:,.2f}"), align="R", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    
    # Discount
    if invoice.discount > 0:
        pdf.set_x(totals_col)
        pdf.cell(30, 5, "Discount:", align="R")
        pdf.cell(val_col, 5, _s(f"- Rs {invoice.discount:,.2f}"), align="R", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        
    # GST Taxes
    if is_interstate:
        if igst > 0:
            pdf.set_x(totals_col)
            pdf.cell(30, 5, "IGST:", align="R")
            pdf.cell(val_col, 5, _s(f"Rs {igst:,.2f}"), align="R", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    else:
        if cgst > 0 or sgst > 0:
            pdf.set_x(totals_col)
            pdf.cell(30, 5, "CGST:", align="R")
            pdf.cell(val_col, 5, _s(f"Rs {cgst:,.2f}"), align="R", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            pdf.set_x(totals_col)
            pdf.cell(30, 5, "SGST:", align="R")
            pdf.cell(val_col, 5, _s(f"Rs {sgst:,.2f}"), align="R", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            
    # Total
    pdf.set_font("Helvetica", "B", 10)
    pdf.set_x(totals_col)
    pdf.cell(30, 6, "Total (incl. GST):", align="R")
    pdf.cell(val_col, 6, _s(f"Rs {invoice.total:,.2f}"), align="R", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    
    # Amount Paid & Due
    pdf.set_font("Helvetica", size=9)
    if invoice.amount_paid > 0:
        pdf.set_x(totals_col)
        pdf.cell(30, 5, "Amount Paid:", align="R")
        pdf.cell(val_col, 5, _s(f"Rs {invoice.amount_paid:,.2f}"), align="R", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        
    due = round(invoice.total - invoice.amount_paid, 2)
    pdf.set_font("Helvetica", "B", 9)
    pdf.set_x(totals_col)
    pdf.cell(30, 5, "Balance Due:", align="R")
    pdf.cell(val_col, 5, _s(f"Rs {due:,.2f}"), align="R", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    
    if invoice.notes:
        pdf.ln(4)
        pdf.set_font("Helvetica", "I", size=8)
        pdf.cell(0, 5, _s(f"Notes: {invoice.notes}"), new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    # Footer
    pdf.ln(8)
    pdf.set_font("Helvetica", "I", 8)
    pdf.cell(0, 5, f"Generated on {datetime.utcnow().date().isoformat()} - computer-generated tax invoice.",
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
