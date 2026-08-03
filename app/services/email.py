from __future__ import annotations

import logging
import smtplib
from email.mime.text import MIMEText
from email.utils import formataddr
from html import escape

from app.core.config import settings


logger = logging.getLogger(__name__)


def send_email_internal(receiver: str, subject: str, body: str) -> tuple[bool, str | None]:
    if not settings.smtp_enabled:
        return False, "SMTP is disabled"
    if not settings.smtp_host or not settings.smtp_user or not settings.smtp_password:
        return False, "SMTP configuration is incomplete"

    full_body = f"<p>{escape(body or '').replace(chr(10), '<br>')}</p><p>Email hosted by third party, do not reply.</p>"
    msg = MIMEText(full_body, "html", "utf-8")
    msg["from"] = formataddr((settings.smtp_sender_name, settings.smtp_user))
    msg["to"] = receiver
    msg["subject"] = subject

    try:
        if settings.smtp_use_ssl:
            with smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=10) as smtp:
                smtp.login(settings.smtp_user, settings.smtp_password)
                smtp.sendmail(settings.smtp_user, [receiver], msg.as_string())
        else:
            with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=10) as smtp:
                smtp.starttls()
                smtp.login(settings.smtp_user, settings.smtp_password)
                smtp.sendmail(settings.smtp_user, [receiver], msg.as_string())
    except Exception as exc:
        logger.exception("Failed to send email to %s", receiver)
        return False, str(exc)
    return True, None
