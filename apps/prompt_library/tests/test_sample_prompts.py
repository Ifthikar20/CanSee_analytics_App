"""Generate-samples endpoint: fills a fresh website with diverse prompts.

Fully offline — the sample generator is the deterministic template pack
engine, never an LLM call.
"""

import pytest
from rest_framework.test import APIClient

from apps.accounts.tests.factories import UserFactory
from apps.prompt_library.models import BrandPrompt
from apps.prompt_library.tests.factories import IndustryFactory
from apps.websites.tests.factories import WebsiteFactory


def _url(website):
    return f"/api/v1/prompt-library/websites/{website.id}/prompts/generate-samples/"


@pytest.fixture
def auth_client(db):
    user = UserFactory()
    client = APIClient()
    client.force_authenticate(user=user)
    return client, user


@pytest.mark.django_db
class TestGenerateSamples:
    def test_creates_ten_diverse_website_prompts(self, auth_client):
        client, user = auth_client
        IndustryFactory(name="Cyber", slug="cyber")
        website = WebsiteFactory(user=user, name="strix", industry="cyber")

        resp = client.post(_url(website), {}, format="json")
        assert resp.status_code == 201
        body = resp.json()["data"]
        assert body["total"] == 10
        assert body["created_count"] == 10
        assert len(body["generated"]) == 10

        # The table reads BrandPrompt rows — all ten must be linked.
        assert BrandPrompt.objects.filter(website=website).count() == 10
        # "Very different types": at least 4 distinct intents in the mix.
        intents = {g["intent"] for g in body["generated"]}
        assert len(intents) >= 4
        # Unique texts.
        texts = [g["text"] for g in body["generated"]]
        assert len(set(texts)) == 10

    def test_rerun_does_not_duplicate(self, auth_client):
        client, user = auth_client
        IndustryFactory(name="Cyber", slug="cyber")
        website = WebsiteFactory(user=user, name="strix", industry="cyber")

        first = client.post(_url(website), {}, format="json")
        assert first.status_code == 201
        second = client.post(_url(website), {}, format="json")
        assert second.status_code == 201
        # Dedup on text hash: nothing new the second time around, and no
        # duplicate BrandPrompt links.
        assert BrandPrompt.objects.filter(website=website).count() == 10

    def test_count_is_clamped(self, auth_client):
        client, user = auth_client
        IndustryFactory(name="SaaS", slug="saas")
        website = WebsiteFactory(user=user, industry="saas")
        resp = client.post(_url(website), {"count": 99}, format="json")
        assert resp.status_code == 201
        assert resp.json()["data"]["total"] <= 15

    def test_unknown_industry_falls_back(self, auth_client):
        client, user = auth_client
        IndustryFactory(name="Anything", slug="anything")
        website = WebsiteFactory(user=user, industry="underwater basket weaving")
        resp = client.post(_url(website), {}, format="json")
        assert resp.status_code == 201
        assert resp.json()["data"]["total"] == 10

    def test_foreign_website_is_404(self, auth_client):
        client, user = auth_client
        IndustryFactory()
        other = WebsiteFactory()  # different owner
        resp = client.post(_url(other), {}, format="json")
        assert resp.status_code == 404

    def test_unauthenticated_401(self, db):
        website = WebsiteFactory()
        resp = APIClient().post(_url(website), {}, format="json")
        assert resp.status_code == 401
