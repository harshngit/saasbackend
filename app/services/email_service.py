"""Outgoing email: SMTP or an HTTPS email API (Resend / Brevo), with masked diagnostics.

Delivery mode comes from settings:
- EMAIL_PROVIDER=resend|brevo  -> "api": an HTTPS call with EMAIL_API_KEY (no SMTP port needed)
- otherwise, SMTP_HOST set     -> "smtp": port 465 = implicit TLS (SMTP_SSL), else STARTTLS
                                  when SMTP_USE_TLS
- otherwise                    -> "console": nothing is sent; a line says so (dev)

Every attempt logs one line on the "crm.email" logger with the recipient MASKED
(s***@gmail.com). Never logged: the body (it can carry a reset link), API keys or the
SMTP password. Failures raise EmailError whose message is safe to show a super admin.
"""
import html
import logging
import re
import smtplib
import ssl
import time
from email.message import EmailMessage
from email.utils import parseaddr

import requests as http_requests

from app.core.config import settings

logger = logging.getLogger("crm.email")

API_PROVIDERS = ("resend", "brevo")
_TIMEOUT_API = 10
_TIMEOUT_SMTP = 15
_MAX_ERROR = 200


class EmailError(Exception):
    """Delivery failed. `str(error)` is "<ExceptionClass>: <short safe message>"."""


# ------------------------------------------------------------------ config


def mask_email(address: str | None) -> str:
    """s***@gmail.com -- enough to tell addresses apart in a log, not to read them."""
    if not address or "@" not in address:
        return "<invalid>"
    local, _, domain = address.strip().rpartition("@")
    return f"{local[:1]}***@{domain}"


def delivery_mode() -> str:
    if (settings.email_provider or "").strip().lower() in API_PROVIDERS:
        return "api"
    return "smtp" if settings.smtp_host else "console"


def _provider() -> str:
    return (settings.email_provider or "smtp").strip().lower()


def _smtp_security() -> str:
    if int(settings.smtp_port) == 465:
        return "ssl"
    return "starttls" if settings.smtp_use_tls else "none"


def from_address() -> str:
    """EMAIL_FROM, else SMTP_FROM; an unset or *.local address (which providers reject)
    falls back to SMTP_USER when one is configured."""
    configured = (settings.email_from or settings.smtp_from or "").strip()
    _, address = parseaddr(configured)
    if (not address or address.lower().endswith(".local")) and settings.smtp_user:
        return settings.smtp_user
    return configured


def config_summary() -> str:
    return (
        f"email: mode={delivery_mode()} host={settings.smtp_host or '-'} port={settings.smtp_port} "
        f"tls={_smtp_security() != 'none'} from={from_address() or '-'}"
    )


def transport_summary() -> str:
    mode = delivery_mode()
    if mode == "api":
        return f"email: transport=https provider={_provider()}"
    if mode == "smtp":
        return {
            "ssl": "email: transport=SMTP_SSL (implicit TLS, port 465)",
            "starttls": "email: transport=SMTP+STARTTLS",
            "none": "email: transport=SMTP without TLS",
        }[_smtp_security()]
    return "email: transport=console (nothing is sent)"


def log_startup_config() -> None:
    """One line at startup describing how email will go out (no secrets)."""
    logger.info(config_summary())
    logger.info(transport_summary())
    configured = (settings.email_from or settings.smtp_from or "").strip()
    _, address = parseaddr(configured)
    if delivery_mode() != "console" and (not address or address.lower().endswith(".local")):
        if settings.smtp_user:
            logger.warning("email: From address %r is unset or .local; using SMTP_USER instead", configured)
        else:
            logger.warning("email: From address %r is unset or .local and will likely be rejected", configured)
    if delivery_mode() == "api" and not settings.email_api_key:
        logger.warning("email: EMAIL_PROVIDER=%s but EMAIL_API_KEY is empty; every send will fail", _provider())


# ------------------------------------------------------------------ errors


def _safe(text: str) -> str:
    for secret in (settings.smtp_password, settings.email_api_key):
        if secret:
            text = text.replace(secret, "***")
    text = re.sub(r"\s+", " ", text).strip()
    return text[:_MAX_ERROR]


def _describe(exc: BaseException) -> str:
    if isinstance(exc, smtplib.SMTPResponseException):
        detail = exc.smtp_error.decode("utf-8", "replace") if isinstance(exc.smtp_error, bytes) else str(exc.smtp_error)
        message = f"{exc.smtp_code} {detail}"
    elif isinstance(exc, smtplib.SMTPRecipientsRefused):
        message = "; ".join(
            f"{code} {(err.decode('utf-8', 'replace') if isinstance(err, bytes) else err)}"
            for code, err in exc.recipients.values()
        )
    else:
        message = str(exc)
    return _safe(f"{type(exc).__name__}: {message}")


# ------------------------------------------------------------------ transports


def _send_smtp(to: str, subject: str, text: str, html_body: str | None) -> None:
    msg = EmailMessage()
    msg["From"] = from_address()
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(text)
    if html_body:
        msg.add_alternative(html_body, subtype="html")

    if _smtp_security() == "ssl":
        server_cm = smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=_TIMEOUT_SMTP,
                                     context=ssl.create_default_context())
    else:
        server_cm = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=_TIMEOUT_SMTP)
    with server_cm as server:
        if _smtp_security() == "starttls":
            server.starttls(context=ssl.create_default_context())
        if settings.smtp_user:
            server.login(settings.smtp_user, settings.smtp_password)
        server.send_message(msg)


class _ApiError(Exception):
    pass


def _api_failure(response: http_requests.Response) -> _ApiError:
    try:
        body = response.json()
    except ValueError:
        body = {}
    message = ""
    if isinstance(body, dict):
        message = str(body.get("message") or body.get("name") or body.get("code") or "")
    return _ApiError(f"HTTP {response.status_code} {message}".strip())


def _send_api(to: str, subject: str, text: str, html_body: str | None) -> None:
    if not settings.email_api_key:
        raise _ApiError("EMAIL_API_KEY is not configured")
    sender = from_address()
    if _provider() == "resend":
        payload = {"from": sender, "to": [to], "subject": subject, "text": text}
        if html_body:
            payload["html"] = html_body
        response = http_requests.post(
            "https://api.resend.com/emails",
            json=payload,
            headers={"Authorization": f"Bearer {settings.email_api_key}"},
            timeout=_TIMEOUT_API,
        )
        if response.status_code not in (200, 201):
            raise _api_failure(response)
        return
    # brevo
    name, address = parseaddr(sender)
    payload = {
        "sender": {"email": address, **({"name": name} if name else {})},
        "to": [{"email": to}],
        "subject": subject,
        "textContent": text,
    }
    if html_body:
        payload["htmlContent"] = html_body
    response = http_requests.post(
        "https://api.brevo.com/v3/smtp/email",
        json=payload,
        headers={"api-key": settings.email_api_key, "accept": "application/json"},
        timeout=_TIMEOUT_API,
    )
    if response.status_code not in (200, 201, 202):
        raise _api_failure(response)


# ------------------------------------------------------------------ entry points


def send_email(to: str, subject: str, body: str, html_body: str | None = None) -> int:
    """Send one email (plain text, plus HTML when given). Returns the duration in ms.

    Raises EmailError (safe message) on failure. In console mode nothing is sent and
    nothing is raised -- local development works without a mailbox.
    """
    mode = delivery_mode()
    masked = mask_email(to)
    if mode == "console":
        logger.warning("email: console mode, nothing sent to %s (subject=%s); body withheld", masked, subject)
        return 0
    started = time.perf_counter()
    try:
        if mode == "api":
            _send_api(to, subject, body, html_body)
        else:
            _send_smtp(to, subject, body, html_body)
    except Exception as exc:  # noqa: BLE001 -- every failure is reported the same safe way
        error = _describe(exc)
        logger.warning("email: FAILED to %s via %s: %s", masked, mode, error)
        raise EmailError(error) from None
    ms = int((time.perf_counter() - started) * 1000)
    logger.info("email: sent to %s via %s in %dms", masked, mode, ms)
    return ms


def password_reset_message(name: str, reset_link: str) -> tuple[str, str, str]:
    """(subject, plain text, html) for a password-reset email."""
    minutes = settings.reset_token_expire_minutes
    subject = "Reset your Beas Suite password"
    text = (
        f"Hi {name},\n\n"
        "We received a request to reset your Beas Suite password.\n"
        f"Open the link below to set a new password. It expires in {minutes} minutes.\n\n"
        f"{reset_link}\n\n"
        "If you did not request this, you can safely ignore this email.\n\n"
        "— Beas Suite"
    )
    link = html.escape(reset_link, quote=True)
    html_body = (
        '<!doctype html><html><body style="font-family:Arial,Helvetica,sans-serif;color:#1f2937;'
        'line-height:1.5;margin:0;padding:24px;background:#f9fafb">'
        '<div style="max-width:480px;margin:0 auto;background:#ffffff;border-radius:8px;padding:24px">'
        f"<p>Hi {html.escape(name)},</p>"
        "<p>We received a request to reset your Beas Suite password.</p>"
        f'<p style="margin:24px 0"><a href="{link}" style="background:#2563eb;color:#ffffff;'
        'text-decoration:none;padding:12px 20px;border-radius:6px;display:inline-block">Reset password</a></p>'
        f"<p>This link expires in {minutes} minutes. If the button doesn't work, copy this link "
        f'into your browser:<br><a href="{link}">{link}</a></p>'
        "<p>If you did not request this, you can safely ignore this email.</p>"
        "<p>— Beas Suite</p></div></body></html>"
    )
    return subject, text, html_body


def send_password_reset(to: str, name: str, reset_link: str) -> int:
    subject, text, html_body = password_reset_message(name, reset_link)
    return send_email(to=to, subject=subject, body=text, html_body=html_body)


def send_test_email(to: str) -> int:
    return send_email(
        to=to,
        subject="Beas Suite email test",
        body="This is a test email from Beas Suite. If you can read this, email delivery works.\n\n— Beas Suite",
        html_body="<p>This is a test email from Beas Suite. If you can read this, email delivery works.</p>"
                  "<p>— Beas Suite</p>",
    )
