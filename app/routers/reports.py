from io import BytesIO
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import require_permission
from app.models import User
from app.models.organization import Organization
from app.schemas.report import ReportResponse
from app.services import report_service

router = APIRouter(prefix="/reports", tags=["reports"])

_view = require_permission("reports", "view")
_export = require_permission("reports", "export")


def _org_id(user: User) -> str:
    if not user.organization_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No organization on this account")
    return user.organization_id


def _to_xlsx(report: dict, org_name: str | None = None) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill

    wb = Workbook()
    ws = wb.active
    ws.title = report.get("type", "Report")[:31]

    # Organization & Report Header
    header_font = Font(name="Arial", size=14, bold=True, color="1F2937")
    sub_font = Font(name="Arial", size=10, bold=True, color="4B5563")
    sec_font = Font(name="Arial", size=11, bold=True, color="111827")
    tbl_hdr_font = Font(name="Arial", size=10, bold=True, color="FFFFFF")
    tbl_hdr_fill = PatternFill(start_color="3B82F6", end_color="3B82F6", fill_type="solid")

    row_idx = 1
    if org_name:
        ws.cell(row=row_idx, column=1, value=org_name).font = header_font
        row_idx += 1

    title = report.get("meta", {}).get("title") or report.get("type", "Report").replace("-", " ").title()
    ws.cell(row=row_idx, column=1, value=f"{title} Report").font = Font(name="Arial", size=12, bold=True)
    row_idx += 1

    period_str = f"Period: {report.get('date_from') or 'All Time'} to {report.get('date_to') or 'Present'}"
    ws.cell(row=row_idx, column=1, value=period_str).font = sub_font
    row_idx += 2

    # Summary Section
    summary = report.get("summary", {})
    if summary:
        ws.cell(row=row_idx, column=1, value="Summary Metrics").font = sec_font
        row_idx += 1
        for k, v in summary.items():
            label = k.replace("_", " ").title()
            ws.cell(row=row_idx, column=1, value=label).font = Font(name="Arial", size=10, bold=True)
            ws.cell(row=row_idx, column=2, value=v).font = Font(name="Arial", size=10)
            row_idx += 1
        row_idx += 1

    # Data Table Section
    rows = report.get("rows", [])
    meta = report.get("meta", {})
    columns = meta.get("columns", [])

    if columns:
        headers = [c["label"] for c in columns]
        keys = [c["key"] for c in columns]
    elif rows:
        keys = list(rows[0].keys())
        headers = [k.replace("_", " ").title() for k in keys]
    else:
        keys = []
        headers = []

    if headers:
        for col_idx, h in enumerate(headers, start=1):
            cell = ws.cell(row=row_idx, column=col_idx, value=h)
            cell.font = tbl_hdr_font
            cell.fill = tbl_hdr_fill
            cell.alignment = Alignment(horizontal="center" if "date" in h.lower() else "left")
        row_idx += 1

        for r in rows:
            for col_idx, k in enumerate(keys, start=1):
                val = r.get(k)
                ws.cell(row=row_idx, column=col_idx, value=val)
            row_idx += 1

    # Auto-adjust column width
    for col in ws.columns:
        max_len = max(len(str(cell.value or "")) for cell in col)
        col_letter = col[0].column_letter
        ws.column_dimensions[col_letter].width = max(max_len + 3, 12)

    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _s(text) -> str:
    """Make text safe for fpdf's latin-1 core fonts."""
    return str("" if text is None else text).encode("latin-1", "replace").decode("latin-1")


def _to_pdf(report: dict, org_name: str | None = None) -> bytes:
    from fpdf import FPDF
    from fpdf.enums import XPos, YPos

    rows = report.get("rows", [])
    if len(rows) > 2500:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"PDF export limit exceeded ({len(rows)} rows). Please use Excel export or filter by date range.",
        )

    pdf = FPDF(orientation="L")
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()

    if org_name:
        pdf.set_font("Helvetica", "B", 14)
        pdf.cell(0, 8, _s(org_name), new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    title = report.get("meta", {}).get("title") or report.get("type", "Report").replace("-", " ").title()
    pdf.set_font("Helvetica", "B", 12)
    pdf.cell(0, 7, _s(f"{title} Report"), new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    pdf.set_font("Helvetica", size=9)
    period = f"Period: {report.get('date_from') or 'All Time'} to {report.get('date_to') or 'Present'}"
    pdf.cell(0, 6, _s(period), new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    # Summary
    summary = report.get("summary", {})
    if summary:
        pdf.ln(2)
        pdf.set_font("Helvetica", "B", 9)
        pdf.cell(0, 6, _s("Summary Metrics:"), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.set_font("Helvetica", size=8)
        summary_str = "  |  ".join(f"{k.replace('_', ' ').title()}: {v}" for k, v in summary.items())
        pdf.multi_cell(0, 5, _s(summary_str))

    pdf.ln(4)

    meta = report.get("meta", {})
    columns = meta.get("columns", [])

    if columns:
        headers = [c["label"] for c in columns]
        keys = [c["key"] for c in columns]
    elif rows:
        keys = list(rows[0].keys())
        headers = [k.replace("_", " ").title() for k in keys]
    else:
        keys = []
        headers = []

    if headers:
        col_w = max(18, min(65, 275 // max(1, len(headers))))
        pdf.set_font("Helvetica", "B", 8)
        pdf.set_fill_color(220, 230, 245)
        for h in headers:
            pdf.cell(col_w, 6, _s(str(h)[:20]), border=1, fill=True)
        pdf.ln()

        pdf.set_font("Helvetica", size=7)
        for r in rows:
            for k in keys:
                val = r.get(k)
                pdf.cell(col_w, 5, _s(str(val if val is not None else ""))[:20], border=1)
            pdf.ln()

    return bytes(pdf.output())


@router.get("/{report_type}", response_model=ReportResponse)
def get_report(
    report_type: str,
    user: User = Depends(_view),
    date_from: str | None = Query(default=None, description="YYYY-MM-DD"),
    date_to: str | None = Query(default=None, description="YYYY-MM-DD"),
    search: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=100),
    customer_id: str | None = Query(default=None),
    supplier_id: str | None = Query(default=None),
    product_id: str | None = Query(default=None),
    warehouse_id: str | None = Query(default=None),
    salesperson_id: str | None = Query(default=None),
    category: str | None = Query(default=None),
    status: str | None = Query(default=None),
    payment_status: str | None = Query(default=None),
    payment_mode: str | None = Query(default=None),
    verification_status: str | None = Query(default=None),
    ageing_bucket: str | None = Query(default=None),
    overdue_only: bool = Query(default=False),
    group_by: str | None = Query(default=None),
    as_of_date: str | None = Query(default=None),
    movement_type: str | None = Query(default=None),
    low_stock_only: bool = Query(default=False),
    db: Session = Depends(get_db),
) -> dict:
    """Standardized financial, inventory, and operational reporting endpoint."""
    filters: dict[str, Any] = {
        "search": search,
        "page": page,
        "page_size": page_size,
        "customer_id": customer_id,
        "supplier_id": supplier_id,
        "product_id": product_id,
        "warehouse_id": warehouse_id,
        "salesperson_id": salesperson_id,
        "category": category,
        "status": status,
        "payment_status": payment_status,
        "payment_mode": payment_mode,
        "verification_status": verification_status,
        "ageing_bucket": ageing_bucket,
        "overdue_only": overdue_only,
        "group_by": group_by,
        "as_of_date": as_of_date,
        "movement_type": movement_type,
        "low_stock_only": low_stock_only,
    }

    return report_service.build_report(
        db=db,
        org_id=_org_id(user),
        report_type=report_type,
        date_from=date_from,
        date_to=date_to,
        filters=filters,
        is_export=False,
    )


@router.get("/{report_type}/export")
def export_report(
    report_type: str,
    user: User = Depends(_export),
    format: str = Query(default="excel", description="excel | pdf"),
    date_from: str | None = Query(default=None),
    date_to: str | None = Query(default=None),
    search: str | None = Query(default=None),
    customer_id: str | None = Query(default=None),
    supplier_id: str | None = Query(default=None),
    product_id: str | None = Query(default=None),
    warehouse_id: str | None = Query(default=None),
    salesperson_id: str | None = Query(default=None),
    category: str | None = Query(default=None),
    status: str | None = Query(default=None),
    payment_status: str | None = Query(default=None),
    payment_mode: str | None = Query(default=None),
    verification_status: str | None = Query(default=None),
    ageing_bucket: str | None = Query(default=None),
    overdue_only: bool = Query(default=False),
    group_by: str | None = Query(default=None),
    as_of_date: str | None = Query(default=None),
    movement_type: str | None = Query(default=None),
    low_stock_only: bool = Query(default=False),
    db: Session = Depends(get_db),
) -> Response:
    """Download full unpaginated report as an Excel (.xlsx) or PDF file."""
    org_id = _org_id(user)
    org = db.get(Organization, org_id)
    org_name = org.name if org else None

    filters: dict[str, Any] = {
        "search": search,
        "customer_id": customer_id,
        "supplier_id": supplier_id,
        "product_id": product_id,
        "warehouse_id": warehouse_id,
        "salesperson_id": salesperson_id,
        "category": category,
        "status": status,
        "payment_status": payment_status,
        "payment_mode": payment_mode,
        "verification_status": verification_status,
        "ageing_bucket": ageing_bucket,
        "overdue_only": overdue_only,
        "group_by": group_by,
        "as_of_date": as_of_date,
        "movement_type": movement_type,
        "low_stock_only": low_stock_only,
    }

    report = report_service.build_report(
        db=db,
        org_id=org_id,
        report_type=report_type,
        date_from=date_from,
        date_to=date_to,
        filters=filters,
        is_export=True,
    )

    if format == "pdf":
        content = _to_pdf(report, org_name=org_name)
        media = "application/pdf"
        ext = "pdf"
    elif format == "excel":
        content = _to_xlsx(report, org_name=org_name)
        media = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        ext = "xlsx"
    else:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="format must be 'excel' or 'pdf'")

    filename = f"{report_type}-report.{ext}"
    return Response(
        content=content,
        media_type=media,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
