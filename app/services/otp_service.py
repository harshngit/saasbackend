"""Staff mobile OTP login: Twilio Verify calls.

Twilio Verify generates, sends, expires and checks the code -- this backend never sees
or stores it beyond passing the user's attempt through. Calls go to the Verify v2 REST
API with `requests`, the same way google_auth_service talks to Google.

Provider errors are mapped to the agreed statuses and sanitized: Twilio's own messages,
credentials, the code and the full phone number are never logged or returned.
"""
import logging

import requests as http_requests

from app.core.config import settings
from app.core.phone import mask

logger = logging.getLogger("crm.otp")

_VERIFY_BASE = "https://verify.twilio.com/v2/Services"
_TIMEOUT_SECONDS = 10
# Twilio error codes with an agreed mapping.
_MAX_SEND_ATTEMPTS = 60203  # -> 429
_INVALID_PARAMETER = 60200  # -> 400

UNAVAILABLE = "Phone login is temporarily unavailable"


class OtpError(Exception):
    """OTP login cannot proceed. `status_code` is what the endpoint answers with."""

    def __init__(self, message: str, status_code: int) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def require_configured() -> None:
    """Fail closed: no Twilio settings, no OTP login (other sign-in methods are unaffected)."""
    if not settings.twilio_configured:
        raise OtpError("Phone login not configured", status_code=503)


def _post(path: str, data: dict) -> http_requests.Response:
    url = f"{_VERIFY_BASE}/{settings.twilio_verify_service_sid.strip()}/{path}"
    try:
        return http_requests.post(
            url,
            data=data,
            auth=(settings.twilio_account_sid.strip(), settings.twilio_auth_token.strip()),
            timeout=_TIMEOUT_SECONDS,
        )
    except http_requests.RequestException as exc:
        logger.warning("Twilio Verify %s request failed: %s", path, type(exc).__name__)
        raise OtpError(UNAVAILABLE, status_code=502) from exc


def _json(response: http_requests.Response) -> dict:
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def _provider_error(path: str, e164: str, response: http_requests.Response, body: dict) -> OtpError:
    code = body.get("code")
    logger.warning("Twilio Verify %s for %s failed: HTTP %s (code %s)", path, mask(e164), response.status_code, code)
    if code == _MAX_SEND_ATTEMPTS:
        return OtpError("Too many OTP requests. Please try again later.", status_code=429)
    if code == _INVALID_PARAMETER:
        return OtpError("Invalid phone number or OTP", status_code=400)
    return OtpError(UNAVAILABLE, status_code=502)


def start_verification(e164: str) -> None:
    """Ask Twilio Verify to text a code to `e164`. Raises OtpError on any failure."""
    response = _post("Verifications", {"To": e164, "Channel": "sms"})
    body = _json(response)
    if response.status_code in (200, 201) and body.get("status") == "pending":
        return
    raise _provider_error("Verifications", e164, response, body)


def check_verification(e164: str, code: str) -> bool:
    """True only when Twilio approves `code` for this very number.

    False for a wrong, expired, already-used or over-attempted code (Twilio answers 200
    "pending", or 404 / 60200 / 60202 for those). Raises OtpError for 60203 (429) and for
    any other provider or network failure (502).
    """
    response = _post("VerificationCheck", {"To": e164, "Code": code})
    body = _json(response)
    if response.status_code == 200:
        return body.get("status") == "approved" and body.get("valid") is True and body.get("to") in (None, e164)
    if response.status_code == 404 or body.get("code") in (_INVALID_PARAMETER, 60202, 20404):
        return False
    raise _provider_error("VerificationCheck", e164, response, body)
