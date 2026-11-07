"""Per-project scoping of the dashboard analytics services.

A user with two projects must never see project A's audits inside
project B's KPIs, overview, deep dive, series, or filter menus.
"""

from datetime import timedelta

import pytest
from django.utils import timezone

from apps.accounts.tests.factories import UserFactory
from apps.llm_ranking.models import LLMRankingAudit, LLMRankingResult
from apps.llm_ranking.services.geo_deep_dive import build_for_user as build_deep_dive
from apps.llm_ranking.services.geo_stats import (
    build_breakdowns_for_user,
    build_kpis_for_user,
)
from apps.llm_ranking.services.overview_stats import (
    build_filter_options,
    build_overview_for_user,
)
from apps.llm_ranking.services.visibility_series import (
    build_for_user as build_visibility_series,
)
from apps.llm_ranking.tests.factories import (
    LLMRankingAuditFactory,
    LLMRankingResultFactory,
)
from apps.websites.tests.factories import WebsiteFactory


@pytest.fixture
def two_projects(db):
    user = UserFactory()
    site_a = WebsiteFactory(user=user, name="Alpha")
    site_b = WebsiteFactory(user=user, name="Beta")
    now = timezone.now()

    audit_a = LLMRankingAuditFactory(
        website=site_a, created_by=user, business_name="Alpha",
        status=LLMRankingAudit.STATUS_COMPLETED,
        completed_at=now - timedelta(days=2), mention_rate=80.0,
    )
    LLMRankingResultFactory(
        audit=audit_a, prompt="best alpha tools",
        response_text="1. Alpha", mention_context="1. Alpha",
    )
    # Beta audited more recently — user-global code would crown Beta as
    # "the brand" for the whole account.
    audit_b = LLMRankingAuditFactory(
        website=site_b, created_by=user, business_name="Beta",
        status=LLMRankingAudit.STATUS_COMPLETED,
        completed_at=now - timedelta(days=1), mention_rate=20.0,
    )
    LLMRankingResultFactory(
        audit=audit_b, provider=LLMRankingResult.PROVIDER_GPT4,
        prompt="best beta tools",
        response_text="1. Beta", mention_context="1. Beta",
        is_mentioned=False, mention_rank=None, sentiment="neutral",
    )
    return user, site_a, site_b, audit_a, audit_b


class TestKpisScoped:
    def test_kpis_use_only_the_given_websites_audits(self, two_projects):
        user, site_a, site_b, *_ = two_projects
        tiles_a = build_kpis_for_user(user, website=site_a)
        tiles_b = build_kpis_for_user(user, website=site_b)
        vis_a = next(t for t in tiles_a if t["label"] == "Visibility")
        vis_b = next(t for t in tiles_b if t["label"] == "Visibility")
        assert vis_a["value"] == "80.0%"
        assert vis_b["value"] == "20.0%"

    def test_no_data_for_website_without_audits(self, two_projects):
        user, site_a, *_ = two_projects
        empty_site = WebsiteFactory(user=user)
        assert build_kpis_for_user(user, website=empty_site) is None


class TestOverviewScoped:
    def test_target_brand_is_the_selected_project(self, two_projects):
        user, site_a, site_b, *_ = two_projects
        overview_a = build_overview_for_user(user, website=site_a)
        assert overview_a["has_data"] is True
        names = [b["name"] for b in overview_a["brands"]]
        # Alpha's overview is built from Alpha's results only — Beta's
        # brand must not appear as a competitor row.
        assert "Beta" not in names
        prompt_texts = [p["text"] for p in overview_a["prompts"]]
        assert prompt_texts == ["best alpha tools"]


class TestDeepDiveScoped:
    def test_available_prompts_come_from_one_project(self, two_projects):
        user, site_a, *_ = two_projects
        payload = build_deep_dive(user, website=site_a)
        values = [p["value"] for p in payload["available_prompts"]]
        assert values == ["best alpha tools"]


class TestBreakdownsScoped:
    def test_provider_rows_come_from_one_project(self, two_projects):
        user, site_a, *_ = two_projects
        payload = build_breakdowns_for_user(user, website=site_a)
        providers = [
            row["provider"] for row in payload["visibility"]["by_provider"]
        ]
        assert providers == [LLMRankingResult.PROVIDER_CLAUDE]


class TestSeriesScoped:
    def test_series_is_none_for_unaudited_website(self, two_projects):
        user, *_ = two_projects
        empty_site = WebsiteFactory(user=user)
        assert build_visibility_series(user, website=empty_site) is None

    def test_series_exists_for_audited_website(self, two_projects):
        user, site_a, *_ = two_projects
        assert build_visibility_series(user, website=site_a) is not None


class TestFilterOptionsScoped:
    def test_models_menu_lists_only_the_projects_providers(self, two_projects):
        user, site_a, site_b, *_ = two_projects
        options_a = build_filter_options(user, website=site_a)
        options_b = build_filter_options(user, website=site_b)
        assert [m["value"] for m in options_a["models"]] == [
            LLMRankingResult.PROVIDER_CLAUDE,
        ]
        assert [m["value"] for m in options_b["models"]] == [
            LLMRankingResult.PROVIDER_GPT4,
        ]


@pytest.mark.django_db
class TestDashboardEndpointScoped:
    def test_dashboard_accepts_website_param(self, two_projects):
        from rest_framework.test import APIClient

        user, site_a, site_b, *_ = two_projects
        client = APIClient()
        client.force_authenticate(user=user)
        resp = client.get(f"/api/v1/websites/dashboard/?website={site_a.id}")
        assert resp.status_code == 200
        data = resp.json()["data"]
        vis = next(t for t in data["stats"] if t["label"] == "Visibility")
        assert vis["value"] == "80.0%"

    def test_foreign_website_id_is_404(self, two_projects):
        from rest_framework.test import APIClient

        user, *_ = two_projects
        other_site = WebsiteFactory()  # different owner
        client = APIClient()
        client.force_authenticate(user=user)
        resp = client.get(f"/api/v1/websites/dashboard/?website={other_site.id}")
        assert resp.status_code == 404
