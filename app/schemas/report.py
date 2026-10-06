from typing import Any
from pydantic import BaseModel, Field


class ReportColumn(BaseModel):
    key: str
    label: str
    type: str = Field(
        default="text",
        description="text | date | datetime | currency | number | status | percent | reference",
    )


class ReportMeta(BaseModel):
    currency: str = "INR"
    group_by: str | None = None
    valuation_basis: str | None = None
    cogs_notes: str | None = None
    notes: str | None = None
    columns: list[ReportColumn] = Field(default_factory=list)


class ReportPagination(BaseModel):
    page: int = 1
    page_size: int = 25
    total: int = 0
    total_pages: int = 0


class ReportChart(BaseModel):
    type: str = "none"  # line | bar | pie | none
    title: str = ""
    data: list[dict[str, Any]] = Field(default_factory=list)


class ReportResponse(BaseModel):
    type: str
    date_from: str | None = None
    date_to: str | None = None
    summary: dict[str, Any] = Field(default_factory=dict)
    rows: list[dict[str, Any]] = Field(default_factory=list)
    pagination: ReportPagination | None = None
    meta: ReportMeta | None = None
    chart: ReportChart | None = None
