from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect, status
from sqlalchemy.orm import Session

from app.core.database import SessionLocal
from app.core.realtime import manager
from app.core.security import ACCESS_TOKEN, decode_token
from app.models import Role, SystemRole, User
from app.services import org_service

router = APIRouter(tags=["websocket"])


@router.websocket("/ws")
async def websocket_endpoint(
    websocket: WebSocket,
    token: str | None = Query(default=None),
) -> None:
    """Central WebSocket endpoint for authenticated real-time updates.

    Authentication:
        Pass the active Bearer JWT token in the query parameter: `?token=<access_token>`

    Lifecycle:
        - Decodes and validates the access token using existing JWT configuration.
        - Enforces active user status, organization membership, and trial/unlocked status.
        - Resolves user permissions for permission-aware event filtering.
        - Handles incoming text messages ('ping' -> 'pong' heartbeat).
        - Automatically cleans up disconnected sockets.
    """
    if not token:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="Missing token")
        return

    try:
        payload = decode_token(token)
    except Exception:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="Invalid token")
        return

    if payload.get("type") != ACCESS_TOKEN:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="Invalid token type")
        return

    user_id = payload.get("sub")
    if not user_id:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="Missing user ID")
        return

    db: Session = SessionLocal()
    try:
        user = db.get(User, user_id)
        if user is None or not user.is_active:
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="Inactive or unknown user")
            return

        org_id = user.organization_id
        if not org_id and user.effective_system_role != SystemRole.SUPER_ADMIN.value:
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="No organization assigned")
            return

        # Check unlocked organization status for non-superadmin
        if user.effective_system_role != SystemRole.SUPER_ADMIN.value and user.organization:
            org = org_service.apply_trial_expiry(db, user.organization)
            if org and org.status in ("locked", "suspended"):
                await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="Organization locked")
                return

        # Resolve permissions
        permissions: set[str] | None = None
        if user.effective_system_role not in (SystemRole.ADMIN.value, SystemRole.SUPER_ADMIN.value):
            role = db.get(Role, user.role_id) if user.role_id else None
            permissions = set()
            if role and role.permissions:
                for mod, actions in role.permissions.items():
                    if isinstance(actions, dict):
                        for act, allowed in actions.items():
                            if allowed:
                                permissions.add(f"{mod}:{act}")
    finally:
        db.close()

    resolved_org_id = org_id or "global"
    await manager.connect(
        websocket=websocket,
        org_id=resolved_org_id,
        user_id=user_id,
        permissions=permissions,
    )

    try:
        while True:
            data = await websocket.receive_text()
            if data == "ping":
                await websocket.send_text("pong")
    except WebSocketDisconnect:
        manager.disconnect(websocket, org_id=resolved_org_id, user_id=user_id)
    except Exception:
        manager.disconnect(websocket, org_id=resolved_org_id, user_id=user_id)
