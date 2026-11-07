"""EmailService, now backed by django.core.mail.

The previous implementation imported the SendGrid SDK, which was never in
requirements, so every send raised ImportError, the broad except swallowed
it, and the method returned False forever. These tests pin the replacement
to the configured backend (locmem under config/settings/test.py) so a
regression to an uninstallable dependency fails loudly.
"""
from unittest.mock import patch

import pytest
from django.core import mail

from apps.notifications.services.email_service import EmailService


class TestSendEmail:
    def test_sends_one_message_through_the_configured_backend(self):
        ok = EmailService.send_email(
            to="owner@example.com",
            subject="Weekly report",
            html_content="<p>Hello <strong>there</strong></p>",
        )
        assert ok is True
        assert len(mail.outbox) == 1

        msg = mail.outbox[0]
        assert msg.to == ["owner@example.com"]
        assert msg.subject == "Weekly report"
        # Plain-text body is derived from the HTML for clients that
        # refuse text/html.
        assert msg.body == "Hello there"
        html_alts = [c for c, t in msg.alternatives if t == "text/html"]
        assert html_alts == ["<p>Hello <strong>there</strong></p>"]

    def test_from_address_comes_from_settings_not_a_hardcode(self, settings):
        settings.DEFAULT_FROM_EMAIL = "noreply@cansee.ai"
        EmailService.send_email(
            to="owner@example.com", subject="s", html_content="<p>b</p>",
        )
        assert mail.outbox[0].from_email == "noreply@cansee.ai"

    def test_backend_failure_returns_false_instead_of_raising(self):
        with patch.object(
            mail.EmailMessage, "send", side_effect=ConnectionError("smtp down"),
        ):
            ok = EmailService.send_email(
                to="owner@example.com", subject="s", html_content="<p>b</p>",
            )
        assert ok is False
        assert len(mail.outbox) == 0


@pytest.mark.django_db
class TestHotLeadAlert:
    def _user(self):
        from apps.accounts.models import User

        return User.objects.create_user(
            email="lead-owner@example.com",
            password="TestPass123!",
            full_name="Lead Owner",
        )

    def test_no_preference_row_means_no_email(self):
        user = self._user()

        class Lead:
            score = 91

        EmailService.send_hot_lead_alert(user=user, lead=Lead())
        assert len(mail.outbox) == 0

    def test_opted_in_user_gets_the_alert(self):
        from apps.notifications.models import NotificationPreference

        user = self._user()
        NotificationPreference.objects.create(user=user, hot_lead_email=True)

        class Lead:
            score = 91

        EmailService.send_hot_lead_alert(user=user, lead=Lead())
        assert len(mail.outbox) == 1
        assert "91" in mail.outbox[0].subject
