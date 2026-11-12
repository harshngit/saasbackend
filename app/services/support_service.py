"""Support tickets: organization users raise and follow tickets, platform staff answer.

Rules (team decisions D3-D11):
- Organization side: an Admin sees every ticket of their organization, Staff only the
  tickets they raised. Anything else -- another firm's ticket included -- is 404.
- Internal notes are platform-only and text-only; they are never returned to the
  organization and never notified.
- Ticket numbers TKT-YYYY-NNNN come from one platform-wide counter per Asia/Kolkata year.
- At most 10 tickets per organization in any rolling 24 hours (429).
- Attachments are files the same user uploaded first (POST /files/upload), in the same
  organization, images or PDF, at most 3 per message, each attached once.
- Reopening (and a customer reply on a resolved ticket) is allowed for 7 days after the
  ticket was resolved / closed.
- Every change moves last_activity_at.
"""

import logging
from datetime import date, datetime, timedelta, timezone

from fastapi import HTTPException, status
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, aliased, selectinload

from app.core import business_time
from app.core.files import FILES_PATH, normalize_file_url
from app.core.pagination import paginate
from app.models import (
    PLATFORM_ROLES,
    Organization,
    StoredFile,
    SupportTicket,
    SupportTicketAttachment,
    SupportTicketMessage,
    SystemRole,
    User,
)
from app.models.support_ticket import ATTACHMENT_FILE_UNIQUE, TICKET_PRIORITIES, TICKET_STATUSES
from app.schemas.pagination import PaginatedResponse
from app.schemas.support import (
    PlatformMessageCreate,
    PlatformTicketListItem,
    PlatformTicketOut,
    PlatformTicketUpdate,
    SupportSummaryOut,
    TicketAttachmentOut,
    TicketCreate,
    TicketListItem,
    TicketMessageCreate,
    TicketMessageOut,
    TicketOut,
)
from app.services import notification_service, numbering_service, rate_limit_service

logger = logging.getLogger("crm.support")

REOPEN_WINDOW = timedelta(days=7)
REOPEN_EXPIRED = "Reopen window expired, please raise a new ticket."
NO_REPLY_AFTER = timedelta(hours=24)
TERMINAL = ("resolved", "closed")
RATE_LIMIT_SCOPE = "support_ticket_org"
_NOT_FOUND = "Ticket not found"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


# ------------------------------------------------------------------ who is who


def is_org_admin(user: User) -> bool:
    return user.effective_system_role == SystemRole.ADMIN.value


def is_platform_user(user: User | None) -> bool:
    """No organization and a platform role on the stored system_role -- never inferred
    from the legacy role column, so an organization user can never pass."""
    return (
        user is not None
        and user.organization_id is None
        and user.is_active
        and user.system_role in PLATFORM_ROLES
    )


def is_super_admin(user: User) -> bool:
    return is_platform_user(user) and user.system_role == SystemRole.SUPER_ADMIN.value


def require_org_user(user: User) -> str:
    if not user.organization_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Support tickets are raised from an organization account",
        )
    return user.organization_id


def require_platform_user(user: User) -> User:
    if not is_platform_user(user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You do not have permission to perform this action")
    return user


# ------------------------------------------------------------------ helpers


def _touch(ticket: SupportTicket, moment: datetime | None = None) -> None:
    moment = moment or _now()
    ticket.last_activity_at = moment
    ticket.updated_at = moment


def _set_status(ticket: SupportTicket, new_status: str, moment: datetime) -> bool:
    """Move the ticket to `new_status` with consistent timestamps. Returns whether it changed.

    resolved -> resolved_at = now, closed_at cleared.
    closed   -> closed_at = now (resolved_at kept: when it was resolved stays true).
    anything else -> both cleared, so no stale terminal timestamp outlives the state.
    """
    if ticket.status == new_status:
        return False
    ticket.status = new_status
    if new_status == "resolved":
        ticket.resolved_at, ticket.closed_at = moment, None
    elif new_status == "closed":
        ticket.closed_at = moment
    else:
        ticket.resolved_at = ticket.closed_at = None
    return True


def _within_window(since: datetime | None, moment: datetime) -> bool:
    """A missing timestamp cannot prove the ticket is inside the window: treated as expired."""
    since = _aware(since)
    return since is not None and moment - since <= REOPEN_WINDOW


def _safe_notify(db: Session, fn, *args) -> None:  # noqa: ANN001
    """Notifications are created in the ticket's transaction, inside a savepoint: a
    failure there is logged and undone without taking the ticket change with it.

    The ticket's own pending rows are flushed first, outside the savepoint, so an error
    in them (a duplicate attachment, say) surfaces as itself and is never mistaken for --
    and swallowed as -- a notification failure."""
    db.flush()
    try:
        with db.begin_nested():
            fn(db, *args)
    except Exception:  # noqa: BLE001
        logger.exception("Support ticket notification failed (%s)", getattr(fn, "__name__", fn))


def _attachable_files(db: Session, user: User, org_id: str, file_ids: list[str]) -> list[StoredFile]:
    """D9: every file must exist, be in this organization, have been uploaded by this user
    (a file with no recorded uploader is never accepted), be an image or PDF, and not be
    attached to a ticket already. Unknown / foreign / someone else's files all get the same
    answer, so a file id's existence is not revealed."""
    ids = [i.strip() for i in file_ids if i and i.strip()]
    if len(set(ids)) != len(ids):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="attachment_file_ids has duplicates")
    if not ids:
        return []
    found = {f.id: f for f in db.query(StoredFile).filter(StoredFile.id.in_(ids))}
    files = []
    for file_id in ids:
        stored = found.get(file_id)
        if (
            stored is None
            or stored.organization_id != org_id
            or stored.uploaded_by_user_id is None
            or stored.uploaded_by_user_id != user.id
        ):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Attachment {file_id} is not a file you uploaded",
            )
        content_type = (stored.content_type or "").lower()
        if not (content_type.startswith("image/") or content_type == "application/pdf"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="Attachments must be images or PDF files"
            )
        files.append(stored)
    taken = db.query(SupportTicketAttachment.stored_file_id).filter(
        SupportTicketAttachment.stored_file_id.in_(ids)
    ).first()
    if taken is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=f"File {taken[0]} is already attached to a ticket"
        )
    return files


def _guard_attachment_race(db: Session, action) -> None:  # noqa: ANN001
    """Run `action` (a flush or the commit). Two requests attaching the same file at once
    both pass the check above; the unique constraint stops the second, which gets the same
    409. Any other integrity error is re-raised unchanged."""
    try:
        action()
    except IntegrityError as exc:
        db.rollback()
        if ATTACHMENT_FILE_UNIQUE in str(getattr(exc, "orig", exc)):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail="A file is already attached to a ticket"
            ) from None
        raise


def _add_attachments(db: Session, ticket_id: str, message_id: str | None, files: list[StoredFile]) -> None:
    for f in files:
        db.add(SupportTicketAttachment(ticket_id=ticket_id, message_id=message_id, stored_file_id=f.id))
    if files:
        _guard_attachment_race(db, db.flush)  # surface a lost race now, before anything else


def _commit(db: Session) -> None:
    _guard_attachment_race(db, db.commit)


# ------------------------------------------------------------------ responses


def _attachment_out(att: SupportTicketAttachment) -> TicketAttachmentOut:
    f = att.stored_file
    return TicketAttachmentOut(
        id=att.id, file_id=att.stored_file_id, message_id=att.message_id,
        filename=f.filename if f else "", content_type=f.content_type if f else "",
        size=f.size if f else 0, url=normalize_file_url(f"{FILES_PATH}/{att.stored_file_id}") or "",
        created_at=att.created_at,
    )


def _message_out(msg: SupportTicketMessage) -> TicketMessageOut:
    return TicketMessageOut(
        id=msg.id, author_type=msg.author_type, author_user_id=msg.author_user_id,
        author_name=msg.author.name if msg.author else None, body=msg.body,
        is_internal=bool(msg.is_internal), created_at=msg.created_at,
        attachments=[_attachment_out(a) for a in msg.attachments],
    )


def _list_fields(t: SupportTicket) -> dict:
    """What a list row shows: no description, messages or attachments (nothing lazy)."""
    return {
        "id": t.id, "ticket_number": t.ticket_number, "subject": t.subject, "category": t.category,
        "priority": t.priority, "status": t.status, "created_by_user_id": t.created_by_user_id,
        "created_by_name": t.created_by.name if t.created_by else None,
        "assigned_to_user_id": t.assigned_to_user_id,
        "assigned_to_name": t.assigned_to.name if t.assigned_to else None,
        "last_activity_at": t.last_activity_at, "created_at": t.created_at,
    }


def _base_fields(t: SupportTicket) -> dict:
    return {
        **_list_fields(t),
        "description": t.description, "resolved_at": t.resolved_at, "closed_at": t.closed_at,
        "updated_at": t.updated_at,
        "attachments": [_attachment_out(a) for a in t.attachments if a.message_id is None],
    }


def org_ticket_out(ticket: SupportTicket) -> TicketOut:
    """The organization's view: public messages only. Internal notes have no attachments
    (rejected on write), and are filtered out here, so nothing of theirs is returned."""
    return TicketOut(
        **_base_fields(ticket),
        messages=[_message_out(m) for m in ticket.messages if not m.is_internal],
    )


def platform_ticket_out(ticket: SupportTicket) -> PlatformTicketOut:
    return PlatformTicketOut(
        **_base_fields(ticket),
        organization_id=ticket.organization_id,
        organization_name=ticket.organization.name if ticket.organization else None,
        messages=[_message_out(m) for m in ticket.messages],
    )


def _load(db: Session, ticket_id: str):  # noqa: ANN202
    return (
        db.query(SupportTicket)
        .options(
            selectinload(SupportTicket.messages).selectinload(SupportTicketMessage.attachments)
            .selectinload(SupportTicketAttachment.stored_file),
            selectinload(SupportTicket.messages).selectinload(SupportTicketMessage.author),
            selectinload(SupportTicket.attachments).selectinload(SupportTicketAttachment.stored_file),
            selectinload(SupportTicket.created_by),
            selectinload(SupportTicket.assigned_to),
        )
        .filter(SupportTicket.id == ticket_id)
    )


# ------------------------------------------------------------------ organization side


def _org_scope(query, user: User, org_id: str):  # noqa: ANN001, ANN202
    """D4: the organization's tickets; a non-admin only the ones they raised."""
    query = query.filter(SupportTicket.organization_id == org_id)
    if not is_org_admin(user):
        query = query.filter(SupportTicket.created_by_user_id == user.id)
    return query


def get_org_ticket(db: Session, user: User, ticket_id: str, *, lock: bool = False) -> SupportTicket:
    org_id = require_org_user(user)
    query = _org_scope(_load(db, ticket_id), user, org_id)
    ticket = query.populate_existing().with_for_update(of=SupportTicket).first() if lock else query.first()
    if ticket is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_NOT_FOUND)
    return ticket


def create_ticket(db: Session, user: User, payload: TicketCreate) -> SupportTicket:
    org_id = require_org_user(user)
    # D3: shared by the whole organization; the advisory lock makes concurrent requests
    # count one after another, so the 11th never slips through.
    rate_limit_service.hold(db, (RATE_LIMIT_SCOPE, org_id))
    if rate_limit_service.exceeded(db, RATE_LIMIT_SCOPE, org_id, rate_limit_service.SUPPORT_TICKET_ORG):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Your organization has raised 10 support tickets in the last 24 hours. Please try again later.",
        )
    files = _attachable_files(db, user, org_id, payload.attachment_file_ids)
    moment = _now()
    ticket = SupportTicket(
        ticket_number=numbering_service.next_global_number(db, "TKT"),
        organization_id=org_id, created_by_user_id=user.id,
        subject=payload.subject, description=payload.description,
        category=payload.category, priority=payload.priority, status="open",
        last_activity_at=moment, created_at=moment, updated_at=moment,
    )
    db.add(ticket)
    db.flush()
    _add_attachments(db, ticket.id, None, files)
    rate_limit_service.record(db, RATE_LIMIT_SCOPE, org_id)
    org = db.get(Organization, org_id)
    _safe_notify(db, notification_service.notify_ticket_created, ticket, org.name if org else None, user.id)
    _commit(db)
    return _load(db, ticket.id).first()


def list_org_tickets(
    db: Session, user: User, *, status_filter: str | None, category: str | None, priority: str | None,
    search: str | None, page: int, page_size: int,
) -> PaginatedResponse[TicketListItem]:
    org_id = require_org_user(user)
    query = _org_scope(db.query(SupportTicket), user, org_id)
    query = _common_filters(query, status_filter, category, priority, search)
    return _paged(query, page, page_size, lambda t: TicketListItem(**_list_fields(t)))


def customer_reply(db: Session, user: User, ticket_id: str, payload: TicketMessageCreate) -> SupportTicket:
    """D5: closed -> 409; resolved within 7 days -> back to open; resolved longer ago ->
    409 (exact reopen message); waiting_on_customer -> open; open / in_progress unchanged."""
    ticket = get_org_ticket(db, user, ticket_id, lock=True)
    moment = _now()
    if ticket.status == "closed":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This ticket is closed. Reopen it, or raise a new ticket.",
        )
    if ticket.status == "resolved" and not _within_window(ticket.resolved_at, moment):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=REOPEN_EXPIRED)
    files = _attachable_files(db, user, ticket.organization_id, payload.attachment_file_ids)
    message = SupportTicketMessage(
        ticket_id=ticket.id, author_user_id=user.id, author_type="customer",
        body=payload.body, is_internal=False, created_at=moment,
    )
    db.add(message)
    db.flush()
    _add_attachments(db, ticket.id, message.id, files)
    if ticket.status in ("waiting_on_customer", "resolved"):
        _set_status(ticket, "open", moment)
    _touch(ticket, moment)
    _safe_notify(db, notification_service.notify_ticket_customer_reply, ticket, message.id, user.id)
    _commit(db)
    return _load(db, ticket.id).first()


def close_ticket(db: Session, user: User, ticket_id: str) -> SupportTicket:
    ticket = get_org_ticket(db, user, ticket_id, lock=True)
    if ticket.status == "closed":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="This ticket is already closed")
    moment = _now()
    _set_status(ticket, "closed", moment)
    _touch(ticket, moment)
    _safe_notify(db, notification_service.notify_ticket_status_changed, ticket, user.id)
    _commit(db)
    return _load(db, ticket.id).first()


def reopen_ticket(db: Session, user: User, ticket_id: str) -> SupportTicket:
    """Only a resolved or closed ticket, and only within 7 days of when it was closed (or,
    if resolved, resolved). A missing timestamp counts as expired."""
    ticket = get_org_ticket(db, user, ticket_id, lock=True)
    if ticket.status not in TERMINAL:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Only a resolved or closed ticket can be reopened"
        )
    moment = _now()
    since = ticket.closed_at if ticket.status == "closed" else ticket.resolved_at
    if not _within_window(since, moment):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=REOPEN_EXPIRED)
    _set_status(ticket, "open", moment)
    _touch(ticket, moment)
    _safe_notify(db, notification_service.notify_ticket_status_changed, ticket, user.id)
    _commit(db)
    return _load(db, ticket.id).first()


# ------------------------------------------------------------------ shared list helpers


def _common_filters(query, status_filter, category, priority, search):  # noqa: ANN001, ANN202
    if status_filter:
        query = query.filter(SupportTicket.status == status_filter)
    if category:
        query = query.filter(SupportTicket.category == category)
    if priority:
        query = query.filter(SupportTicket.priority == priority)
    if search and search.strip():
        pattern = f"%{search.strip()}%"  # a bound parameter, never part of the SQL text
        query = query.filter(or_(SupportTicket.ticket_number.ilike(pattern), SupportTicket.subject.ilike(pattern)))
    return query


def _paged(query, page: int, page_size: int, convert) -> PaginatedResponse:  # noqa: ANN001
    query = query.options(selectinload(SupportTicket.created_by), selectinload(SupportTicket.assigned_to),
                          selectinload(SupportTicket.organization))
    query = query.order_by(SupportTicket.last_activity_at.desc(), SupportTicket.id.desc())
    items, total, page, page_size, total_pages = paginate(query, page=page, page_size=page_size)
    return PaginatedResponse(items=[convert(t) for t in items], total=total, page=page,
                             page_size=page_size, total_pages=total_pages)


# ------------------------------------------------------------------ platform side


def get_platform_ticket(db: Session, ticket_id: str, *, lock: bool = False) -> SupportTicket:
    query = _load(db, ticket_id).options(selectinload(SupportTicket.organization))
    ticket = query.populate_existing().with_for_update(of=SupportTicket).first() if lock else query.first()
    if ticket is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_NOT_FOUND)
    return ticket


def list_platform_tickets(
    db: Session, user: User, *, organization_id: str | None, status_filter: str | None,
    priority: str | None, category: str | None, assigned_to_user_id: str | None,
    assigned_to_me: bool, unassigned: bool, search: str | None,
    date_from: date | None, date_to: date | None, page: int, page_size: int,
) -> PaginatedResponse[PlatformTicketListItem]:
    if unassigned and (assigned_to_me or assigned_to_user_id):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="unassigned cannot be combined with assigned_to_me or assigned_to_user_id",
        )
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="date_from is after date_to")
    query = _common_filters(db.query(SupportTicket), status_filter, category, priority, search)
    if organization_id:
        query = query.filter(SupportTicket.organization_id == organization_id)
    if assigned_to_user_id:
        query = query.filter(SupportTicket.assigned_to_user_id == assigned_to_user_id)
    if assigned_to_me:
        query = query.filter(SupportTicket.assigned_to_user_id == user.id)
    if unassigned:
        query = query.filter(SupportTicket.assigned_to_user_id.is_(None))
    tz = business_time.zone(None)  # platform-wide: Asia/Kolkata business days, both ends inclusive
    if date_from:
        query = query.filter(SupportTicket.created_at >= business_time.day_start(date_from, tz))
    if date_to:
        query = query.filter(SupportTicket.created_at <= business_time.day_end(date_to, tz))

    def convert(t: SupportTicket) -> PlatformTicketListItem:
        return PlatformTicketListItem(**_list_fields(t), organization_id=t.organization_id,
                                      organization_name=t.organization.name if t.organization else None)

    return _paged(query, page, page_size, convert)


def update_ticket(db: Session, actor: User, ticket_id: str, payload: PlatformTicketUpdate) -> SupportTicket:
    sent = payload.model_fields_set
    ticket = get_platform_ticket(db, ticket_id, lock=True)
    moment = _now()
    changed = status_changed = False

    if "status" in sent:
        if payload.status is None:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="status cannot be null")
        status_changed = _set_status(ticket, payload.status, moment)
        changed |= status_changed
    if "priority" in sent:
        if payload.priority is None:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="priority cannot be null")
        if ticket.priority != payload.priority:
            ticket.priority = payload.priority
            changed = True
    if "assigned_to_user_id" in sent:
        new = payload.assigned_to_user_id
        if new != ticket.assigned_to_user_id:
            if new is not None:
                assignee = db.get(User, new)
                if not is_platform_user(assignee):
                    raise HTTPException(
                        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                        detail="assigned_to_user_id must be an active platform user",
                    )
            if not is_super_admin(actor):
                # A Sales Manager takes a ticket for themselves, or lets go of their own.
                allowed = new == actor.id or (new is None and ticket.assigned_to_user_id == actor.id)
                if not allowed:
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN,
                        detail="Only a Super Admin can assign a ticket to someone else",
                    )
            ticket.assigned_to_user_id = new
            changed = True

    if changed:
        _touch(ticket, moment)
        if status_changed:
            _safe_notify(db, notification_service.notify_ticket_status_changed, ticket, actor.id)
        _commit(db)
    return get_platform_ticket(db, ticket.id)


def platform_message(db: Session, actor: User, ticket_id: str, payload: PlatformMessageCreate) -> SupportTicket:
    """A public reply (open -> in_progress, creator notified) or an internal note (text
    only, notifies nobody, visible to platform users only)."""
    if payload.attachment_file_ids:
        detail = ("Internal notes are text only: attachments are not allowed"
                  if payload.is_internal else "Attachments are not supported on platform replies")
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=detail)
    ticket = get_platform_ticket(db, ticket_id, lock=True)
    moment = _now()
    message = SupportTicketMessage(
        ticket_id=ticket.id, author_user_id=actor.id, author_type="platform",
        body=payload.body, is_internal=payload.is_internal, created_at=moment,
    )
    db.add(message)
    db.flush()
    if not payload.is_internal and ticket.status == "open":
        _set_status(ticket, "in_progress", moment)
    _touch(ticket, moment)
    if not payload.is_internal:
        _safe_notify(db, notification_service.notify_ticket_platform_reply, ticket, message.id, actor.id)
    _commit(db)
    return get_platform_ticket(db, ticket.id)


def summary(db: Session) -> SupportSummaryOut:
    """Aggregates in SQL; no ticket rows are loaded."""
    by_status = {s: 0 for s in TICKET_STATUSES}
    for value, count in db.query(SupportTicket.status, func.count(SupportTicket.id)).group_by(SupportTicket.status):
        by_status[value] = count
    by_priority = {p: 0 for p in TICKET_PRIORITIES}
    for value, count in db.query(SupportTicket.priority, func.count(SupportTicket.id)).group_by(SupportTicket.priority):
        by_priority[value] = count
    active = SupportTicket.status.notin_(TERMINAL)
    unassigned = db.query(func.count(SupportTicket.id)).filter(
        active, SupportTicket.assigned_to_user_id.is_(None)).scalar() or 0

    # D6: latest customer activity = their latest message, else the ticket's creation;
    # it qualifies once older than 24 hours with no public platform reply after it.
    last_customer = func.coalesce(
        select(func.max(SupportTicketMessage.created_at))
        .where(SupportTicketMessage.ticket_id == SupportTicket.id, SupportTicketMessage.author_type == "customer")
        .correlate(SupportTicket).scalar_subquery(),
        SupportTicket.created_at,
    )
    last_public_reply = (
        select(func.max(SupportTicketMessage.created_at))
        .where(SupportTicketMessage.ticket_id == SupportTicket.id, SupportTicketMessage.author_type == "platform",
               SupportTicketMessage.is_internal.is_(False))
        .correlate(SupportTicket).scalar_subquery()
    )
    waiting = db.query(func.count(SupportTicket.id)).filter(
        active,
        last_customer < _now() - NO_REPLY_AFTER,
        or_(last_public_reply.is_(None), last_public_reply < last_customer),
    ).scalar() or 0
    return SupportSummaryOut(
        total=sum(by_status.values()), by_status=by_status, by_priority=by_priority,
        unassigned=unassigned, no_public_reply_over_24h=waiting,
    )
