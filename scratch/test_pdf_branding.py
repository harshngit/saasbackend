import sys, os
sys.path.insert(0, os.path.abspath("."))
from datetime import datetime, timezone
from app.core.pdf_docs import invoice_simple_pdf, invoice_detailed_pdf

class Dummy:
    pass

def test_pdf_branding():
    org = Dummy()
    org.name = 'Test Corp'
    org.gst_number = '27AAACC4175D1ZS'
    org.gstin_pan = None
    org.address = '123 Main St'
    org.phone = '9999999999'
    org.upi_id = 'test@upi'
    org.bank_name = None
    org.bank_account_holder = None
    org.bank_ifsc = None
    org.bank_account_details = None

    cust = Dummy()
    cust.name = 'John'
    cust.business_name = 'John Co'
    cust.phone = '8888888888'
    cust.billing_address = 'Billing'
    cust.delivery_address = 'Shipping'
    cust.gst_number = '27BBBCC4175D1ZS'

    inv = Dummy()
    inv.invoice_number = 'INV-001'
    inv.invoice_date = datetime.now(timezone.utc)
    inv.due_date = datetime.now(timezone.utc)
    inv.order = None
    inv.status = 'unpaid'
    inv.additional_charges = 0
    inv.round_off = 0

    item = Dummy()
    item.product_name = 'Widget'
    item.quantity = 2
    item.unit_price = 100.0
    item.line_total = 200.0
    item.discount = 0
    item.tax = 0
    item.tax_rate = 0
    item.hsn_code = '1234'
    item.batch_number = 'B1'
    item.expiry_date = datetime.now(timezone.utc)
    inv.items = [item]

    inv.subtotal = 200.0
    inv.discount = 0
    inv.tax = 0
    inv.total = 200.0
    inv.amount_paid = 0.0

    settings = {
        'template': 'modern',
        'branding': {'primary_color': '#1E40AF'},
        'fields': {'show_company_gstin': True, 'show_upi_qr': True}
    }

    pdf1 = invoice_simple_pdf(org, cust, inv, settings)
    pdf2 = invoice_detailed_pdf(org, cust, inv, settings)
    
    assert len(pdf1) > 100, "Simple PDF generated"
    assert len(pdf2) > 100, "Detailed PDF generated"
    print(f"SUCCESS: Simple PDF ({len(pdf1)} bytes), Detailed PDF ({len(pdf2)} bytes)")

if __name__ == '__main__':
    test_pdf_branding()
