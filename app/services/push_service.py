"""Push notifications over Firebase Cloud Messaging, for the web app and the Android app.

Backend-only API: callers pass a User the request is already authorised to notify (normally
the signed-in user, or a recipient resolved by business logic inside the same organization).
There is no "send to any user id" entry point and no broadcast.

Delivery problems do not raise: a send returns a PushResult. Push disabled, Firebase
misconfigured or the firebase-admin package missing -> `enabled=False` with the reason in
`detail`; a whole-batch Firebase/network failure -> counted in `transient_failures`. Tokens
Firebase reports as unregistered / not belonging to this project are deleted; transient
failures (network, quota, Firebase unavailable) leave the token in place.

What does raise: PushPayloadError for content the caller built wrongly (empty title, reserved
or non-scalar data keys, oversized data) -- a programming error, raised before anything is
sent -- and database errors from the caller's session.

"accepted" means Firebase accepted the message for delivery. It does not mean the device
received or displayed it (the device may be offline, notifications may be blocked, or the
app may handle the message silently).
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.firebase import FirebaseConfigError, get_app
from app.models import Notification, PushDevice, User

logger = logging.getLogger("crm.push")

FCM_BATCH_LIMIT = 500             # send_each accepts at most 500 messages per call
MAX_DEVICES_PER_USER = 20         # keeps "all of a user's devices" a small, bounded set
_RESERVED_DATA_KEYS = {"from", "notification", "message_type"}
_RESERVED_DATA_PREFIXES = ("google.", "gcm.")
_MAX_DATA_BYTES = 3500            # FCM caps the whole data payload at 4KB


class PushTokenConflict(Exception):
    """The token is registered in a different organization; it is not moved across firms."""


class PushPayloadError(ValueError):
    """The notification content cannot be sent as given."""


@dataclass
class PushResult:
    enabled: bool = True
    attempted: int = 0
    accepted: int = 0
    invalid_tokens_removed: int = 0
    transient_failures: int = 0
    detail: str | None = None
    message_ids: list[str] = field(default_factory=list)


def _suffix(token: str) -> str:
    return f"...{token[-6:]}"


def _clean_data(data: dict | None) -> dict[str, str]:
    """FCM data must be a flat string->string map without reserved keys."""
    if not data:
        return {}
    cleaned: dict[str, str] = {}
    for key, value in data.items():
        key = str(key)
        if not key or key in _RESERVED_DATA_KEYS or key.startswith(_RESERVED_DATA_PREFIXES):
            raise PushPayloadError(f"data key '{key}' is reserved by FCM")
        if value is None:
            continue
        if not isinstance(value, (str, int, float, bool)):
            raise PushPayloadError(f"data value for '{key}' must be a string, number or boolean")
        cleaned[key] = str(value).lower() if isinstance(value, bool) else str(value)
    if sum(len(k) + len(v) for k, v in cleaned.items()) > _MAX_DATA_BYTES:
        raise PushPayloadError("data payload is too large for FCM")
    return cleaned


def build_message(device: PushDevice, title: str, body: str | None = None, data: dict | None = None,
                  link: str | None = None):
    """One FCM message for one registered device.

    - data: string map the client app reads for navigation (e.g. {"type": "order", "id": "..."}).
    - link: for web only -- an absolute https URL the browser opens on click. Anything else is
      ignored, since FCM rejects non-https web links. No domain is hard-coded here.
    """
    from firebase_admin import messaging

    title = (title or "").strip()
    if not title:
        raise PushPayloadError("title is required")
    payload = _clean_data(data)
    notification = messaging.Notification(title=title[:200], body=(body or None) and body[:1000])
    kwargs = {"token": device.token, "notification": notification, "data": payload or None}
    if device.platform == "android":
        kwargs["android"] = messaging.AndroidConfig(priority="high")
    elif device.platform == "ios":
        # APNs alert + default sound only; no Android options. No badge: the backend has no
        # agreed per-device badge count, and a wrong badge is worse than none.
        kwargs["apns"] = messaging.APNSConfig(
            payload=messaging.APNSPayload(
                aps=messaging.Aps(alert=messaging.ApsAlert(title=notification.title, body=notification.body),
                                  sound="default")
            )
        )
    elif device.platform == "web":
        fcm_options = messaging.WebpushFCMOptions(link=link) if link and link.startswith("https://") else None
        kwargs["webpush"] = messaging.WebpushConfig(fcm_options=fcm_options)
    return messaging.Message(**kwargs)


def _is_invalid_token(exc: Exception) -> bool:
    """True only when Firebase says this token is dead or belongs to another project."""
    from firebase_admin import exceptions, messaging

    if isinstance(exc, (messaging.UnregisteredError, messaging.SenderIdMismatchError)):
        return True
    if isinstance(exc, exceptions.InvalidArgumentError):
        return "registration token" in str(exc).lower()
    return False


def send_to_devices(db: Session, devices: list[PushDevice], title: str, body: str | None = None,
                    data: dict | None = None, link: str | None = None) -> PushResult:
    """Send one notification to the given registrations. Invalid tokens are deleted (flushed,
    not committed -- the caller owns the transaction)."""
    try:
        app = get_app()
    except FirebaseConfigError as exc:
        logger.warning("Push not sent: %s", exc)
        return PushResult(enabled=False, detail=str(exc))
    if not devices:
        return PushResult(detail="no registered devices")

    from firebase_admin import exceptions, messaging

    messages = [build_message(d, title, body, data, link) for d in devices]
    result = PushResult(attempted=len(devices))
    for start in range(0, len(messages), FCM_BATCH_LIMIT):
        batch_devices = devices[start:start + FCM_BATCH_LIMIT]
        try:
            response = messaging.send_each(messages[start:start + FCM_BATCH_LIMIT], app=app)
        except exceptions.FirebaseError as exc:
            # The whole call failed (auth, network, Firebase outage): keep every token.
            result.transient_failures += len(batch_devices)
            logger.warning("Push batch failed (%s); %d tokens kept", type(exc).__name__, len(batch_devices))
            continue
        for device, item in zip(batch_devices, response.responses):
            if item.success:
                result.accepted += 1
                if item.message_id:
                    result.message_ids.append(item.message_id)
            elif _is_invalid_token(item.exception):
                logger.info("Removing invalid push token %s (%s)", _suffix(device.token), type(item.exception).__name__)
                db.delete(device)
                result.invalid_tokens_removed += 1
            else:
                result.transient_failures += 1
                logger.warning("Push to %s failed (%s); token kept", _suffix(device.token),
                               type(item.exception).__name__)
    db.flush()
    return result


def devices_for_user(db: Session, user: User) -> list[PushDevice]:
    """The user's own registrations, inside the user's own organization only."""
    query = db.query(PushDevice).filter(PushDevice.user_id == user.id)
    if user.organization_id:
        query = query.filter(PushDevice.organization_id == user.organization_id)
    else:
        query = query.filter(PushDevice.organization_id.is_(None))
    return query.order_by(PushDevice.last_seen_at.desc()).limit(MAX_DEVICES_PER_USER).all()


def send_to_user(db: Session, user: User, title: str, body: str | None = None,
                 data: dict | None = None, link: str | None = None) -> PushResult:
    """Send to every device the user has registered (at most MAX_DEVICES_PER_USER)."""
    return send_to_devices(db, devices_for_user(db, user), title, body, data, link)


def _device_for_token(db: Session, token: str) -> PushDevice | None:
    return db.query(PushDevice).filter(PushDevice.token == token).first()


def register_device(db: Session, user: User, token: str, platform: str, label: str | None) -> tuple[PushDevice, bool]:
    """Create or refresh the registration for this token, owned by `user`. Returns (device, created).

    An existing row for the same token is taken over when it belongs to the same organization
    (another user signed in on that device). A row in a different organization is never moved:
    PushTokenConflict is raised and the row is left untouched -- the device's previous user must
    unregister it first, or the client must obtain a fresh FCM token. Keeps each user to
    MAX_DEVICES_PER_USER, dropping the least recently seen."""
    now = datetime.now(timezone.utc)
    device = _device_for_token(db, token)
    created = False
    if device is None:
        candidate = PushDevice(token=token, user_id=user.id, organization_id=user.organization_id,
                               platform=platform, label=label, last_seen_at=now)
        try:
            with db.begin_nested():  # a savepoint: a lost race rolls back only this insert
                db.add(candidate)
                db.flush()
            device, created = candidate, True
        except IntegrityError:
            # A concurrent request registered the same token first. Refresh that row instead;
            # if no such row exists, the failure was something else and is re-raised.
            device = _device_for_token(db, token)
            if device is None:
                raise
    if not created:
        if device.organization_id != user.organization_id:
            # Checked on both paths -- the first lookup and the re-read after a lost race --
            # before anything on the row is changed.
            logger.info("Push token %s is registered in another organization; not moved", _suffix(token))
            raise PushTokenConflict()
        if device.user_id != user.id:
            logger.info("Push token %s moved to a different signed-in user", _suffix(token))
        device.user_id = user.id
        device.organization_id = user.organization_id
        device.platform = platform
        device.label = label if label is not None else device.label
        device.last_seen_at = now
    db.flush()

    stale = (
        db.query(PushDevice)
        .filter(PushDevice.user_id == user.id, PushDevice.id != device.id)
        .order_by(PushDevice.last_seen_at.desc())
        .offset(MAX_DEVICES_PER_USER - 1)
        .all()
    )
    for old in stale:
        db.delete(old)
    return device, created


# --------------------------------------------------------------------------------------------
# Push for in-app notifications: sent only after the notification's transaction commits.
#
# notification_service.notify() calls queue_notification_push(); the id waits in session.info
# until COMMIT, then a small in-process worker pool sends it with its own session (SQL is not
# allowed on the committing session inside after_commit, and a Firebase call must not block the
# request). A rollback discards the queue, so nothing is pushed for a failed operation. Each
# committed notification is dispatched exactly once; a push failure never touches the in-app row.
# No new infrastructure: if the process exits mid-send that push is lost, the in-app record stays.
# --------------------------------------------------------------------------------------------

_PENDING_KEY = "_pending_push_notifications"
_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="push")


def queue_notification_push(db: Session, notification: Notification) -> None:
    """Schedule a push for this (not yet committed) notification. No-op when push is off."""
    if not settings.push_notifications_enabled or not hasattr(db, "info"):
        return
    pending = db.info.setdefault(_PENDING_KEY, [])
    if notification.id not in pending:
        pending.append(notification.id)


def push_data(notification: Notification) -> dict[str, str]:
    """The FCM data map for an in-app notification: its id, the event as `type`, the in-app
    category, the organization and the event's identifiers -- all strings, nothing sensitive."""
    stored = dict(notification.data or {})
    event_name = stored.pop("event", None)
    data = {
        "notification_id": notification.id,
        "type": event_name or notification.type,
        "category": notification.type,
        "organization_id": notification.organization_id or "",
        **{k: str(v) for k, v in stored.items() if v is not None},
    }
    if notification.link:
        data["link"] = notification.link
    return {k: v for k, v in data.items() if v != ""}


def deliver_notification_pushes(notification_ids: list[str]) -> list[PushResult]:
    """Send the pushes for committed notifications, with a fresh session. Failures are logged
    (no tokens, no payloads) and never raised -- the in-app notifications already exist."""
    from app.core.database import SessionLocal

    results = []
    db = SessionLocal()
    try:
        for notification_id in notification_ids:
            notification = db.get(Notification, notification_id)
            user = db.get(User, notification.user_id) if notification else None
            if notification is None or user is None or not user.is_active:
                continue
            try:
                result = send_to_user(db, user, notification.title, notification.body, push_data(notification))
                db.commit()  # persist removal of tokens Firebase reported as invalid
            except Exception as exc:  # noqa: BLE001 -- isolate one notification from the next
                db.rollback()
                logger.warning("Push for notification %s failed (%s)", notification_id, type(exc).__name__)
                continue
            results.append(result)
            if result.attempted:
                logger.info(
                    "Push for notification %s: %d accepted, %d invalid removed, %d transient failures",
                    notification_id, result.accepted, result.invalid_tokens_removed, result.transient_failures,
                )
    finally:
        db.close()
    return results


def _dispatch(notification_ids: list[str]) -> None:
    """Hand committed notifications to the worker pool (tests replace this to run inline)."""
    _executor.submit(deliver_notification_pushes, notification_ids)


@event.listens_for(Session, "after_commit")
def _send_queued_pushes(session: Session) -> None:
    pending = session.info.pop(_PENDING_KEY, None)
    if pending:
        try:
            _dispatch(list(pending))
        except Exception as exc:  # noqa: BLE001 -- never let push break a committed request
            logger.warning("Could not schedule %d pushes (%s)", len(pending), type(exc).__name__)


@event.listens_for(Session, "after_rollback")
def _drop_queued_pushes(session: Session) -> None:
    session.info.pop(_PENDING_KEY, None)
