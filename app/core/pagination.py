from typing import Any
from sqlalchemy.orm import Query
from app.schemas.pagination import PaginatedResponse


def paginate(
    query: Query,
    page: int = 1,
    page_size: int = 10,
    max_page_size: int = 100,
) -> PaginatedResponse:
    """Execute SQL COUNT and apply database LIMIT and OFFSET.

    Returns:
        PaginatedResponse (which is also iterable as (items, total, page, page_size, total_pages))
    """
    if page < 1:
        page = 1
    if page_size < 1:
        page_size = 10
    elif page_size > max_page_size:
        page_size = max_page_size

    total = query.order_by(None).count()
    total_pages = (total + page_size - 1) // page_size if total > 0 else 0
    offset = (page - 1) * page_size
    items = query.offset(offset).limit(page_size).all()
    return PaginatedResponse(
        items=items,
        total=total,
        page=page,
        page_size=page_size,
        total_pages=total_pages,
    )
