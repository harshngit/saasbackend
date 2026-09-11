import csv
import io
from datetime import date, datetime
from typing import Any

from openpyxl import Workbook, load_workbook
from pydantic import BaseModel, Field


class RowError(BaseModel):
    row: int
    column: str | None = None
    value: Any = None
    message: str


class ImportSummaryOut(BaseModel):
    total_rows: int = 0
    total_records: int = 0
    success_count: int = 0
    error_count: int = 0
    created_ids: list[str] = Field(default_factory=list)
    errors: list[RowError] = Field(default_factory=list)


def generate_xlsx_template(columns: list[str], example_row: list[Any] | None = None) -> bytes:
    """Generate a clean .xlsx template with the exact provided columns."""
    wb = Workbook()
    ws = wb.active
    ws.title = "Template"
    ws.append(columns)
    if example_row:
        ws.append(example_row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def parse_spreadsheet_rows(
    content: bytes, filename: str, required_headers: list[str] | None = None
) -> tuple[list[dict[str, Any]], list[RowError]]:
    """Parse .xlsx or .csv content into normalized list of row dictionaries.

    Returns: (rows, errors)
    Each row dictionary has keys matching column names (lowercased, stripped).
    Row numbers in errors match the 1-indexed sheet/file line number (header is row 1, data starts at row 2).
    """
    rows: list[dict[str, Any]] = []
    errors: list[RowError] = []

    is_csv = filename.lower().endswith(".csv")

    if is_csv:
        try:
            # Handle potential BOM (utf-8-sig)
            text_stream = io.StringIO(content.decode("utf-8-sig"))
            reader = csv.reader(text_stream)
            raw_headers = next(reader, None)
            if not raw_headers:
                errors.append(RowError(row=1, message="Uploaded CSV file is empty or has no header row"))
                return rows, errors

            headers = [h.strip() for h in raw_headers]
            for line_idx, raw_row in enumerate(reader, start=2):
                if not raw_row or not any(str(c).strip() for c in raw_row):
                    continue  # Skip blank lines
                row_dict: dict[str, Any] = {}
                for col_idx, header in enumerate(headers):
                    val = raw_row[col_idx].strip() if col_idx < len(raw_row) else ""
                    row_dict[header] = val if val != "" else None
                row_dict["_row_number"] = line_idx
                rows.append(row_dict)
        except Exception as exc:
            errors.append(RowError(row=1, message=f"Failed to parse CSV file: {exc}"))
            return rows, errors
    else:
        try:
            wb = load_workbook(io.BytesIO(content), data_only=True, read_only=True)
            ws = wb.active
            if ws is None:
                errors.append(RowError(row=1, message="Uploaded Excel workbook contains no active sheets"))
                return rows, errors

            raw_iter = ws.iter_rows(values_only=True)
            header_row = next(raw_iter, None)
            if not header_row or not any(header_row):
                errors.append(RowError(row=1, message="Uploaded Excel file is empty or has no header row"))
                return rows, errors

            headers = [str(h).strip() if h is not None else "" for h in header_row]

            for row_idx, raw_row in enumerate(raw_iter, start=2):
                if not raw_row or not any(c is not None and str(c).strip() != "" for c in raw_row):
                    continue  # Skip blank lines
                row_dict = {}
                for col_idx, header in enumerate(headers):
                    if not header:
                        continue
                    val = raw_row[col_idx] if col_idx < len(raw_row) else None
                    if isinstance(val, (datetime, date)):
                        row_dict[header] = val.isoformat()
                    elif isinstance(val, str):
                        s_val = val.strip()
                        row_dict[header] = s_val if s_val != "" else None
                    else:
                        row_dict[header] = val
                row_dict["_row_number"] = row_idx
                rows.append(row_dict)
        except Exception as exc:
            errors.append(RowError(row=1, message=f"Failed to parse Excel file: {exc}"))
            return rows, errors

    # Check required headers if specified
    if required_headers and headers:
        header_set = set(headers)
        for req in required_headers:
            if req not in header_set:
                errors.append(RowError(row=1, column=req, message=f"Missing required column '{req}' in template header"))

    return rows, errors
