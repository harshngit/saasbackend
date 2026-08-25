from datetime import date
from sqlalchemy.orm import Session

from app.models import Leave


def calculate_days_count(start_date: date, end_date: date) -> float:
    """Calculate inclusive number of days between start_date and end_date."""
    if start_date > end_date:
        raise ValueError("start_date cannot be after end_date")
    return float((end_date - start_date).days + 1)
