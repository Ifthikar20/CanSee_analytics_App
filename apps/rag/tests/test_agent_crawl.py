"""Agent crawl consent: the enable switch and its seed crawl."""

from datetime import timedelta
from unittest.mock import patch

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from apps.rag.models import AgentCrawlConsent
from apps.websites.tests.factories import WebsiteFactory

pytestmark = pytest.mark.django_db


@pytest.fixture
def website(db):
    return WebsiteFactory(url="https://strix.ai")


@pytest.fixture
def client(website):
    api = APIClient()
    api.force_authenticate(user=website.user)
    return api


def _url(website):
    return f"/api/v1/rag/{website.id}/agent-crawl/"


class TestAgentCrawlConsent:
    def test_defaults_to_disabled(self, client, website):
        res = client.get(_url(website))
        assert res.status_code == 200
        body = res.json()["data"]
        assert body["enabled"] is False
        assert body["last_seeded_at"] is None

    @patch("apps.rag.tasks.ingest_site_task.delay")
    def test_enable_flags_and_seeds_a_crawl(self, delay, client, website):
        res = client.post(_url(website), {"enabled": True}, format="json")
        assert res.status_code == 200
        body = res.json()["data"]
        assert body["enabled"] is True
        assert body["seeded"] is True

        delay.assert_called_once_with(
            user_id=website.user_id,
            website_id=str(website.id),
            seed_url="https://strix.ai",
            page_cap=12,
            depth=1,
        )
        consent = AgentCrawlConsent.objects.get(user=website.user, website=website)
        assert consent.enabled is True
        assert consent.last_seeded_at is not None

    @patch("apps.rag.tasks.ingest_site_task.delay")
    def test_reenable_within_cooldown_does_not_reseed(self, delay, client, website):
        client.post(_url(website), {"enabled": True}, format="json")
        client.post(_url(website), {"enabled": False}, format="json")
        res = client.post(_url(website), {"enabled": True}, format="json")

        assert res.json()["data"]["seeded"] is False
        assert delay.call_count == 1

    @patch("apps.rag.tasks.ingest_site_task.delay")
    def test_reenable_after_cooldown_reseeds(self, delay, client, website):
        client.post(_url(website), {"enabled": True}, format="json")
        AgentCrawlConsent.objects.filter(website=website).update(
            last_seeded_at=timezone.now() - timedelta(hours=25),
        )
        client.post(_url(website), {"enabled": False}, format="json")
        res = client.post(_url(website), {"enabled": True}, format="json")

        assert res.json()["data"]["seeded"] is True
        assert delay.call_count == 2

    @patch("apps.rag.tasks.ingest_site_task.delay")
    def test_disable_keeps_history(self, delay, client, website):
        client.post(_url(website), {"enabled": True}, format="json")
        res = client.post(_url(website), {"enabled": False}, format="json")

        body = res.json()["data"]
        assert body["enabled"] is False
        assert body["last_seeded_at"] is not None  # history survives

    @patch("apps.rag.tasks.ingest_site_task.delay")
    def test_other_users_website_is_404(self, delay, client):
        foreign = WebsiteFactory()  # different owner
        res = client.get(_url(foreign))
        assert res.status_code == 404
        res = client.post(_url(foreign), {"enabled": True}, format="json")
        assert res.status_code == 404
        delay.assert_not_called()
