"""Tests for the Brand Research AI-engine lane.

Providers are mocked at the registry level. config/settings/test.py sets
BRAND_RESEARCH_ENGINES_ENABLED = False, so tests that exercise the lane
re-enable it explicitly -- an un-mocked path would otherwise make real,
billable calls to whatever keys are in the developer's .env.
"""

from unittest.mock import patch

import pytest

from apps.citations.models import SourceScan, SourceScanEngineAnswer
from apps.citations.services import engine_probe, source_scan
from apps.websites.tests.factories import WebsiteFactory


class _Result:
    """Stand-in for core.llm.base.ProviderResult."""

    def __init__(self, *, succeeded=True, text="", error="", citations=None):
        self.succeeded = succeeded
        self.text = text
        self.error = error
        self.citations = citations or []


def _provider(*, configured=True, result=None, model="fake-1", raises=None):
    class _Fake:
        def __init__(self):
            self.model = model

        def is_configured(self):
            return configured

        def query(self, prompt, **kwargs):
            if raises:
                raise raises
            return result or _Result(text="I would recommend Starship Bagel.")

    return _Fake


ANALYSIS = {
    "relevant_to_query": True,
    "brands": [{"name": "Starship Bagel", "mentions": 1, "sentiment": 0.8,
                "weight": 0.9, "quotes": [], "issues": []}],
}

ANALYZE = "apps.citations.services.source_sentiment.analyze_content"


# -- per-engine behaviour ------------------------------------------------------


@pytest.mark.django_db
def test_configured_engine_yields_ok_row(settings):
    settings.BRAND_RESEARCH_ENGINES_ENABLED = True
    website = WebsiteFactory(name="Brooklyn Bagel Co")
    with patch(ANALYZE, return_value=ANALYSIS):
        row = engine_probe.probe_engine(
            "claude", _provider(),
            query="best bagels in dallas", target_brand="Brooklyn Bagel Co",
            website=website, user=None,
        )
    assert row["status"] == engine_probe.STATUS_OK
    assert row["model"] == "fake-1"
    assert row["brands"][0]["name"] == "Starship Bagel"


@pytest.mark.django_db
def test_unconfigured_engine_is_recorded_not_dropped(settings):
    """The UI greys out an engine with no key. Dropping the row instead would
    read as 'this engine had nothing to say', which is a different claim."""
    settings.BRAND_RESEARCH_ENGINES_ENABLED = True
    row = engine_probe.probe_engine(
        "grok", _provider(configured=False),
        query="q", target_brand="", website=None, user=None,
    )
    assert row["status"] == engine_probe.STATUS_NOT_CONFIGURED
    assert row["brands"] == []


@pytest.mark.django_db
def test_raising_engine_fails_alone(settings):
    settings.BRAND_RESEARCH_ENGINES_ENABLED = True
    row = engine_probe.probe_engine(
        "gemini", _provider(raises=RuntimeError("upstream 500")),
        query="q", target_brand="", website=None, user=None,
    )
    assert row["status"] == engine_probe.STATUS_FAILED
    assert "upstream 500" in row["error"]


@pytest.mark.django_db
def test_unsuccessful_result_is_failed_not_ok(settings):
    settings.BRAND_RESEARCH_ENGINES_ENABLED = True
    row = engine_probe.probe_engine(
        "gpt4", _provider(result=_Result(succeeded=False, error="rate limited")),
        query="q", target_brand="", website=None, user=None,
    )
    assert row["status"] == engine_probe.STATUS_FAILED
    assert row["error"] == "rate limited"


@pytest.mark.django_db
def test_probe_all_covers_every_registry_entry(settings):
    settings.BRAND_RESEARCH_ENGINES_ENABLED = True
    registry = {
        "claude": _provider(),
        "gpt4": _provider(configured=False),
        "grok": _provider(raises=RuntimeError("boom")),
    }
    with patch("apps.llm_ranking.providers.PROVIDERS", registry), \
         patch(ANALYZE, return_value=ANALYSIS):
        rows = engine_probe.probe_all(
            query="q", target_brand="", website=None, user=None,
        )
    by_provider = {r["provider"]: r["status"] for r in rows}
    assert by_provider == {
        "claude": engine_probe.STATUS_OK,
        "gpt4": engine_probe.STATUS_NOT_CONFIGURED,
        "grok": engine_probe.STATUS_FAILED,
    }


def test_probe_all_disabled_is_a_no_op(settings):
    settings.BRAND_RESEARCH_ENGINES_ENABLED = False

    def _boom(*args, **kwargs):  # pragma: no cover - must never be reached
        raise AssertionError("the disabled lane must not call out")

    with patch("apps.llm_ranking.providers.PROVIDERS", {"claude": _boom}):
        assert engine_probe.probe_all(
            query="q", target_brand="", website=None, user=None,
        ) == []


# -- citation extraction + cross-link ------------------------------------------


def test_extract_citations_merges_native_and_inline():
    out = engine_probe.extract_citations(
        "Try https://starshipbagel.com and also https://example.com/guide.",
        [{"url": "https://www.reddit.com/r/Dallas/comments/abc/", "title": "Thread"}],
    )
    urls = [c["url"] for c in out]
    assert "https://www.reddit.com/r/Dallas/comments/abc/" in urls
    assert "https://starshipbagel.com" in urls
    # The trailing sentence period must not become part of the URL.
    assert "https://example.com/guide" in urls
    assert out[0]["domain"] == "reddit.com"


def test_extract_citations_dedupes_across_extractors():
    out = engine_probe.extract_citations(
        "See https://example.com/a?utm_source=x for details.",
        [{"url": "https://example.com/a"}],
    )
    assert len(out) == 1, "the same page cited natively and inline is one citation"


class _Row:
    def __init__(self, url, rank):
        self.url = url
        self.rank = rank


def test_link_citations_matches_scan_sources():
    engine_rows = [
        {"provider": "perplexity", "citations": [
            # Same page as source rank 2, with tracking params and a slash.
            {"url": "https://example.com/guide/?utm_source=pplx"},
            {"url": "https://novel-site.com/unseen"},
        ]},
        {"provider": "claude", "citations": [{"url": "https://nowhere.com/x"}]},
    ]
    sources = [_Row("https://reddit.com/r/x/", 1), _Row("https://example.com/guide", 2)]

    linked = engine_probe.link_citations_to_rows(engine_rows, sources)

    assert linked == {"perplexity": [2]}, "only URLs the scan also found link back"


def test_ai_overview_becomes_an_engine_row():
    with patch(ANALYZE, return_value=ANALYSIS):
        row = engine_probe.row_from_ai_overview(
            {"text": "Starship Bagel is widely recommended.",
             "references": [{"url": "https://starshipbagel.com",
                             "domain": "starshipbagel.com", "title": "Starship"}]},
            query="q", target_brand="", website=None, user=None,
        )
    assert row["provider"] == "google_ai_overview"
    assert row["status"] == engine_probe.STATUS_OK
    assert row["brands"][0]["name"] == "Starship Bagel"
    assert row["citations"][0]["domain"] == "starshipbagel.com"


def test_empty_ai_overview_is_no_row():
    assert engine_probe.row_from_ai_overview(
        {}, query="q", target_brand="", website=None, user=None,
    ) is None


# -- lane integration ----------------------------------------------------------


@pytest.mark.django_db
def test_engine_lane_persists_rows_and_reports_stage(settings):
    settings.BRAND_RESEARCH_ENGINES_ENABLED = True
    website = WebsiteFactory(name="Brooklyn Bagel Co")
    scan = SourceScan.objects.create(website=website, query="best bagels in dallas")
    source_scan._init_stages(scan)

    registry = {"claude": _provider(), "grok": _provider(configured=False)}
    with patch("apps.llm_ranking.providers.PROVIDERS", registry), \
         patch(ANALYZE, return_value=ANALYSIS):
        source_scan._run_engine_lane(scan, website, "Brooklyn Bagel Co")

    scan.refresh_from_db()
    answers = {a.provider: a for a in SourceScanEngineAnswer.objects.filter(scan=scan)}
    assert set(answers) == {"claude", "grok"}
    assert answers["claude"].status == "ok"
    assert answers["grok"].status == "not_configured"
    # count is answers that actually came back, not rows written.
    assert scan.stages["engines"]["status"] == source_scan.COMPLETE
    assert scan.stages["engines"]["count"] == 1


@pytest.mark.django_db
def test_engine_lane_disabled_reports_skipped(settings):
    settings.BRAND_RESEARCH_ENGINES_ENABLED = False
    website = WebsiteFactory()
    scan = SourceScan.objects.create(website=website, query="q")
    source_scan._init_stages(scan)

    source_scan._run_engine_lane(scan, website, "")

    scan.refresh_from_db()
    assert scan.stages["engines"]["status"] == source_scan.SKIPPED
    assert SourceScanEngineAnswer.objects.filter(scan=scan).count() == 0


@pytest.mark.django_db
def test_engine_recommendation_feeds_share_of_voice(settings):
    """An engine naming a brand must move share of voice even when no page
    in the scan mentions it -- that is the whole point of the lane."""
    settings.BRAND_RESEARCH_ENGINES_ENABLED = True
    website = WebsiteFactory(name="Brooklyn Bagel Co")
    scan = SourceScan.objects.create(website=website, query="q")
    SourceScanEngineAnswer.objects.create(
        scan=scan, provider="claude", model="fake-1", status="ok",
        answer_text="Starship Bagel.",
        brands=[{"name": "Starship Bagel", "mentions": 2, "sentiment": 0.7,
                 "weight": 0.8, "quotes": [], "issues": []}],
    )

    rollup = source_scan._aggregate(scan, "Brooklyn Bagel Co")

    assert rollup[0]["name"] == "Starship Bagel"
    assert rollup[0]["engines"] == ["claude"]
    assert rollup[0]["engine_mentions"] == 1
    assert rollup[0]["weighted_score"] > 0


@pytest.mark.django_db
def test_failed_engine_answer_is_excluded_from_rollup(settings):
    website = WebsiteFactory(name="Brooklyn Bagel Co")
    scan = SourceScan.objects.create(website=website, query="q")
    SourceScanEngineAnswer.objects.create(
        scan=scan, provider="grok", status="not_configured",
        brands=[{"name": "Ghost Brand", "mentions": 9, "sentiment": 1.0, "weight": 1.0}],
    )
    assert source_scan._aggregate(scan, "Brooklyn Bagel Co") == []
