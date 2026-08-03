"""On-the-fly PDF generation for receipts (and later, invoices)."""

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
