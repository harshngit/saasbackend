"""Support tickets.

/support            organization users (Admin: the whole organization; Staff: their own)
/platform/support   platform staff (Super Admin, Sales Manager)

The organization routes deliberately take no require_unlocked_org / plan entitlement:
a locked or restricted organization must still be able to ask for help (D8). They
still require a logged-in organization user.
"""

from datetime import date

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import get_current_user
from app.models import User
from app.routers.platform import _platform_guard
from app.schemas.pagination import PaginatedResponse
from app.schemas.support import (
    PlatformMessageCreate,
    PlatformTicketListItem,
    PlatformTicketOut,
    PlatformTicketUpdate,
    SupportSummaryOut,
    TicketCategory,
    TicketCreate,
    TicketListItem,
    TicketMessageCreate,
    TicketOut,
    TicketPriority,
    TicketStatus,
)
from app.services import support_service

_E401 = {401: {"description": "Not logged in"}}
_E403 = {403: {"description": "Not allowed (e.g. a platform account on the organization routes)"}}
_E404 = {404: {"description": "No such ticket visible to you (another organization's tickets included)"}}
_E409 = {409: {"description": "Not allowed in the ticket's current state"}}
_E400_FILES = {400: {"description": "An attachment is not your uploaded image/PDF file"}}

org_router = APIRouter(prefix="/support", tags=["support"])


def _org_user(user: User = Depends(get_current_user)) -> User:
    support_service.require_org_user(user)
    return user


@org_router.post(
    "/tickets", response_model=TicketOut, status_code=status.HTTP_201_CREATED,
    responses={**_E400_FILES, **_E401, **_E403,
               409: {"description": "An attachment is already attached to a ticket"},
               429: {"description": "10 tickets in the last 24 hours for this organization"}},
)
def create_ticket(payload: TicketCreate, user: User = Depends(_org_user), db: Session = Depends(get_db)) -> TicketOut:
    """Raise a ticket. Upload attachments first with POST /files/upload and send their `file_id`s."""
    return support_service.org_ticket_out(support_service.create_ticket(db, user, payload))


@org_router.get("/tickets", response_model=PaginatedResponse[TicketListItem], responses={**_E401, **_E403})
def list_tickets(
    status_filter: TicketStatus | None = Query(default=None, alias="status"),
    category: TicketCategory | None = Query(default=None),
    priority: TicketPriority | None = Query(default=None),
    search: str | None = Query(default=None, description="Ticket number or subject (contains, case-insensitive)"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    user: User = Depends(_org_user),
    db: Session = Depends(get_db),
) -> PaginatedResponse[TicketListItem]:
    """Most recently active first. Admin: every ticket of the organization; Staff: their own."""
    return support_service.list_org_tickets(
        db, user, status_filter=status_filter, category=category, priority=priority,
        search=search, page=page, page_size=page_size,
    )


@org_router.get("/tickets/{ticket_id}", response_model=TicketOut, responses={**_E401, **_E403, **_E404})
def get_ticket(ticket_id: str, user: User = Depends(_org_user), db: Session = Depends(get_db)) -> TicketOut:
    """The ticket with its public conversation (internal notes are never included)."""
    return support_service.org_ticket_out(support_service.get_org_ticket(db, user, ticket_id))


@org_router.post(
    "/tickets/{ticket_id}/messages", response_model=TicketOut, status_code=status.HTTP_201_CREATED,
    responses={**_E400_FILES, **_E401, **_E403, **_E404,
               409: {"description": "Ticket closed, or resolved more than 7 days ago "
                                    "(\"Reopen window expired, please raise a new ticket.\")"}},
)
def reply(ticket_id: str, payload: TicketMessageCreate, user: User = Depends(_org_user),
          db: Session = Depends(get_db)) -> TicketOut:
    """Reply. A ticket waiting on you, or resolved within the last 7 days, goes back to open."""
    return support_service.org_ticket_out(support_service.customer_reply(db, user, ticket_id, payload))


@org_router.post("/tickets/{ticket_id}/close", response_model=TicketOut,
                 responses={**_E401, **_E403, **_E404, **_E409})
def close(ticket_id: str, user: User = Depends(_org_user), db: Session = Depends(get_db)) -> TicketOut:
    return support_service.org_ticket_out(support_service.close_ticket(db, user, ticket_id))


@org_router.post(
    "/tickets/{ticket_id}/reopen", response_model=TicketOut,
    responses={**_E401, **_E403, **_E404,
               409: {"description": "Not resolved/closed, or \"Reopen window expired, please raise a new ticket.\""}},
)
def reopen(ticket_id: str, user: User = Depends(_org_user), db: Session = Depends(get_db)) -> TicketOut:
    """Reopen a resolved or closed ticket within 7 days of when it was closed (or resolved)."""
    return support_service.org_ticket_out(support_service.reopen_ticket(db, user, ticket_id))


# ------------------------------------------------------------------ platform

platform_router = APIRouter(
    prefix="/platform/support", tags=["platform-support"], dependencies=[Depends(_platform_guard)]
)


def _platform_user(user: User = Depends(_platform_guard)) -> User:
    # Besides the role guard: no organization, and the stored platform role itself.
    return support_service.require_platform_user(user)


@platform_router.get("/summary", response_model=SupportSummaryOut, responses={**_E401, **_E403})
def summary(_: User = Depends(_platform_user), db: Session = Depends(get_db)) -> SupportSummaryOut:
    return support_service.summary(db)


@platform_router.get("/tickets", response_model=PaginatedResponse[PlatformTicketListItem],
                     responses={**_E401, **_E403})
def platform_list(
    organization_id: str | None = Query(default=None),
    status_filter: TicketStatus | None = Query(default=None, alias="status"),
    priority: TicketPriority | None = Query(default=None),
    category: TicketCategory | None = Query(default=None),
    assigned_to_user_id: str | None = Query(default=None),
    assigned_to_me: bool = Query(default=False),
    unassigned: bool = Query(default=False),
    search: str | None = Query(default=None, description="Ticket number or subject"),
    date_from: date | None = Query(default=None, description="Created on/after (YYYY-MM-DD, Asia/Kolkata)"),
    date_to: date | None = Query(default=None, description="Created on/before (YYYY-MM-DD, Asia/Kolkata)"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    user: User = Depends(_platform_user),
    db: Session = Depends(get_db),
) -> PaginatedResponse[PlatformTicketListItem]:
    """Every organization's tickets, most recently active first."""
    return support_service.list_platform_tickets(
        db, user, organization_id=organization_id, status_filter=status_filter, priority=priority,
        category=category, assigned_to_user_id=assigned_to_user_id, assigned_to_me=assigned_to_me,
        unassigned=unassigned, search=search, date_from=date_from, date_to=date_to,
        page=page, page_size=page_size,
    )


@platform_router.get("/tickets/{ticket_id}", response_model=PlatformTicketOut,
                     responses={**_E401, **_E403, **_E404})
def platform_get(ticket_id: str, _: User = Depends(_platform_user), db: Session = Depends(get_db)) -> PlatformTicketOut:
    """The ticket with every message, internal notes included."""
    return support_service.platform_ticket_out(support_service.get_platform_ticket(db, ticket_id))


@platform_router.patch("/tickets/{ticket_id}", response_model=PlatformTicketOut,
                       responses={**_E401, **_E403, **_E404,
                                  422: {"description": "Invalid status / priority, or assignee not a platform user"}})
def platform_update(ticket_id: str, payload: PlatformTicketUpdate, user: User = Depends(_platform_user),
                    db: Session = Depends(get_db)) -> PlatformTicketOut:
    """Change status, priority and/or assignee. A Sales Manager may only assign themselves."""
    return support_service.platform_ticket_out(support_service.update_ticket(db, user, ticket_id, payload))


@platform_router.post("/tickets/{ticket_id}/messages", response_model=PlatformTicketOut,
                      status_code=status.HTTP_201_CREATED,
                      responses={**_E401, **_E403, **_E404,
                                 422: {"description": "Attachments sent (not supported on platform messages)"}})
def platform_reply(ticket_id: str, payload: PlatformMessageCreate, user: User = Depends(_platform_user),
                   db: Session = Depends(get_db)) -> PlatformTicketOut:
    """A public reply (an open ticket moves to in_progress; the creator is notified) or an
    internal note (`is_internal: true`: platform-only, text only, notifies nobody)."""
    return support_service.platform_ticket_out(support_service.platform_message(db, user, ticket_id, payload))
