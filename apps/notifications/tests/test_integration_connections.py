"""Tests for the IntegrationConnection connect/update webhook-URL validation."""
from unittest.mock import patch

import pytest
from rest_framework.test import APIClient

from apps.accounts.tests.factories import UserFactory
from apps.notifications.models import IntegrationConnection

INTEGRATIONS_URL = "/api/v1/notifications/integrations/"

SLACK_URL = "https://hooks.slack.com/services/T0/B0/xyz"
DISCORD_URL = "https://discord.com/api/webhooks/123/token-abc"
DISCORDAPP_URL = "https://discordapp.com/api/webhooks/123/token-abc"


@pytest.fixture
def auth():
    user = UserFactory()
    client = APIClient()
    client.force_authenticate(user=user)
    return client, user


@pytest.fixture(autouse=True)
def _no_outbound_http():
    """The connect flow fires a synchronous test message - keep it offline."""
    with patch("apps.notifications.services.slack_service.requests.post") as slack_post, \
         patch("apps.notifications.services.discord_service.requests.post") as discord_post:
        slack_post.return_value.status_code = 200
        discord_post.return_value.status_code = 204
        yield


@pytest.mark.django_db
class TestConnectValidation:
    def test_requires_auth(self):
        resp = APIClient().post(INTEGRATIONS_URL, {"platform": "slack"}, format="json")
        assert resp.status_code == 401

    def test_slack_url_on_discord_platform_rejected(self, auth):
        client, user = auth
        resp = client.post(
            INTEGRATIONS_URL,
            {"platform": "discord", "webhook_url": SLACK_URL},
            format="json",
        )
        assert resp.status_code == 400
        assert "discord" in resp.json()["error"].lower()
        assert not IntegrationConnection.objects.filter(user=user).exists()

    def test_discord_url_on_slack_platform_rejected(self, auth):
        client, _user = auth
        resp = client.post(
            INTEGRATIONS_URL,
            {"platform": "slack", "webhook_url": DISCORD_URL},
            format="json",
        )
        assert resp.status_code == 400

    def test_arbitrary_host_rejected(self, auth):
        client, _user = auth
        resp = client.post(
            INTEGRATIONS_URL,
            {"platform": "slack", "webhook_url": "https://evil.example.com/hook"},
            format="json",
        )
        assert resp.status_code == 400

    def test_valid_slack_url_accepted(self, auth):
        client, user = auth
        resp = client.post(
            INTEGRATIONS_URL,
            {"platform": "slack", "webhook_url": SLACK_URL},
            format="json",
        )
        assert resp.status_code == 201
        connection = IntegrationConnection.objects.get(user=user, platform="slack")
        assert connection.webhook_url == SLACK_URL

    def test_valid_discord_urls_accepted(self, auth):
        client, user = auth
        for url in (DISCORD_URL, DISCORDAPP_URL):
            resp = client.post(
                INTEGRATIONS_URL,
                {"platform": "discord", "webhook_url": url},
                format="json",
            )
            assert resp.status_code in (200, 201)
        connection = IntegrationConnection.objects.get(user=user, platform="discord")
        assert connection.webhook_url == DISCORDAPP_URL  # second upsert won

    def test_webhook_url_whitespace_stripped(self, auth):
        client, user = auth
        resp = client.post(
            INTEGRATIONS_URL,
            {"platform": "slack", "webhook_url": f"  {SLACK_URL}  "},
            format="json",
        )
        assert resp.status_code == 201
        connection = IntegrationConnection.objects.get(user=user, platform="slack")
        assert connection.webhook_url == SLACK_URL

    def test_external_team_id_persisted(self, auth):
        client, user = auth
        resp = client.post(
            INTEGRATIONS_URL,
            {
                "platform": "discord",
                "webhook_url": DISCORD_URL,
                "external_team_id": "999888777",
            },
            format="json",
        )
        assert resp.status_code == 201
        connection = IntegrationConnection.objects.get(user=user, platform="discord")
        assert connection.external_team_id == "999888777"

    def test_telegram_platform_no_longer_accepted(self, auth):
        client, _user = auth
        resp = client.post(
            INTEGRATIONS_URL,
            {"platform": "telegram", "webhook_url": "123456"},
            format="json",
        )
        assert resp.status_code == 400
        assert "Invalid platform" in resp.json()["error"]

    def test_put_validates_webhook_url_too(self, auth):
        client, user = auth
        create = client.post(
            INTEGRATIONS_URL,
            {"platform": "slack", "webhook_url": SLACK_URL},
            format="json",
        )
        connection_id = create.json()["data"]["data"]["id"]
        resp = client.put(
            f"{INTEGRATIONS_URL}{connection_id}/",
            {"webhook_url": "https://evil.example.com/hook"},
            format="json",
        )
        assert resp.status_code == 400
        connection = IntegrationConnection.objects.get(user=user, platform="slack")
        assert connection.webhook_url == SLACK_URL
