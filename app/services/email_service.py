import logging
import smtplib
from email.message import EmailMessage

from app.core.config import settings

logger = logging.getLogger("crm.email")


def send_email(to: str, subject: str, body: str) -> None:
    """Send a plain-text email.

    If no SMTP host is configured we run in "console" mode and log the message
    instead of sending it — so local dev works without a mailbox.
    """
    if not settings.smtp_host:
        logger.warning(
            "[email:console] SMTP not configured. Would send:\n"
            "  To: %s\n  Subject: %s\n  Body:\n%s",
            to,
            subject,
            body,
        )
        return

    msg = EmailMessage()
    msg["From"] = settings.smtp_from
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)

    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=15) as server:
        if settings.smtp_use_tls:
            server.starttls()
        if settings.smtp_user:
            server.login(settings.smtp_user, settings.smtp_password)
        server.send_message(msg)
    logger.info("Sent email to %s (subject=%s)", to, subject)


def send_password_reset(to: str, name: str, reset_link: str) -> None:
    minutes = settings.reset_token_expire_minutes
    body = (
        f"Hi {name},\n\n"
        "We received a request to reset your CRM SaaS password.\n"
        f"Click the link below to set a new password (valid for {minutes} minutes):\n\n"
        f"{reset_link}\n\n"
        "If you did not request this, you can safely ignore this email.\n\n"
        "— CRM SaaS"
    )
    send_email(to=to, subject="Reset your CRM SaaS password", body=body)
