"""Outbound email.

Sends through Django's mail framework, so the backend is whatever the
active settings module configures:

- prod:  SMTP (config/settings/prod.py) — any provider that speaks SMTP,
         including AWS SES. Note prod.py silently downgrades to the
         console backend when EMAIL_HOST is unset, so mail only truly
         leaves the box once the SMTP env vars are filled in.
- dev:   console backend (printed to runserver output).
- test:  locmem backend (django.core.mail.outbox).

History: this module previously called the SendGrid SDK, but the
``sendgrid`` package was never added to requirements, so the import
inside the try block raised on every call, the broad except swallowed
it, and every email in the product silently reported failure. Routing
through django.core.mail uses the backend that was already configured
and removes the dead dependency.
"""
import logging

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.utils.html import strip_tags

logger = logging.getLogger("apps")


class EmailService:
    @staticmethod
    def send_email(*, to: str, subject: str, html_content: str) -> bool:
        """Send one HTML email. Returns True when the backend accepted it.

        A plain-text alternative is derived from the HTML so recipients
        whose clients block HTML still get readable content, which also
        modestly helps spam scoring.
        """
        try:
            message = EmailMultiAlternatives(
                subject=subject,
                body=strip_tags(html_content),
                from_email=settings.DEFAULT_FROM_EMAIL,
                to=[to],
            )
            message.attach_alternative(html_content, "text/html")
            sent = message.send(fail_silently=False)
            return sent == 1
        except Exception as e:
            # Log the recipient domain only — never the full address (PII).
            domain = to.rsplit("@", 1)[-1] if "@" in (to or "") else "?"
            logger.error("Email send failed (domain=%s): %s", domain, e)
            return False
