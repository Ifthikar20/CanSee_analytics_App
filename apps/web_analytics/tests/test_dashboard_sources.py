"""Dashboard source resolution: GA4/Cloudflare builders + resolver + views."""

from unittest.mock import patch

import pytest
from django.core.cache import cache
from rest_framework.test import APIClient

from apps.accounts.tests.factories import UserFactory
from apps.web_analytics.services import (
    cloudflare_dashboard,
    ga4_dashboard,
    source_resolver,
)
from apps.web_analytics.tests.factories import (
    CloudflareIntegrationFactory,
    Ga4IntegrationFactory,
)
from apps.websites.tests.factories import WebsiteFactory


@pytest.fixture(autouse=True)
def _clean(settings):
    settings.WEB_ANALYTICS_ENABLED = True
    cache.clear()
    yield
    cache.clear()


def _report(rows):
    return {
        "rows": [
            {
                "dimensionValues": [{"value": d} for d in dims],
                "metricValues": [{"value": str(m)} for m in mets],
            }
            for dims, mets in rows
        ]
    }


# ── GA4 builder ──────────────────────────────────────────────────────────────


@pytest.mark.django_db
def test_ga4_bundle_report_period_shapes():
    responses = [
        # KPIs: two date ranges -> implicit dateRange dimension.
        _report([
            (["date_range_0"], [200, 640, 95.0, 0.42]),
            (["date_range_1"], [100, 400, 100.0, 0.5]),
        ]),
        # Chart (date, [users, views, sessions]).
        _report([(["20260824"], [10, 30, 12]), (["20260825"], [12, 40, 14])]),
        # Pages.
        _report([(["/pricing"], [50]), (["/docs"], [30])]),
        # Sources (sessionSource, sessionMedium, sessions).
        _report([
            (["google", "organic"], [80]),
            (["chatgpt.com", "referral"], [20]),
        ]),
        # Devices / browsers / OS.
        _report([(["desktop"], [150]), (["mobile"], [50])]),
        _report([(["Chrome"], [120]), (["Safari"], [60])]),
        _report([(["Windows"], [90]), (["macOS"], [70])]),
        # Countries.
        _report([(["United States"], [120]), (["Germany"], [80])]),
    ]
    with patch.object(ga4_dashboard.ga4_client, "run_report", side_effect=responses):
        bundle = ga4_dashboard.build_bundle("tok", "111", period="30d", website_id="w1")

    o = bundle["overview"]
    assert o["data_source"] == "ga4"
    assert o["period"] == "30d"
    assert o["total_visitors"] == 200
    assert o["visitor_growth_pct"] == 100.0
    assert o["total_pageviews"] == 640
    assert o["pageviews_trend"] == 60.0
    assert o["avg_session"] == "1:35"
    assert o["bounce_rate"] == 42.0
    assert o["hot_leads"] == 0

    # Chart is zero-filled across the whole window with GA4 rows merged in.
    assert len(bundle["chart"]) == 30
    merged = [b for b in bundle["chart"] if b["visitors"]]
    assert merged and merged[-1]["pageviews"] == 40
    assert all("key" not in b for b in bundle["chart"])

    assert bundle["pages"][0] == {"url": "/pricing", "views": 50}

    ai_row = next(s for s in bundle["sources"] if s["source"] == "chatgpt.com")
    assert ai_row["medium"] == "ai"
    assert ai_row["count"] == ai_row["sessions"] == 20
    assert ai_row["percentage"] == 20.0
    organic = next(s for s in bundle["sources"] if s["source"] == "google")
    assert organic["medium"] == "organic"

    desktop = bundle["devices"]["devices"][0]
    assert desktop["name"] == "Desktop"
    assert desktop["count"] == 150
    assert desktop["color"] == "var(--brand-accent)"
    assert bundle["devices"]["browsers"][0]["name"] == "Chrome"

    assert bundle["countries"][0] == {"name": "United States", "pct": 60.0, "visitors": 120}


@pytest.mark.django_db
def test_ga4_bundle_realtime_period_uses_snapshot():
    snapshot = {
        "active_users": 7,
        "page_views": 12,
        "per_minute": [
            {"minutes_ago": m, "active_users": 1 if m < 5 else 0, "page_views": m % 2}
            for m in range(29, -1, -1)
        ],
        "top_pages": [{"page": "/live", "page_views": 4, "active_users": 2}],
        "countries": [{"country": "US", "active_users": 5}],
        "devices": [{"device": "desktop", "active_users": 6}],
        "window_minutes": 30,
        "fetched_at": "now",
    }
    with patch.object(ga4_dashboard.ga4_client, "build_realtime_snapshot", return_value=snapshot):
        bundle = ga4_dashboard.build_bundle("tok", "111", period="15m", website_id="w1")

    assert bundle["overview"]["total_visitors"] == 7
    assert bundle["overview"]["realtime"] == 7
    assert len(bundle["chart"]) == 15  # trimmed to the requested window
    assert bundle["chart"][-1]["label"] == "now"
    assert bundle["sources"] == []  # realtime has no source/medium
    assert bundle["devices"]["devices"][0]["color"] == "var(--brand-accent)"


@pytest.mark.django_db
def test_ga4_bundle_none_when_kpis_fail():
    with patch.object(ga4_dashboard.ga4_client, "run_report", return_value=None):
        assert ga4_dashboard.build_bundle("tok", "111", period="30d") is None


# ── Cloudflare builder ───────────────────────────────────────────────────────


def _cf_group(requests_count, visits, interval=1, dims=None):
    group = {
        "count": requests_count,
        "avg": {"sampleInterval": interval},
        "sum": {"visits": visits, "edgeResponseBytes": 0},
    }
    if dims:
        group["dimensions"] = dims
    return group


def _cf_settings():
    return {key: dict(value) for key, value in cloudflare_dashboard.DEFAULT_DATASET_SETTINGS.items()}


def _cf_rollup_group(page_views, uniques, requests=0, dims=None):
    group = {
        "sum": {"pageViews": page_views, "requests": requests},
        "uniq": {"uniques": uniques},
    }
    if dims:
        group["dimensions"] = dims
    return group


def _cf_main(series=None, totals=None):
    return {"viewer": {"zones": [{"series": series or [], "totals": [totals] if totals else []}]}}


def _cf_prev(totals=None):
    return {"viewer": {"zones": [{"previous": [totals] if totals else []}]}}


def _cf_maps(country_map=None, browser_map=None):
    return {"viewer": {"zones": [{
        "maps": [{"sum": {"countryMap": country_map or [], "browserMap": browser_map or []}}]
    }]}}


def _cf_breakdowns(pages=None, devices=None, oses=None, browsers=None, countries=None):
    return {"viewer": {"zones": [{
        "pages": pages or [],
        "deviceTypes": devices or [],
        "oses": oses or [],
        "browsers": browsers or [],
        "countries": countries or [],
    }]}}


def _patched_settings():
    return patch.object(cloudflare_dashboard, "_zone_settings", return_value=_cf_settings())


def test_cloudflare_daily_bundle_uses_rollups_and_breakdowns():
    from django.utils import timezone as dj_tz

    today = dj_tz.now().date().isoformat()
    main = _cf_main(
        series=[_cf_rollup_group(120, 40, requests=300, dims={"date": today})],
        totals=_cf_rollup_group(800, 150, requests=2000),
    )
    previous = _cf_prev(_cf_rollup_group(400, 100, requests=1000))
    breakdowns = _cf_breakdowns(
        pages=[
            _cf_group(30, 0, 2, {"clientRequestPath": "/pricing"}),
            _cf_group(90, 0, 2, {"clientRequestPath": "/static/app.js"}),  # asset -> dropped
        ],
        devices=[_cf_group(40, 0, 2, {"clientDeviceType": "desktop"})],
        oses=[_cf_group(25, 0, 2, {"userAgentOS": "Windows"})],
        browsers=[_cf_group(20, 0, 2, {"userAgentBrowser": "Firefox"})],
        countries=[_cf_group(10, 0, 2, {"clientCountryName": "JP"})],
    )
    maps = _cf_maps(
        country_map=[
            {"clientCountryName": "US", "requests": 1200},
            {"clientCountryName": "DE", "requests": 800},
        ],
        browser_map=[
            {"uaBrowserFamily": "Chrome", "pageViews": 90},
            {"uaBrowserFamily": "Safari", "pageViews": 30},
        ],
    )
    with _patched_settings(), \
         patch.object(cloudflare_dashboard, "_post_graphql",
                      side_effect=[main, previous, breakdowns, maps]) as mock_gql:
        bundle = cloudflare_dashboard.build_bundle("tok", "z1", period="7d")

    o = bundle["overview"]
    assert o["data_source"] == "cloudflare"
    assert o["total_visitors"] == 150       # real uniques from the rollup
    assert o["total_pageviews"] == 800      # real pageViews (nonzero -> no fallback)
    assert o["visitor_growth_pct"] == 50.0
    assert o["pageviews_trend"] == 100.0
    assert o["breakdown_window_hours"] == 24  # free-plan adaptive clamp

    assert len(bundle["chart"]) == 7
    assert bundle["chart"][-1]["visitors"] == 40
    assert bundle["chart"][-1]["pageviews"] == 120

    assert bundle["pages"] == [{"url": "/pricing", "views": 60}]  # asset filtered out
    assert bundle["sources"] == []  # clientRefererHost not queryable on free zones
    assert bundle["devices"]["devices"][0]["name"] == "Desktop"
    assert bundle["devices"]["devices"][0]["color"] == "var(--brand-accent)"
    assert bundle["devices"]["operating_systems"][0]["name"] == "Windows"
    # Rollup browserMap wins over the adaptive browser rows.
    assert bundle["devices"]["browsers"][0] == {"name": "Chrome", "count": 90, "pct": 75.0}
    # Rollup countryMap wins over the adaptive country rows.
    assert bundle["countries"][0] == {"name": "US", "pct": 60.0, "visitors": 1200}
    assert mock_gql.call_count == 4

    # The adaptive breakdowns window was clamped to <= 1 day (free plan).
    adaptive_vars = mock_gql.call_args_list[2].args[2]
    from datetime import datetime
    span = datetime.strptime(adaptive_vars["now"], "%Y-%m-%dT%H:%M:%SZ") - datetime.strptime(
        adaptive_vars["since"], "%Y-%m-%dT%H:%M:%SZ"
    )
    assert span.total_seconds() <= 86400


def test_cloudflare_pageviews_fall_back_to_requests_when_zero():
    main = _cf_main(totals=_cf_rollup_group(0, 150, requests=2000))
    previous = _cf_prev(_cf_rollup_group(0, 100, requests=500))
    with _patched_settings(), \
         patch.object(cloudflare_dashboard, "_post_graphql",
                      side_effect=[main, previous, _cf_breakdowns(), _cf_maps()]):
        bundle = cloudflare_dashboard.build_bundle("tok", "z1", period="30d")

    # SPA/bot-only zones report pageViews 0; requests stand in.
    assert bundle["overview"]["total_pageviews"] == 2000
    assert bundle["overview"]["pageviews_trend"] == 300.0
    assert bundle["overview"]["total_visitors"] == 150


def test_cloudflare_hourly_bundle():
    main = _cf_main(totals=_cf_rollup_group(50, 20, requests=90))
    with _patched_settings(), \
         patch.object(cloudflare_dashboard, "_post_graphql",
                      side_effect=[main, None, _cf_breakdowns(), _cf_maps()]):
        bundle = cloudflare_dashboard.build_bundle("tok", "z1", period="24h")

    assert bundle["overview"]["total_visitors"] == 20
    assert bundle["overview"]["visitor_growth_pct"] == 0  # previous failed -> trend 0
    assert len(bundle["chart"]) == 24


def test_cloudflare_minute_period_uses_adaptive_estimates():
    main = {"viewer": {"zones": [{
        "series": [],
        "totals": [_cf_group(100, 0, 2)],  # visits 0 -> visitors fall back to requests
    }]}}
    previous = {"viewer": {"zones": [{"previous": [_cf_group(40, 0, 2)]}]}}
    with _patched_settings(), \
         patch.object(cloudflare_dashboard, "_post_graphql",
                      side_effect=[main, previous, _cf_breakdowns(
                          countries=[_cf_group(30, 0, 2, {"clientCountryName": "US"})]
                      )]) as mock_gql:
        bundle = cloudflare_dashboard.build_bundle("tok", "z1", period="10m")

    assert bundle["overview"]["total_visitors"] == 200   # requests fallback
    assert bundle["overview"]["total_pageviews"] == 200
    assert len(bundle["chart"]) == 10
    assert bundle["countries"] == [{"name": "US", "pct": 100.0, "visitors": 60}]
    assert mock_gql.call_count == 3  # no maps call on the adaptive path


def test_cloudflare_breakdown_failure_keeps_kpis_and_maps():
    main = _cf_main(totals=_cf_rollup_group(800, 150, requests=2000))
    maps = _cf_maps(country_map=[{"clientCountryName": "US", "requests": 10}])
    with _patched_settings(), \
         patch.object(cloudflare_dashboard, "_post_graphql",
                      side_effect=[main, None, None, maps]):
        bundle = cloudflare_dashboard.build_bundle("tok", "z1", period="7d")

    assert bundle["overview"]["total_visitors"] == 150
    assert bundle["pages"] == []
    assert bundle["devices"]["devices"] == []
    assert bundle["countries"] == [{"name": "US", "pct": 100.0, "visitors": 10}]


def test_cloudflare_maps_failure_falls_back_to_adaptive_rows():
    main = _cf_main(totals=_cf_rollup_group(800, 150, requests=2000))
    breakdowns = _cf_breakdowns(
        browsers=[_cf_group(20, 0, 2, {"userAgentBrowser": "Firefox"})],
        countries=[_cf_group(30, 0, 2, {"clientCountryName": "JP"})],
    )
    with _patched_settings(), \
         patch.object(cloudflare_dashboard, "_post_graphql",
                      side_effect=[main, None, breakdowns, None]):
        bundle = cloudflare_dashboard.build_bundle("tok", "z1", period="7d")

    assert bundle["devices"]["browsers"][0]["name"] == "Firefox"
    assert bundle["countries"] == [{"name": "JP", "pct": 100.0, "visitors": 60}]


def test_cloudflare_settings_cached_across_builds():
    settings_resp = {"viewer": {"zones": [{"settings": {
        "adaptive": {"enabled": True, "maxDuration": 86400, "notOlderThan": 691200},
        "m1": {"enabled": False, "maxDuration": 0, "notOlderThan": 0},
        "h1": {"enabled": True, "maxDuration": 259200, "notOlderThan": 262800},
        "d1": {"enabled": True, "maxDuration": 31539600, "notOlderThan": 31539600},
    }}]}}
    main = _cf_main(totals=_cf_rollup_group(10, 5, requests=20))
    responses = [settings_resp,
                 main, None, _cf_breakdowns(), _cf_maps(),
                 main, None, _cf_breakdowns(), _cf_maps()]
    with patch.object(cloudflare_dashboard, "_post_graphql", side_effect=responses) as mock_gql:
        cloudflare_dashboard.build_bundle("tok", "z1", period="7d")
        cloudflare_dashboard.build_bundle("tok", "z1", period="7d")

    assert mock_gql.call_count == 9  # settings fired once, not twice
    settings_calls = [c for c in mock_gql.call_args_list if "settings" in c.args[1]]
    assert len(settings_calls) == 1


def test_cloudflare_bundle_none_on_failure():
    with _patched_settings(), \
         patch.object(cloudflare_dashboard, "_post_graphql", return_value=None):
        assert cloudflare_dashboard.build_bundle("tok", "z1", period="30d") is None


def test_suspicious_path_classifier():
    classify = cloudflare_dashboard._classify_suspicious
    assert classify("/wp-login.php") == "WordPress probe"
    assert classify("/wp-admin/install.php") == "WordPress probe"
    assert classify("/.env") == "Secrets/config probe"
    assert classify("/backup.sql") == "Secrets/config probe"
    assert classify("/phpmyadmin/index.php") == "Admin scan"
    assert classify("/_profiler/phpinfo") == "Admin scan"
    assert classify("/vendor/phpunit/whatever") == "Shell/exploit probe"
    # Legitimate content must never be flagged.
    assert classify("/pricing") is None
    assert classify("/blog/how-to-administer-medicine") is None
    assert classify("/contact.php") is None


def test_cloudflare_security_slice_in_overview():
    main = _cf_main(totals=_cf_rollup_group(800, 150, requests=2000))
    breakdowns = _cf_breakdowns(pages=[
        _cf_group(20, 0, 2, {"clientRequestPath": "/wp-login.php"}),
        _cf_group(10, 0, 2, {"clientRequestPath": "/pricing"}),
    ])
    maps = {"viewer": {"zones": [{"maps": [{"sum": {
        "threats": 37,
        "countryMap": [], "browserMap": [],
        "threatPathingMap": [{"threatPathingName": "user.ban.ip", "requests": 30}],
    }}]}]}}
    with _patched_settings(), \
         patch.object(cloudflare_dashboard, "_post_graphql",
                      side_effect=[main, None, breakdowns, maps]):
        bundle = cloudflare_dashboard.build_bundle("tok", "z1", period="7d")

    security = bundle["overview"]["security"]
    assert security["flagged"] == [{"url": "/wp-login.php", "requests": 40, "category": "WordPress probe"}]
    assert security["threats"] == 37
    assert security["threat_categories"] == [{"name": "user.ban.ip", "requests": 30}]
    # Flagged paths are kept OUT of Top Pages.
    assert bundle["pages"] == [{"url": "/pricing", "views": 20}]


# ── Resolver precedence ──────────────────────────────────────────────────────


@pytest.mark.django_db
class TestResolver:
    def test_pixel_data_wins(self):
        website = Ga4IntegrationFactory().website
        with patch.object(source_resolver, "_pixel_has_data", return_value=True):
            assert source_resolver.dashboard_slice(website, "overview", "30d") is None

    def test_nothing_connected_yields_pixel_path(self):
        website = WebsiteFactory()
        with patch.object(source_resolver, "_pixel_has_data", return_value=False):
            assert source_resolver.dashboard_slice(website, "overview", "30d") is None

    def test_feature_flag_off(self, settings):
        settings.WEB_ANALYTICS_ENABLED = False
        website = Ga4IntegrationFactory().website
        assert source_resolver.dashboard_slice(website, "overview", "30d") is None

    def test_ga4_bundle_served_and_cached(self):
        integration = Ga4IntegrationFactory()
        bundle = {"overview": {"total_visitors": 9, "data_source": "ga4"}, "chart": [1]}
        with patch.object(source_resolver, "_pixel_has_data", return_value=False), \
             patch.object(source_resolver.ga4_dashboard, "build_bundle", return_value=dict(bundle)) as mock_build:
            first = source_resolver.dashboard_slice(integration.website, "overview", "30d")
            second = source_resolver.dashboard_slice(integration.website, "chart", "30d")

        assert first["total_visitors"] == 9
        assert second == [1]
        mock_build.assert_called_once()  # six endpoints share one build

    def test_ga4_preferred_over_cloudflare(self):
        integration = Ga4IntegrationFactory()
        CloudflareIntegrationFactory(website=integration.website)
        with patch.object(source_resolver, "_pixel_has_data", return_value=False), \
             patch.object(source_resolver.ga4_dashboard, "build_bundle",
                          return_value={"overview": {"data_source": "ga4"}}) as ga_mock, \
             patch.object(source_resolver.cloudflare_dashboard, "build_bundle") as cf_mock:
            out = source_resolver.dashboard_slice(integration.website, "overview", "30d")
        assert out["data_source"] == "ga4"
        ga_mock.assert_called_once()
        cf_mock.assert_not_called()

    def test_cloudflare_when_only_zone_connected(self):
        integration = CloudflareIntegrationFactory()
        with patch.object(source_resolver, "_pixel_has_data", return_value=False), \
             patch.object(source_resolver.cloudflare_dashboard, "build_bundle",
                          return_value={"overview": {"data_source": "cloudflare"}}):
            out = source_resolver.dashboard_slice(integration.website, "overview", "30d")
        assert out["data_source"] == "cloudflare"

    def test_build_failure_falls_back_to_pixel_path(self):
        integration = Ga4IntegrationFactory()
        with patch.object(source_resolver, "_pixel_has_data", return_value=False), \
             patch.object(source_resolver.ga4_dashboard, "build_bundle", return_value=None):
            assert source_resolver.dashboard_slice(integration.website, "overview", "30d") is None


# ── Endpoint integration ─────────────────────────────────────────────────────


@pytest.mark.django_db
class TestAnalyticsEndpointsUseExternalSource:
    @pytest.fixture
    def auth_client(self):
        user = UserFactory()
        website = WebsiteFactory(user=user)
        client = APIClient()
        client.force_authenticate(user=user)
        return client, website

    def test_overview_and_chart_served_from_ga4(self, auth_client):
        client, website = auth_client
        Ga4IntegrationFactory(website=website)
        bundle = {
            "overview": {"total_visitors": 42, "data_source": "ga4"},
            "chart": [{"label": "Aug 25", "visitors": 42, "pageviews": 80, "sessions": 40}],
            "pages": [{"url": "/p", "views": 5}],
            "sources": [], "devices": {"devices": [], "browsers": [], "operating_systems": []},
            "countries": [],
        }
        with patch.object(source_resolver, "_pixel_has_data", return_value=False), \
             patch.object(source_resolver.ga4_dashboard, "build_bundle", return_value=bundle):
            overview = client.get(f"/api/v1/analytics/{website.id}/overview/", {"period": "30d"})
            chart = client.get(f"/api/v1/analytics/{website.id}/chart/", {"period": "30d"})
            pages = client.get(f"/api/v1/analytics/{website.id}/pages/", {"period": "30d"})

        assert overview.json()["data"]["total_visitors"] == 42
        assert overview.json()["data"]["data_source"] == "ga4"
        assert chart.json()["data"][0]["visitors"] == 42
        assert pages.json()["data"] == [{"url": "/p", "views": 5}]

    def test_pixel_path_untouched_when_pixel_has_data(self, auth_client):
        client, website = auth_client
        Ga4IntegrationFactory(website=website)
        with patch.object(source_resolver, "_pixel_has_data", return_value=True), \
             patch.object(source_resolver.ga4_dashboard, "build_bundle") as mock_build:
            resp = client.get(f"/api/v1/analytics/{website.id}/overview/", {"period": "30d"})

        mock_build.assert_not_called()
        data = resp.json()["data"]
        assert "data_source" not in data  # pixel payload, unlabeled
        assert data["total_visitors"] == 0

    def test_pixel_path_when_nothing_connected(self, auth_client):
        client, website = auth_client
        with patch.object(source_resolver, "_pixel_has_data", return_value=False):
            resp = client.get(f"/api/v1/analytics/{website.id}/overview/", {"period": "30d"})
        assert "data_source" not in resp.json()["data"]
