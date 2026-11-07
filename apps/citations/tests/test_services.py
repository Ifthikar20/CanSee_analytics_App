"""Unit + integration tests for citation services.

These tests exercise the URL normaliser, domain classifier, extractors,
and end-to-end orchestrator. The orchestrator test bypasses the Celery
hop by calling the service function directly.
"""
import pytest

from apps.citations.models import (
    Citation,
    DomainClassification,
    SourceClass,
    SourceInfluenceSnapshot,
)
from apps.citations.services.domain_classifier import classify
from apps.citations.services.extraction_service import extract_for_result
from apps.citations.services.extractors.gemini import GeminiGroundingExtractor
from apps.citations.services.extractors.perplexity import PerplexityNativeExtractor
from apps.citations.services.extractors.regex import RegexExtractor
from apps.citations.services.snapshot_service import (
    compute_audit_breakdown,
    compute_for_website,
)
from apps.citations.services.url_normalizer import (
    extract_apex_domain,
    normalize_url,
)
from apps.llm_ranking.models import LLMRankingResult
from apps.llm_ranking.tests.factories import (
    LLMRankingAuditFactory,
    LLMRankingResultFactory,
)
from apps.websites.tests.factories import WebsiteFactory

pytestmark = pytest.mark.django_db


# ---------------------------------------------------------------- normalizer
class TestNormalizeUrl:
    def test_strips_tracking_params(self):
        n, host, apex = normalize_url(
            "https://example.com/post?utm_source=x&utm_medium=y&id=42&fbclid=abc"
        )
        assert "utm_" not in n
        assert "fbclid" not in n
        assert "id=42" in n
        assert host == "example.com"
        assert apex == "example.com"

    def test_lowercases_host_strips_default_port_and_fragment(self):
        n, host, _ = normalize_url("HTTPS://Example.COM:443/Path/#frag")
        assert n == "https://example.com/Path"
        assert host == "example.com"

    def test_apex_for_subdomain(self):
        _, host, apex = normalize_url("https://blog.mixpanel.com/article")
        assert host == "blog.mixpanel.com"
        assert apex == "mixpanel.com"

    def test_apex_two_label_tld_fallback(self):
        # Without tldextract this exercises the static suffix list.
        apex = extract_apex_domain("news.bbc.co.uk")
        assert apex == "bbc.co.uk"

    def test_idempotent(self):
        n1, _, _ = normalize_url("https://Example.com/?utm_source=a")
        n2, _, _ = normalize_url(n1)
        assert n1 == n2

    def test_empty(self):
        assert normalize_url("") == ("", "", "")


# ----------------------------------------------------------- classifier rules
class TestDomainClassifier:
    def test_reddit(self):
        assert classify("reddit.com", host="www.reddit.com") == SourceClass.REDDIT

    def test_wikipedia(self):
        assert classify("wikipedia.org", host="en.wikipedia.org") == SourceClass.WIKIPEDIA

    def test_youtube(self):
        assert classify("youtube.com", host="www.youtube.com") == SourceClass.YOUTUBE
        assert classify("youtu.be", host="youtu.be") == SourceClass.YOUTUBE

    def test_news_starter_list(self):
        assert classify("techcrunch.com", host="techcrunch.com") == SourceClass.NEWS

    def test_blog_subdomain(self):
        assert classify("acme.com", host="blog.acme.com") == SourceClass.BLOG

    def test_docs_subdomain(self):
        assert classify("acme.com", host="docs.acme.com") == SourceClass.DOCS

    def test_gov_tld(self):
        assert classify("whitehouse.gov", host="www.whitehouse.gov") == SourceClass.GOV

    def test_edu_tld(self):
        assert classify("mit.edu", host="mit.edu") == SourceClass.EDU

    def test_unknown_falls_through_to_other(self):
        assert classify("totally-random-xyz.io", host="totally-random-xyz.io") == SourceClass.OTHER

    def test_cache_persists_and_short_circuits(self):
        DomainClassification.objects.all().delete()
        classify("techcrunch.com", host="techcrunch.com")
        row = DomainClassification.objects.get(apex_domain="techcrunch.com")
        assert row.source_class == SourceClass.NEWS
        # Subsequent call returns the cached value.
        assert classify("techcrunch.com", host="techcrunch.com") == SourceClass.NEWS

    def test_your_site_override(self):
        website = WebsiteFactory(url="https://example.com")
        assert classify("example.com", host="www.example.com", website=website) == SourceClass.YOUR_SITE

    def test_competitor_site_override(self):
        assert classify(
            "competitor.io",
            host="competitor.io",
            competitor_apexes={"competitor.io"},
        ) == SourceClass.COMPETITOR_SITE


# ---------------------------------------------------------------- extractors
class _StubResult:
    def __init__(self, *, response_text="", citations=None, grounding_chunks=None):
        self.response_text = response_text
        self.citations = citations or []
        self.grounding_chunks = grounding_chunks or []


class TestExtractors:
    def test_perplexity_native(self):
        r = _StubResult(citations=[
            {"url": "https://reddit.com/r/saas", "title": "thread"},
            "https://techcrunch.com/x",
        ])
        out = PerplexityNativeExtractor().extract(r)
        assert [c.url for c in out] == [
            "https://reddit.com/r/saas",
            "https://techcrunch.com/x",
        ]
        assert out[0].confidence == 1.0

    def test_gemini_grounding_chunks(self):
        r = _StubResult(grounding_chunks=[
            {"web": {"uri": "https://wikipedia.org/wiki/SaaS", "title": "SaaS"}},
            {"web": {"uri": "https://example.com", "title": "ex"}},
        ])
        out = GeminiGroundingExtractor().extract(r)
        assert {c.url for c in out} == {
            "https://wikipedia.org/wiki/SaaS",
            "https://example.com",
        }

    def test_regex_finds_urls_and_dedupes(self):
        r = _StubResult(
            response_text=(
                "See https://reddit.com/a and https://reddit.com/a and "
                "also (https://example.com/b)."
            )
        )
        out = RegexExtractor().extract(r)
        urls = [c.url for c in out]
        assert "https://reddit.com/a" in urls
        assert "https://example.com/b" in urls
        # Dedup
        assert len(urls) == 2


# ------------------------------------------------------- extraction service E2E
class TestExtractForResult:
    @pytest.fixture(autouse=True)
    def _no_auto_extraction(self, settings):
        # pytest 8 rejects override_settings on plain classes; the
        # pytest-django settings fixture is the supported equivalent.
        settings.CITATION_EXTRACTION_ENABLED = False

    def test_creates_citations_and_classifies(self):
        result = LLMRankingResultFactory(
            provider=LLMRankingResult.PROVIDER_PERPLEXITY,
            citations=[
                {"url": "https://reddit.com/r/saas", "title": "t"},
                {"url": "https://techcrunch.com/article", "title": "tc"},
            ],
            response_text="See https://en.wikipedia.org/wiki/SaaS for more.",
        )
        n = extract_for_result(str(result.id))
        assert n == 3
        rows = Citation.objects.filter(result=result).order_by("apex_domain")
        classes = {r.apex_domain: r.source_class for r in rows}
        assert classes["reddit.com"] == SourceClass.REDDIT
        assert classes["techcrunch.com"] == SourceClass.NEWS
        assert classes["wikipedia.org"] == SourceClass.WIKIPEDIA

    def test_idempotent(self):
        result = LLMRankingResultFactory(
            provider=LLMRankingResult.PROVIDER_PERPLEXITY,
            citations=[{"url": "https://reddit.com/x"}],
            response_text="",
        )
        extract_for_result(str(result.id))
        extract_for_result(str(result.id))
        assert Citation.objects.filter(result=result).count() == 1


# ---------------------------------------------------------- snapshot rollups
class TestSnapshots:
    @pytest.fixture(autouse=True)
    def _no_auto_extraction(self, settings):
        settings.CITATION_EXTRACTION_ENABLED = False

    def test_compute_audit_breakdown(self):
        audit = LLMRankingAuditFactory()
        r1 = LLMRankingResultFactory(
            audit=audit, provider="perplexity", prompt_index=0,
            citations=[{"url": "https://reddit.com/x"}],
        )
        r2 = LLMRankingResultFactory(
            audit=audit, provider="claude", prompt_index=1,
            response_text="see https://techcrunch.com/y",
        )
        extract_for_result(str(r1.id))
        extract_for_result(str(r2.id))

        out = compute_audit_breakdown(audit)
        assert out["total_citations"] == 2
        assert out["unique_domains"] == 2
        assert "perplexity" in out["providers"]
        assert "claude" in out["providers"]

    def test_compute_for_website_creates_snapshot(self):
        audit = LLMRankingAuditFactory()
        r = LLMRankingResultFactory(
            audit=audit, provider="perplexity", prompt_index=0,
            citations=[{"url": "https://reddit.com/x"}],
        )
        extract_for_result(str(r.id))
        n = compute_for_website(audit.website, period_days=30)
        assert n >= 1
        snap = SourceInfluenceSnapshot.objects.get(
            website=audit.website, provider="perplexity",
        )
        assert snap.total_citations == 1
        assert "reddit" in snap.breakdown
        assert snap.breakdown["reddit"]["count"] == 1
