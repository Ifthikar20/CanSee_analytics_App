"""Internal ops API: key gating and payload shapes for the admin server."""

import pytest
from rest_framework.test import APIClient

from apps.accounts.tests.factories import UserFactory
from apps.billing.models import Subscription
from apps.websites.tests.factories import WebsiteFactory
from core.utils.constants import Plan, SubscriptionStatus

OVERVIEW = "/api/v1/internal/admin/overview/"
USERS = "/api/v1/internal/admin/users/"
KEY = "test-ops-key"


@pytest.fixture
def ops_settings(settings):
    settings.ADMIN_OPS_KEY = KEY
    settings.ADMIN_OPS_ALLOWED_CIDRS = ["127.0.0.0/8"]
    return settings


def _client(key=KEY):
    client = APIClient()
    if key is not None:
        client.credentials(HTTP_X_ADMIN_KEY=key)
    return client


@pytest.mark.django_db
class TestKeyGate:
    def test_no_key_is_404(self, ops_settings):
        assert _client(key=None).get(OVERVIEW).status_code == 404

    def test_wrong_key_is_404(self, ops_settings):
        assert _client(key="nope").get(OVERVIEW).status_code == 404

    def test_disabled_when_setting_empty(self, settings):
        settings.ADMIN_OPS_KEY = ""
        # Even a "correct-looking" key is refused when the surface is off.
        assert _client(key="").get(OVERVIEW).status_code == 404
        assert _client(key="anything").get(USERS).status_code == 404

    def test_jwt_of_a_normal_user_does_not_help(self, ops_settings):
        client = APIClient()
        client.force_authenticate(user=UserFactory())
        assert client.get(OVERVIEW).status_code == 404

    def test_foreign_source_ip_is_404_even_with_the_key(self, ops_settings):
        resp = _client().get(OVERVIEW, REMOTE_ADDR="203.0.113.9")
        assert resp.status_code == 404

    def test_empty_cidr_list_denies_everyone(self, ops_settings):
        ops_settings.ADMIN_OPS_ALLOWED_CIDRS = []
        assert _client().get(OVERVIEW).status_code == 404


@pytest.mark.django_db
class TestOverview:
    def test_counts(self, ops_settings):
        # The shared test DB may carry residue rows (see conftest's
        # persistent django_db_setup) — assert deltas, not absolutes.
        before = _client().get(OVERVIEW).json()["data"]

        user = UserFactory(is_email_verified=True)
        UserFactory(is_email_verified=False)
        WebsiteFactory(user=user)
        Subscription.objects.create(
            user=user, plan=Plan.PRO, status=SubscriptionStatus.ACTIVE,
        )
        after = _client().get(OVERVIEW).json()["data"]
        assert after["users"]["total"] == before["users"]["total"] + 2
        assert after["users"]["verified"] == before["users"]["verified"] + 1
        assert (
            after["subscriptions"]["active"]
            == before["subscriptions"]["active"] + 1
        )
        assert after["projects"]["total"] == before["projects"]["total"] + 1


@pytest.mark.django_db
class TestUsersList:
    def test_rows_and_search(self, ops_settings):
        import uuid

        marker = uuid.uuid4().hex[:10]
        alice = UserFactory(
            email=f"alice-{marker}@example.com", full_name="Alice A",
        )
        UserFactory(email=f"bob-{marker}@example.com", full_name="Bob B")
        WebsiteFactory(user=alice)
        WebsiteFactory(user=alice)
        Subscription.objects.create(
            user=alice, plan=Plan.PRO, status=SubscriptionStatus.ACTIVE,
        )

        body = _client().get(USERS, {"search": marker}).json()["data"]
        assert body["total"] == 2

        body = _client().get(USERS, {"search": f"alice-{marker}"}).json()["data"]
        assert body["total"] == 1
        row = body["rows"][0]
        assert row["email"] == f"alice-{marker}@example.com"
        assert row["plan"] == "pro"
        assert row["projects"] == 2

    def test_enrichment_columns(self, ops_settings):
        import uuid

        from apps.accounts.models import AITokenUsage
        from apps.llm_ranking.models import LLMRankingAudit
        from apps.prompt_library.models import BrandPrompt
        from apps.prompt_library.tests.factories import IndustryFactory, PromptFactory
        from apps.websites.models import Integration

        marker = uuid.uuid4().hex[:10]
        user = UserFactory(email=f"rich-{marker}@example.com")
        site = WebsiteFactory(user=user, pixel_verified=True)
        Integration.objects.create(website=site, type="cloudflare", is_active=True)
        Integration.objects.create(website=site, type="ga", is_active=False)
        industry = IndustryFactory()
        for _ in range(3):
            BrandPrompt.objects.create(
                website=site, prompt=PromptFactory(industry=industry),
            )
        LLMRankingAudit.objects.create(
            website=site, created_by=user, business_name="X", industry="saas",
        )
        AITokenUsage.objects.create(
            user=user, website=site, module="llm_ranking",
            model_name="claude", total_tokens=1200,
            estimated_cost_usd="0.034500",
        )

        row = _client().get(USERS, {"search": marker}).json()["data"]["rows"][0]
        assert row["prompts"] == 3
        assert row["audits"] == 1
        assert row["tokens_total"] == 1200
        assert row["ai_cost_usd"] == pytest.approx(0.0345)
        # Active cloudflare + verified pixel appear; the INACTIVE ga does not.
        assert row["integrations"] == ["cloudflare", "pixel"]

    def test_soft_deleted_projects_not_counted(self, ops_settings):
        user = UserFactory(email="carol@example.com")
        site = WebsiteFactory(user=user)
        site.soft_delete(user=user)
        body = _client().get(USERS, {"search": "carol"}).json()["data"]
        assert body["rows"][0]["projects"] == 0
