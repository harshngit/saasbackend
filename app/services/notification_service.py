from sqlalchemy.orm import Session

from app.models import Notification, User, UserRole


def notify(db: Session, user_id: str, title: str, body: str | None = None,
           type: str = "info", link: str | None = None, organization_id: str | None = None) -> Notification:
    """Create a notification for a single user (does not commit)."""
    n = Notification(user_id=user_id, organization_id=organization_id, title=title, body=body, type=type, link=link)
    db.add(n)
    return n


def notify_org_admins(db: Session, organization_id: str, title: str, body: str | None = None,
                      type: str = "info", link: str | None = None) -> None:
    """Notify every admin of an organization."""
    admins = (
        db.query(User)
        .filter(User.organization_id == organization_id, User.role == UserRole.ADMIN, User.is_active.is_(True))
        .all()
    )
    for admin in admins:
        notify(db, admin.id, title, body, type, link, organization_id)
