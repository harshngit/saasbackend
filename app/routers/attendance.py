from datetime import date, datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import get_current_user, require_permission, require_system_role
from app.models import ATTENDANCE_TYPES, Attendance, SystemRole, User

router = APIRouter(prefix="/attendance", tags=["attendance"])

_mark = require_permission("attendance", "create")
_view = require_permission("attendance", "view")
_ADMIN = require_system_role(SystemRole.ADMIN)

from app.schemas.attendance import AttendanceOut, CheckInBody  # noqa: E402


def _date_range(date_from: str | None, date_to: str | None) -> tuple[date | None, date | None]:
    try:
        d_from = date.fromisoformat(date_from) if date_from else None
        d_to = date.fromisoformat(date_to) if date_to else None
    except ValueError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Dates must be YYYY-MM-DD")
    if d_from and d_to and d_from > d_to:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="date_from cannot be after date_to")
    return d_from, d_to


@router.post("/check-in", response_model=AttendanceOut, status_code=status.HTTP_201_CREATED)
def check_in(payload: CheckInBody, user: User = Depends(_mark), db: Session = Depends(get_db)) -> Attendance:
    """Record one of the four daily checkpoints for the logged-in user."""
    today = datetime.now(timezone.utc).date()
    row = (
        db.query(Attendance)
        .filter(Attendance.user_id == user.id, Attendance.day == today)
        .first()
    )
    if row is None:
        row = Attendance(organization_id=user.organization_id, user_id=user.id, day=today)
        db.add(row)

    if getattr(row, payload.type) is not None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"'{payload.type}' already recorded today")

    # Enforce order: each checkpoint needs the previous one done first.
    idx = ATTENDANCE_TYPES.index(payload.type)
    for prior in ATTENDANCE_TYPES[:idx]:
        if getattr(row, prior) is None:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Record '{prior}' before '{payload.type}'")

    setattr(row, payload.type, datetime.now(timezone.utc))
    db.commit()
    db.refresh(row)
    return row


@router.get("/me", response_model=list[AttendanceOut])
def my_attendance(
    user: User = Depends(_view),
    date_from: str | None = Query(default=None),
    date_to: str | None = Query(default=None),
    db: Session = Depends(get_db),
) -> list[Attendance]:
    d_from, d_to = _date_range(date_from, date_to)
    query = db.query(Attendance).filter(Attendance.user_id == user.id)
    if d_from:
        query = query.filter(Attendance.day >= d_from)
    if d_to:
        query = query.filter(Attendance.day <= d_to)
    return query.order_by(Attendance.day.desc()).all()


@router.get("", response_model=list[AttendanceOut])
def all_attendance(
    admin: User = Depends(_ADMIN),
    user_id: str | None = Query(default=None),
    date_from: str | None = Query(default=None),
    date_to: str | None = Query(default=None),
    db: Session = Depends(get_db),
) -> list[Attendance]:
    """Admin monitoring view — attendance across the firm's staff."""
    d_from, d_to = _date_range(date_from, date_to)
    # Scope to the admin's org via the users in it.
    org_user_ids = [u.id for u in db.query(User.id).filter(User.organization_id == admin.organization_id)]
    query = db.query(Attendance).filter(Attendance.user_id.in_(org_user_ids))
    if user_id:
        query = query.filter(Attendance.user_id == user_id)
    if d_from:
        query = query.filter(Attendance.day >= d_from)
    if d_to:
        query = query.filter(Attendance.day <= d_to)
    return query.order_by(Attendance.day.desc()).all()
