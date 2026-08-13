"""Financial / transaction report builders.

Each builder returns {"summary": {...}, "rows": [ {...}, ... ]} so the frontend
renders a stat-card header + a data table with one consistent pattern.
"""

from datetime import date, datetime, time, timezone

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.models import (
    Customer,
    CustomerPayment,
    Expense,
    PurchaseInvoice,
    SalesOrder,
    Supplier,
    SupplierPayment,
)

REPORT_TYPES = {
    "daily-transaction", "sales", "purchase", "customer-outstanding",
    "supplier-outstanding", "payment-collection", "expense", "cash-collection",
    "gst-summary", "sales-return", "purchase-return", "profit-loss",
}

# Order statuses that count as a realised sale. Sourced from the workflow module so
# the reports, the dashboards and the order router can never disagree about what a
# sale is — see app/core/workflow.py for the two status axes.
from app.core.workflow import SALE_ORDER_STATUSES as SALE_STATUSES  # noqa: E402


def _range(date_from: str | None, date_to: str | None) -> tuple[datetime | None, datetime | None]:
    try:
        df = datetime.combine(date.fromisoformat(date_from), time.min, tzinfo=timezone.utc) if date_from else None
        dt = datetime.combine(date.fromisoformat(date_to), time.max, tzinfo=timezone.utc) if date_to else None
    except ValueError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Dates must be YYYY-MM-DD")
    if df and dt and df > dt:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="date_from cannot be after date_to")
    return df, dt


def _between(query, col, df, dt):
    if df is not None:
        query = query.filter(col >= df)
    if dt is not None:
        query = query.filter(col <= dt)
    return query


def _customer_names(db: Session, org_id: str) -> dict:
    return {c.id: (c.business_name or c.name) for c in db.query(Customer).filter(Customer.organization_id == org_id)}


def _supplier_names(db: Session, org_id: str) -> dict:
    return {s.id: s.name for s in db.query(Supplier).filter(Supplier.organization_id == org_id)}


# --------------------------------- builders ---------------------------------


def _sales(db, org_id, df, dt):
    q = _between(db.query(SalesOrder).filter(SalesOrder.organization_id == org_id, SalesOrder.status.in_(SALE_STATUSES)),
                 SalesOrder.created_at, df, dt).order_by(SalesOrder.created_at)
    orders = q.all()
    rows = [{"date": o.created_at.date().isoformat(), "order_number": o.order_number,
             "customer": o.customer.name if o.customer else None, "amount": o.total, "status": o.status}
            for o in orders]
    return {"summary": {"total_sales": round(sum(o.total for o in orders), 2), "total_orders": len(orders)}, "rows": rows}


def _purchase(db, org_id, df, dt):
    q = _between(db.query(PurchaseInvoice).filter(PurchaseInvoice.organization_id == org_id, PurchaseInvoice.status == "approved"),
                 PurchaseInvoice.invoice_date, df, dt).order_by(PurchaseInvoice.invoice_date)
    invs = q.all()
    rows = [{"date": i.invoice_date.date().isoformat(), "invoice_number": i.invoice_number,
             "supplier": i.supplier.name if i.supplier else None, "amount": i.total} for i in invs]
    return {"summary": {"total_purchases": round(sum(i.total for i in invs), 2), "total_invoices": len(invs)}, "rows": rows}


def _customer_outstanding(db, org_id, df, dt):
    custs = db.query(Customer).filter(Customer.organization_id == org_id).all()
    rows = [{"customer": c.name, "business_name": c.business_name, "phone": c.phone,
             "outstanding": round(c.outstanding_balance or 0, 2)} for c in custs if (c.outstanding_balance or 0) > 0]
    return {"summary": {"total_outstanding": round(sum(r["outstanding"] for r in rows), 2), "customers": len(rows)}, "rows": rows}


def _supplier_outstanding(db, org_id, df, dt):
    sups = db.query(Supplier).filter(Supplier.organization_id == org_id).all()
    rows = [{"supplier": s.name, "phone": s.phone, "outstanding": s.outstanding_payable}
            for s in sups if s.outstanding_payable > 0]
    return {"summary": {"total_payable": round(sum(r["outstanding"] for r in rows), 2), "suppliers": len(rows)}, "rows": rows}


def _payment_collection(db, org_id, df, dt):
    names = _customer_names(db, org_id)
    q = _between(db.query(CustomerPayment).filter(CustomerPayment.organization_id == org_id),
                 CustomerPayment.received_on, df, dt).order_by(CustomerPayment.received_on)
    pays = q.all()
    rows = [{"date": p.received_on.date().isoformat(), "customer": names.get(p.customer_id),
             "amount": p.amount, "mode": p.payment_mode, "reference": p.reference} for p in pays]
    return {"summary": {"total_collected": round(sum(p.amount for p in pays), 2), "payments": len(pays)}, "rows": rows}


def _cash_collection(db, org_id, df, dt):
    names = _customer_names(db, org_id)
    q = _between(db.query(CustomerPayment).filter(CustomerPayment.organization_id == org_id, CustomerPayment.payment_mode == "cash"),
                 CustomerPayment.received_on, df, dt).order_by(CustomerPayment.received_on)
    pays = q.all()
    rows = [{"date": p.received_on.date().isoformat(), "customer": names.get(p.customer_id), "amount": p.amount} for p in pays]
    return {"summary": {"total_cash": round(sum(p.amount for p in pays), 2), "payments": len(pays)}, "rows": rows}


def _expense(db, org_id, df, dt):
    q = _between(db.query(Expense).filter(Expense.organization_id == org_id), Expense.expense_date, df, dt).order_by(Expense.expense_date)
    exps = q.all()
    approved = sum(e.amount for e in exps if e.status == "approved")
    rows = [{"date": e.expense_date.date().isoformat(), "category": e.category, "amount": e.amount,
             "status": e.status, "description": e.description} for e in exps]
    return {"summary": {"total_expense": round(approved, 2), "entries": len(exps)}, "rows": rows}


def _gst_summary(db, org_id, df, dt):
    sales = _between(db.query(SalesOrder).filter(SalesOrder.organization_id == org_id, SalesOrder.status.in_(SALE_STATUSES)),
                     SalesOrder.created_at, df, dt).all()
    purch = _between(db.query(PurchaseInvoice).filter(PurchaseInvoice.organization_id == org_id, PurchaseInvoice.status == "approved"),
                     PurchaseInvoice.invoice_date, df, dt).all()
    output_gst = round(sum(o.tax or 0 for o in sales), 2)
    input_gst = round(sum(i.tax or 0 for i in purch), 2)
    rows = [{"type": "Output GST (Sales)", "amount": output_gst},
            {"type": "Input GST (Purchases)", "amount": input_gst},
            {"type": "Net GST Payable", "amount": round(output_gst - input_gst, 2)}]
    return {"summary": {"output_gst": output_gst, "input_gst": input_gst, "net_gst": round(output_gst - input_gst, 2)}, "rows": rows}


def _sales_return(db, org_id, df, dt):
    q = _between(db.query(SalesOrder).filter(SalesOrder.organization_id == org_id, SalesOrder.status.in_(("cancelled", "returned"))),
                 SalesOrder.updated_at, df, dt).order_by(SalesOrder.updated_at)
    orders = q.all()
    rows = [{"date": o.updated_at.date().isoformat(), "order_number": o.order_number,
             "customer": o.customer.name if o.customer else None, "amount": o.total, "status": o.status} for o in orders]
    return {"summary": {"total_returns": round(sum(o.total for o in orders), 2), "count": len(orders)}, "rows": rows}


def _purchase_return(db, org_id, df, dt):
    q = _between(db.query(PurchaseInvoice).filter(PurchaseInvoice.organization_id == org_id, PurchaseInvoice.status == "cancelled"),
                 PurchaseInvoice.updated_at, df, dt).order_by(PurchaseInvoice.updated_at)
    invs = q.all()
    rows = [{"date": i.updated_at.date().isoformat(), "invoice_number": i.invoice_number,
             "supplier": i.supplier.name if i.supplier else None, "amount": i.total} for i in invs]
    return {"summary": {"total_returns": round(sum(i.total for i in invs), 2), "count": len(invs)}, "rows": rows}


def _profit_loss(db, org_id, df, dt):
    sales = _sales(db, org_id, df, dt)["summary"]["total_sales"]
    purchases = _purchase(db, org_id, df, dt)["summary"]["total_purchases"]
    expenses = _expense(db, org_id, df, dt)["summary"]["total_expense"]
    profit = round(sales - purchases - expenses, 2)
    rows = [{"item": "Sales Revenue", "amount": sales},
            {"item": "Purchases", "amount": -purchases},
            {"item": "Expenses", "amount": -expenses},
            {"item": "Net Profit", "amount": profit}]
    return {"summary": {"total_sales": sales, "total_purchases": purchases, "total_expenses": expenses, "net_profit": profit}, "rows": rows}


def _daily_transaction(db, org_id, df, dt):
    cust_names = _customer_names(db, org_id)
    sup_names = _supplier_names(db, org_id)
    rows = []
    for o in _between(db.query(SalesOrder).filter(SalesOrder.organization_id == org_id, SalesOrder.status.in_(SALE_STATUSES)), SalesOrder.created_at, df, dt):
        rows.append({"date": o.created_at.date().isoformat(), "type": "Sale", "reference": o.order_number,
                     "party": o.customer.name if o.customer else None, "amount": o.total})
    for i in _between(db.query(PurchaseInvoice).filter(PurchaseInvoice.organization_id == org_id, PurchaseInvoice.status == "approved"), PurchaseInvoice.invoice_date, df, dt):
        rows.append({"date": i.invoice_date.date().isoformat(), "type": "Purchase", "reference": i.invoice_number,
                     "party": sup_names.get(i.supplier_id), "amount": -i.total})
    for p in _between(db.query(CustomerPayment).filter(CustomerPayment.organization_id == org_id), CustomerPayment.received_on, df, dt):
        rows.append({"date": p.received_on.date().isoformat(), "type": "Payment In", "reference": p.reference,
                     "party": cust_names.get(p.customer_id), "amount": p.amount})
    for p in _between(db.query(SupplierPayment).filter(SupplierPayment.organization_id == org_id), SupplierPayment.paid_on, df, dt):
        rows.append({"date": p.paid_on.date().isoformat(), "type": "Payment Out", "reference": p.reference,
                     "party": sup_names.get(p.supplier_id), "amount": -p.amount})
    for e in _between(db.query(Expense).filter(Expense.organization_id == org_id), Expense.expense_date, df, dt):
        rows.append({"date": e.expense_date.date().isoformat(), "type": "Expense", "reference": e.category,
                     "party": None, "amount": -e.amount})
    rows.sort(key=lambda r: r["date"])
    money_in = round(sum(r["amount"] for r in rows if r["amount"] > 0), 2)
    money_out = round(sum(-r["amount"] for r in rows if r["amount"] < 0), 2)
    return {"summary": {"money_in": money_in, "money_out": money_out, "net": round(money_in - money_out, 2), "transactions": len(rows)}, "rows": rows}


_BUILDERS = {
    "daily-transaction": _daily_transaction,
    "sales": _sales,
    "purchase": _purchase,
    "customer-outstanding": _customer_outstanding,
    "supplier-outstanding": _supplier_outstanding,
    "payment-collection": _payment_collection,
    "expense": _expense,
    "cash-collection": _cash_collection,
    "gst-summary": _gst_summary,
    "sales-return": _sales_return,
    "purchase-return": _purchase_return,
    "profit-loss": _profit_loss,
}


def build_report(db: Session, org_id: str, report_type: str, date_from: str | None, date_to: str | None) -> dict:
    if report_type not in _BUILDERS:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Unknown report type. Valid: {sorted(REPORT_TYPES)}")
    df, dt = _range(date_from, date_to)
    result = _BUILDERS[report_type](db, org_id, df, dt)
    result["type"] = report_type
    result["date_from"] = date_from
    result["date_to"] = date_to
    return result
