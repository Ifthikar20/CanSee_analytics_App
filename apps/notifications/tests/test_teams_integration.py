"""Microsoft Teams outbound (incoming-webhook) integration.

Covers the connect-endpoint validation for the new ``teams`` platform and
the TeamsService payload selection. The two-way command bot (Bot Framework)
is a separate surface and not exercised here.
"""
from unittest.mock import MagicMock, patch

import pytest
from rest_framework.test import APIClient

from apps.accounts.tests.factories import UserFactory
from apps.notifications.models import IntegrationConnection
from apps.notifications.services.teams_service import TeamsService

INTEGRATIONS_URL = "/api/v1/notifications/integrations/"

# Classic connector (MessageCard) and Workflows (Adaptive Card) URLs.
TEAMS_CONNECTOR_URL = "https://acme.webhook.office.com/webhookb2/abc@def/IncomingWebhook/xyz/guid"
TEAMS_WORKFLOW_URL = "https://prod-12.westus.logic.azure.com:443/workflows/abc/triggers/manual/paths/invoke?sig=x"
SLACK_URL = "https://hooks.slack.com/services/T0/B0/xyz"


@pytest.fixture
def auth():
    user = UserFactory()
    client = APIClient()
    client.force_authenticate(user=user)
    return client, user


@pytest.fixture(autouse=True)
def _no_outbound_http():
    """The connect flow fires a synchronous test message — keep it offline."""
    with patch("apps.notifications.services.teams_service.requests.post") as teams_post:
        teams_post.return_value.status_code = 200
        yield teams_post


@pytest.mark.django_db
class TestTeamsConnect:
    def test_teams_platform_accepted_with_connector_url(self, auth):
        client, user = auth
        resp = client.post(
            INTEGRATIONS_URL,
            {"platform": "teams", "webhook_url": TEAMS_CONNECTOR_URL,
             "channel_name": "results"},
            format="json",
        )
        assert resp.status_code == 201
        conn = IntegrationConnection.objects.get(user=user, platform="teams")
        assert conn.is_active is True
        assert conn.webhook_url == TEAMS_CONNECTOR_URL

    def test_teams_platform_accepted_with_workflow_url(self, auth):
        client, user = auth
        resp = client.post(
            INTEGRATIONS_URL,
            {"platform": "teams", "webhook_url": TEAMS_WORKFLOW_URL},
            format="json",
        )
        assert resp.status_code == 201
        assert IntegrationConnection.objects.filter(user=user, platform="teams").exists()

    def test_non_teams_host_rejected(self, auth):
        client, user = auth
        resp = client.post(
            INTEGRATIONS_URL,
            {"platform": "teams", "webhook_url": "https://evil.example.com/hook"},
            format="json",
        )
        assert resp.status_code == 400
        assert "teams" in resp.json()["error"].lower()
        assert not IntegrationConnection.objects.filter(user=user).exists()

    def test_slack_url_on_teams_platform_rejected(self, auth):
        client, _user = auth
        resp = client.post(
            INTEGRATIONS_URL,
            {"platform": "teams", "webhook_url": SLACK_URL},
            format="json",
        )
        assert resp.status_code == 400

    def test_http_teams_url_rejected(self, auth):
        client, _user = auth
        resp = client.post(
            INTEGRATIONS_URL,
            {"platform": "teams",
             "webhook_url": "http://acme.webhook.office.com/webhookb2/x"},
            format="json",
        )
        assert resp.status_code == 400

    def test_connect_sends_test_message(self, auth, _no_outbound_http):
        client, _user = auth
        client.post(
            INTEGRATIONS_URL,
            {"platform": "teams", "webhook_url": TEAMS_CONNECTOR_URL},
            format="json",
        )
        assert _no_outbound_http.called


class TestTeamsService:
    def _mock_post(self):
        m = MagicMock()
        m.return_value = MagicMock(status_code=200, text="")
        return m

    def test_connector_url_sends_message_card(self):
        with patch("apps.notifications.services.teams_service.requests.post", self._mock_post()) as post:
            ok = TeamsService.send_message(
                webhook_url=TEAMS_CONNECTOR_URL, title="Hi", text="body")
        assert ok is True
        payload = post.call_args.kwargs["json"]
        assert payload["@type"] == "MessageCard"
        assert payload["title"] == "Hi"
        assert payload["text"] == "body"

    def test_workflow_url_sends_adaptive_card(self):
        with patch("apps.notifications.services.teams_service.requests.post", self._mock_post()) as post:
            ok = TeamsService.send_message(
                webhook_url=TEAMS_WORKFLOW_URL, title="Hi", text="body")
        assert ok is True
        payload = post.call_args.kwargs["json"]
        assert payload["type"] == "message"
        content = payload["attachments"][0]["content"]
        assert content["type"] == "AdaptiveCard"
        assert any(b.get("text") == "body" for b in content["body"])

    def test_empty_webhook_is_noop(self):
        assert TeamsService.send_message(webhook_url="", text="x") is False

    def test_workflow_accepts_202(self):
        m = MagicMock(return_value=MagicMock(status_code=202, text=""))
        with patch("apps.notifications.services.teams_service.requests.post", m):
            assert TeamsService.send_message(webhook_url=TEAMS_WORKFLOW_URL, text="x") is True


@pytest.mark.django_db
class TestTeamsDailyReport:
    @patch("apps.notifications.tasks._send_teams_report")
    @patch("apps.notifications.tasks._build_report_data", return_value={})
    def test_daily_report_routes_to_teams(self, _build, send_teams):
        user = UserFactory()
        IntegrationConnection.objects.create(
            user=user, platform="teams", webhook_url=TEAMS_CONNECTOR_URL,
            is_active=True, notify_daily_report=True, frequency="daily",
        )
        from apps.notifications.tasks import send_daily_growth_reports
        send_daily_growth_reports()
        assert send_teams.called
