"""Firebase Admin SDK setup for push notifications (FCM).

Credentials come ONLY from the FIREBASE_SERVICE_ACCOUNT_JSON runtime secret and are handed
to the SDK as an explicit `credentials.Certificate`. Application Default Credentials, the
metadata server and any other implicit discovery are never used: `initialize_app` is always
called with that certificate, under this module's own named app.

Nothing here runs at import or startup. The app is created on first use, once per process;
when push is disabled or misconfigured, callers get a FirebaseConfigError with an actionable
message (never the key itself) and the rest of the API keeps working.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import threading

from app.core.config import settings

logger = logging.getLogger("crm.push")

APP_NAME = "crm-push"
REQUIRED_FIELDS = ("type", "project_id", "private_key_id", "private_key", "client_email", "token_uri")

_lock = threading.Lock()
_app = None


class FirebaseConfigError(RuntimeError):
    """Push is disabled, or the Firebase configuration is missing or invalid."""


def parse_service_account(raw: str, expected_project_id: str | None = None) -> dict:
    """Validate the service-account JSON (raw JSON or base64 of it) and return it as a dict.

    Error messages name the problem, never the key material."""
    text = (raw or "").strip()
    if not text:
        raise FirebaseConfigError("FIREBASE_SERVICE_ACCOUNT_JSON is not set")
    try:
        info = json.loads(text)
    except ValueError:
        try:
            info = json.loads(base64.b64decode(text, validate=True).decode("utf-8"))
        except (ValueError, binascii.Error, UnicodeDecodeError):
            raise FirebaseConfigError(
                "FIREBASE_SERVICE_ACCOUNT_JSON is neither valid JSON nor base64-encoded JSON"
            ) from None
    if not isinstance(info, dict):
        raise FirebaseConfigError("FIREBASE_SERVICE_ACCOUNT_JSON must be a JSON object")
    missing = [field for field in REQUIRED_FIELDS if not str(info.get(field) or "").strip()]
    if missing:
        raise FirebaseConfigError(f"FIREBASE_SERVICE_ACCOUNT_JSON is missing: {', '.join(missing)}")
    if info["type"] != "service_account":
        raise FirebaseConfigError("FIREBASE_SERVICE_ACCOUNT_JSON must be a service-account key (type 'service_account')")
    # A key pasted into a single-line secret often arrives with literal "\n" sequences.
    if "\\n" in info["private_key"] and "\n" not in info["private_key"]:
        info = {**info, "private_key": info["private_key"].replace("\\n", "\n")}
    if "BEGIN PRIVATE KEY" not in info["private_key"]:
        raise FirebaseConfigError("FIREBASE_SERVICE_ACCOUNT_JSON private_key is not a PEM private key")
    expected = (expected_project_id or "").strip()
    if expected and expected != info["project_id"]:
        raise FirebaseConfigError(
            f"FIREBASE_PROJECT_ID ({expected}) does not match the service account's project_id ({info['project_id']})"
        )
    return info


def push_status() -> dict:
    """Configuration state for diagnostics -- contains no secret material."""
    if not settings.push_notifications_enabled:
        return {"enabled": False, "configured": False, "project_id": None, "error": None}
    try:
        info = parse_service_account(settings.firebase_service_account_json, settings.firebase_project_id)
    except FirebaseConfigError as exc:
        return {"enabled": True, "configured": False, "project_id": None, "error": str(exc)}
    return {"enabled": True, "configured": True, "project_id": info["project_id"], "error": None}


def get_app():
    """The process-wide Firebase app, created on first use from the explicit certificate."""
    global _app
    if _app is not None:
        return _app
    if not settings.push_notifications_enabled:
        raise FirebaseConfigError("Push notifications are disabled (PUSH_NOTIFICATIONS_ENABLED is not true)")
    info = parse_service_account(settings.firebase_service_account_json, settings.firebase_project_id)

    try:
        import firebase_admin
        from firebase_admin import credentials
    except ImportError:
        raise FirebaseConfigError(
            "Push notifications are enabled but the firebase-admin package is not installed"
        ) from None

    with _lock:
        if _app is not None:
            return _app
        try:
            _app = firebase_admin.get_app(APP_NAME)  # already initialised in this process
        except ValueError:
            try:
                cred = credentials.Certificate(info)
            except (ValueError, TypeError) as exc:
                raise FirebaseConfigError(
                    f"FIREBASE_SERVICE_ACCOUNT_JSON could not be loaded as a certificate ({type(exc).__name__})"
                ) from None
            _app = firebase_admin.initialize_app(cred, {"projectId": info["project_id"]}, name=APP_NAME)
            logger.info("Firebase push initialised for project %s", info["project_id"])
    return _app


def reset_for_tests() -> None:
    """Drop the cached app (tests only)."""
    global _app
    with _lock:
        try:
            import firebase_admin

            firebase_admin.delete_app(firebase_admin.get_app(APP_NAME))
        except (ImportError, ValueError):
            pass
        _app = None
