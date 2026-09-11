import asyncio
from collections import defaultdict
from datetime import datetime, timezone
import json
import logging
from typing import Any, Callable

from fastapi import WebSocket
from sqlalchemy import event
from sqlalchemy.orm import Session

logger = logging.getLogger("crm.realtime")

_main_loop: asyncio.AbstractEventLoop | None = None


def set_main_event_loop(loop: asyncio.AbstractEventLoop) -> None:
    global _main_loop
    _main_loop = loop


class ConnectionManager:
    """In-memory connection manager for WebSocket clients with organization and
    user scoping, permission filtering, and safe cleanup of disconnected sockets."""

    def __init__(self) -> None:
        # connections[org_id][user_id] -> set of active WebSockets
        self.connections: dict[str, dict[str, set[WebSocket]]] = defaultdict(lambda: defaultdict(set))
        # websocket -> (org_id, user_id, permissions_set_or_None_for_admin)
        self.socket_meta: dict[WebSocket, tuple[str, str, set[str] | None]] = {}

    async def connect(
        self,
        websocket: WebSocket,
        org_id: str,
        user_id: str,
        permissions: set[str] | None = None,
    ) -> None:
        await websocket.accept()
        self.connections[org_id][user_id].add(websocket)
        self.socket_meta[websocket] = (org_id, user_id, permissions)
        logger.debug("WS connected: user=%s, org=%s (total sockets: %d)", user_id, org_id, len(self.socket_meta))

    def disconnect(self, websocket: WebSocket, org_id: str | None = None, user_id: str | None = None) -> None:
        meta = self.socket_meta.pop(websocket, None)
        if meta:
            o_id, u_id, _ = meta
            org_id = org_id or o_id
            user_id = user_id or u_id

        if org_id and user_id:
            user_sockets = self.connections.get(org_id, {}).get(user_id)
            if user_sockets:
                user_sockets.discard(websocket)
                if not user_sockets:
                    self.connections[org_id].pop(user_id, None)
            if org_id in self.connections and not self.connections[org_id]:
                self.connections.pop(org_id, None)

        logger.debug("WS disconnected: user=%s, org=%s (remaining sockets: %d)", user_id, org_id, len(self.socket_meta))

    def _has_permission(self, permissions: set[str] | None, required: str | None) -> bool:
        if not required:
            return True
        # Admin or super_admin have None (full access)
        if permissions is None:
            return True
        return required in permissions

    async def _broadcast_org_async(
        self,
        org_id: str,
        event_name: str,
        data: dict[str, Any],
        required_permission: str | None = None,
    ) -> None:
        payload = json.dumps({
            "event": event_name,
            "data": data,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

        dead_sockets: list[tuple[WebSocket, str, str]] = []
        org_conns = self.connections.get(org_id, {})
        for u_id, sockets in list(org_conns.items()):
            for ws in list(sockets):
                meta = self.socket_meta.get(ws)
                perms = meta[2] if meta else None
                if not self._has_permission(perms, required_permission):
                    continue
                try:
                    await ws.send_text(payload)
                except Exception:
                    dead_sockets.append((ws, org_id, u_id))

        for ws, o_id, u_id in dead_sockets:
            self.disconnect(ws, o_id, u_id)

    async def _send_user_async(
        self,
        org_id: str,
        user_id: str,
        event_name: str,
        data: dict[str, Any],
    ) -> None:
        payload = json.dumps({
            "event": event_name,
            "data": data,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

        dead_sockets: list[WebSocket] = []
        sockets = self.connections.get(org_id, {}).get(user_id, set())
        for ws in list(sockets):
            try:
                await ws.send_text(payload)
            except Exception:
                dead_sockets.append(ws)

        for ws in dead_sockets:
            self.disconnect(ws, org_id, user_id)

    def emit(
        self,
        org_id: str,
        event_name: str,
        data: dict[str, Any],
        user_id: str | None = None,
        required_permission: str | None = None,
    ) -> None:
        """Safely schedule an asynchronous event emission across active sockets.
        Never raises exceptions or blocks the calling thread."""
        try:
            if user_id:
                coro = self._send_user_async(org_id, user_id, event_name, data)
            else:
                coro = self._broadcast_org_async(org_id, event_name, data, required_permission)

            try:
                loop = asyncio.get_running_loop()
                loop.create_task(coro)
            except RuntimeError:
                global _main_loop
                if _main_loop and _main_loop.is_running():
                    asyncio.run_coroutine_threadsafe(coro, _main_loop)
                else:
                    # Run in one-off task runner if no persistent loop
                    asyncio.run(coro)
        except Exception as exc:
            logger.warning("Realtime emit failed silently: %s", exc)


manager = ConnectionManager()


def queue_event(
    db: Session,
    org_id: str,
    event_name: str,
    data: dict[str, Any],
    user_id: str | None = None,
    required_permission: str | None = None,
) -> None:
    """Queue a real-time event to be dispatched ONLY AFTER the current DB transaction commits successfully."""
    if not hasattr(db, "info"):
        # Not a standard session; emit immediately
        manager.emit(org_id, event_name, data, user_id, required_permission)
        return

    pending = db.info.setdefault("_pending_realtime_events", [])
    pending.append({
        "org_id": org_id,
        "event": event_name,
        "data": data,
        "user_id": user_id,
        "required_permission": required_permission,
    })


@event.listens_for(Session, "after_commit")
def _on_session_after_commit(session: Session) -> None:
    """Trigger queued realtime events upon successful transaction commit."""
    if not hasattr(session, "info"):
        return
    pending = session.info.pop("_pending_realtime_events", None)
    if pending:
        for item in pending:
            manager.emit(
                org_id=item["org_id"],
                event_name=item["event"],
                data=item["data"],
                user_id=item.get("user_id"),
                required_permission=item.get("required_permission"),
            )


@event.listens_for(Session, "after_rollback")
def _on_session_after_rollback(session: Session) -> None:
    """Discard pending events if the transaction rolls back."""
    if hasattr(session, "info"):
        session.info.pop("_pending_realtime_events", None)
