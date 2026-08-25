import logging
import secrets
import urllib.parse
from datetime import datetime, timedelta, timezone
from typing import Any

import jwt
import requests as http_requests
from google.auth.transport import requests as google_transport_requests
from google.oauth2 import id_token

from app.core.config import settings

logger = logging.getLogger("crm.auth.google")

_GOOGLE_ISSUERS = ("accounts.google.com", "https://accounts.google.com")
_STATE_TOKEN_TYPE = "oauth_state"


class GoogleAuthError(Exception):
    """Base exception for Google token and OAuth redirect verification failures."""

    def __init__(self, message: str, status_code: int = 401) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def generate_oauth_state() -> str:
    """Generate a signed, tamper-proof state token with a 10-minute expiration."""
    now = datetime.now(timezone.utc)
    payload = {
        "nonce": secrets.token_urlsafe(16),
        "type": _STATE_TOKEN_TYPE,
        "iat": now,
        "exp": now + timedelta(minutes=10),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm="HS256")


def verify_oauth_state(state: str | None) -> dict[str, Any]:
    """Validate that the OAuth state parameter is present, signed with our secret, and unexpired."""
    if not state or not state.strip():
        raise GoogleAuthError("Missing OAuth state parameter", status_code=400)

    try:
        claims = jwt.decode(state, settings.jwt_secret, algorithms=["HS256"])
    except jwt.ExpiredSignatureError as exc:
        raise GoogleAuthError("OAuth state parameter has expired", status_code=400) from exc
    except jwt.PyJWTError as exc:
        raise GoogleAuthError("Invalid or tampered OAuth state parameter", status_code=400) from exc

    if claims.get("type") != _STATE_TOKEN_TYPE:
        raise GoogleAuthError("Invalid OAuth state token", status_code=400)

    return claims


def build_google_authorization_url(state: str) -> str:
    """Build Google's OAuth 2.0 authorization URL with required scopes and state."""
    if not settings.google_client_id:
        raise GoogleAuthError("Google OAuth is not configured (missing GOOGLE_CLIENT_ID)", status_code=503)

    params = {
        "response_type": "code",
        "client_id": settings.google_client_id,
        "redirect_uri": settings.google_redirect_uri,
        "scope": "openid email profile",
        "state": state,
        "access_type": "offline",
        "prompt": "select_account",
    }
    return f"https://accounts.google.com/o/oauth2/v2/auth?{urllib.parse.urlencode(params)}"


def exchange_authorization_code(code: str) -> dict[str, Any]:
    """Exchange an authorization code with Google and cryptographically verify the returned ID token.

    Never logs secrets or sensitive token values.
    """
    if not settings.google_client_id or not settings.google_client_secret:
        raise GoogleAuthError("Google OAuth is not configured on this server", status_code=503)

    token_endpoint = "https://oauth2.googleapis.com/token"
    data = {
        "code": code,
        "client_id": settings.google_client_id,
        "client_secret": settings.google_client_secret,
        "redirect_uri": settings.google_redirect_uri,
        "grant_type": "authorization_code",
    }

    try:
        response = http_requests.post(token_endpoint, data=data, timeout=10)
    except Exception as exc:
        logger.warning("Failed to connect to Google token endpoint: %s", exc)
        raise GoogleAuthError("Failed to reach Google token endpoint", status_code=502) from exc

    if response.status_code != 200:
        logger.warning("Google token exchange returned HTTP %s", response.status_code)
        raise GoogleAuthError("Failed to exchange authorization code with Google", status_code=400)

    token_data = response.json()
    raw_id_token = token_data.get("id_token")
    if not raw_id_token:
        raise GoogleAuthError("Google token response did not contain an ID token", status_code=400)

    return verify_google_id_token(raw_id_token, client_id=settings.google_client_id)


def verify_google_id_token(credential: str, client_id: str | None = None) -> dict[str, Any]:
    """Cryptographically verify a Google ID Token using google-auth.

    Validates:
    - Signature via Google public certificates (handled by google-auth)
    - Expiration (handled by google-auth)
    - Audience (`aud`) against settings.google_client_id (if configured)
    - Issuer (`iss`) is Google
    - `email_verified` is True
    - Required claims (`sub`, `email`) are present

    Returns the verified identity dictionary containing `sub`, `email`, `name`, etc.
    Raises GoogleAuthError on any verification failure.
    """
    if not credential or not credential.strip():
        raise GoogleAuthError("Missing Google ID token credential", status_code=400)

    target_audience = client_id if client_id is not None else (settings.google_client_id or None)

    try:
        id_info = id_token.verify_oauth2_token(
            id_token=credential,
            request=google_transport_requests.Request(),
            audience=target_audience,
        )
    except Exception as exc:
        logger.warning("Google ID token verification failed: %s", exc)
        raise GoogleAuthError(f"Invalid or expired Google token: {exc}", status_code=401) from exc

    issuer = id_info.get("iss")
    if issuer not in _GOOGLE_ISSUERS:
        logger.warning("Google ID token issuer invalid: %s", issuer)
        raise GoogleAuthError(f"Invalid Google token issuer: {issuer}", status_code=401)

    # Ensure email is verified by Google to prevent account takeover
    email_verified = id_info.get("email_verified")
    if email_verified is not True and str(email_verified).lower() != "true":
        logger.warning("Google account email is not verified: %s", id_info.get("email"))
        raise GoogleAuthError("Google email is not verified", status_code=401)

    sub = id_info.get("sub")
    email = id_info.get("email")
    if not sub or not email:
        raise GoogleAuthError("Google token missing required identity claims", status_code=401)

    return {
        "sub": str(sub),
        "email": str(email).strip().lower(),
        "name": id_info.get("name") or id_info.get("given_name") or str(email),
        "picture": id_info.get("picture"),
        "id_info": id_info,
    }
