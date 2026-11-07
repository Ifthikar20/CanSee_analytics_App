"""
Tests for the multi-page crawler.

The HTTP-fetching paths (``crawl_site``, ``discover_from_sitemap``,
``extract_same_domain_links``) are mocked at the SSRF-guarded
``core.validators.safe_http.safe_get`` boundary so the suite stays
hermetic and never hits the network. The crawler imports ``safe_get``
at call time, so patching the attribute on the ``safe_http`` module is
sufficient.
"""
from unittest.mock import MagicMock, patch

from apps.rag.services import crawler
from apps.rag.services.crawler import (
    _canonical,
    _is_asset,
    _kind_from_url,
    _normalize_domain,
    _parse_sitemap_xml,
    crawl_site,
    discover_from_sitemap,
    extract_same_domain_links,
)
from core.validators import safe_http
from core.validators.safe_http import FetchError


class TestSitemapParsing:
    def test_extracts_loc_entries(self):
        xml = (
            "<urlset>"
            "<url><loc>https://example.com/a</loc></url>"
            "<url><loc>https://example.com/b</loc></url>"
            "</urlset>"
        )
        urls = _parse_sitemap_xml(xml)
        assert urls == ["https://example.com/a", "https://example.com/b"]

    def test_handles_whitespace_and_case(self):
        xml = "<urlset><url><LOC>  https://example.com/c  </LOC></url></urlset>"
        urls = _parse_sitemap_xml(xml)
        assert urls == ["https://example.com/c"]

    def test_returns_empty_for_garbage(self):
        assert _parse_sitemap_xml("") == []
        assert _parse_sitemap_xml("not xml") == []
        assert _parse_sitemap_xml(None) == []


class TestKindFromURL:
    def test_homepage(self):
        assert _kind_from_url("https://example.com/") == "homepage"
        assert _kind_from_url("https://example.com") == "homepage"

    def test_blog(self):
        assert _kind_from_url("https://example.com/blog/post") == "blog"
        assert _kind_from_url("https://example.com/news/2024") == "blog"

    def test_docs(self):
        assert _kind_from_url("https://example.com/docs/api") == "docs"
        assert _kind_from_url("https://example.com/help") == "docs"

    def test_product(self):
        assert _kind_from_url("https://example.com/product/x") == "product"
        assert _kind_from_url("https://example.com/pricing") == "product"

    def test_review(self):
        assert _kind_from_url("https://example.com/customers") == "review"
        assert _kind_from_url("https://example.com/case-studies") == "review"

    def test_other(self):
        assert _kind_from_url("https://example.com/about") == "other"


class TestCanonicalAndDomain:
    def test_canonical_strips_fragment_and_trailing_slash(self):
        assert _canonical("https://EXAMPLE.com/foo/") == "https://example.com/foo"
        assert _canonical("https://example.com/foo#bar") == "https://example.com/foo"

    def test_normalize_domain_drops_www(self):
        assert _normalize_domain("www.Example.COM") == "example.com"
        assert _normalize_domain("example.com") == "example.com"
        assert _normalize_domain("") == ""

    def test_is_asset_detects_extensions(self):
        assert _is_asset("https://example.com/foo.png") is True
        assert _is_asset("https://example.com/foo.pdf") is True
        assert _is_asset("https://example.com/foo.css") is True
        assert _is_asset("https://example.com/foo") is False
        assert _is_asset("https://example.com/foo.html") is False


class TestDiscoverFromSitemap:
    def test_returns_urls_when_sitemap_succeeds(self):
        with patch.object(safe_http, "safe_get") as get:
            get.return_value = MagicMock(status_code=200, text=(
                "<urlset>"
                "<url><loc>https://example.com/a</loc></url>"
                "<url><loc>https://example.com/b</loc></url>"
                "</urlset>"
            ))
            urls = discover_from_sitemap("https://example.com")
            assert urls == ["https://example.com/a", "https://example.com/b"]

    def test_returns_empty_when_sitemap_404(self):
        with patch.object(safe_http, "safe_get") as get:
            get.return_value = MagicMock(status_code=404, text="")
            assert discover_from_sitemap("https://example.com") == []

    def test_swallows_fetch_errors(self):
        with patch.object(safe_http, "safe_get") as get:
            get.side_effect = FetchError("network down")
            assert discover_from_sitemap("https://example.com") == []


class TestExtractSameDomainLinks:
    def _make_response(self, html: str, *, url: str = "https://example.com"):
        return MagicMock(status_code=200, text=html, final_url=url)

    def test_returns_only_same_domain_links(self):
        html = (
            "<a href='/about'>About</a>"
            "<a href='https://example.com/blog'>Blog</a>"
            "<a href='https://other.com/x'>Other</a>"
            "<a href='https://www.example.com/contact'>Contact</a>"
        )
        with patch.object(safe_http, "safe_get") as get:
            get.return_value = self._make_response(html)
            links = extract_same_domain_links("https://example.com", "example.com")
        assert any("/about" in link for link in links)
        assert any("/blog" in link for link in links)
        assert any("/contact" in link for link in links)
        assert not any("other.com" in link for link in links)

    def test_skips_assets(self):
        html = "<a href='/x.pdf'>doc</a><a href='/foo'>foo</a>"
        with patch.object(safe_http, "safe_get") as get:
            get.return_value = self._make_response(html)
            links = extract_same_domain_links("https://example.com", "example.com")
        assert any("/foo" in link for link in links)
        assert not any(".pdf" in link for link in links)

    def test_skips_anchor_and_mailto(self):
        html = (
            "<a href='#section'>section</a>"
            "<a href='mailto:foo@bar.com'>mail</a>"
            "<a href='tel:+15551234'>tel</a>"
            "<a href='/foo'>foo</a>"
        )
        with patch.object(safe_http, "safe_get") as get:
            get.return_value = self._make_response(html)
            links = extract_same_domain_links("https://example.com", "example.com")
        assert len(links) == 1
        assert "/foo" in links[0]

    def test_dedupes_canonical_form(self):
        html = (
            "<a href='/foo/'>foo1</a>"
            "<a href='/foo'>foo2</a>"
            "<a href='/foo#anchor'>foo3</a>"
        )
        with patch.object(safe_http, "safe_get") as get:
            get.return_value = self._make_response(html)
            links = extract_same_domain_links("https://example.com", "example.com")
        # All three render to the same canonical URL — dedup keeps one.
        assert len(links) == 1


class TestCrawlSiteIntegration:
    def test_seed_only_when_depth_zero_and_no_sitemap(self):
        scan_result = {
            "success": True, "business_name": "Example",
            "content_summary": "homepage text", "url": "https://example.com",
        }
        with patch.object(crawler, "scan_domain", return_value=scan_result), \
             patch.object(crawler, "discover_from_sitemap", return_value=[]):
            results = crawl_site("https://example.com", depth=0, page_cap=5,
                                 include_sitemap=False)
        assert len(results) == 1
        assert results[0]["url"] == "https://example.com"
        assert results[0]["depth"] == 0

    def test_page_cap_respected(self):
        # Sitemap returns 50 URLs; page_cap=3 should keep only the seed + 2.
        many = [f"https://example.com/{i}" for i in range(50)]
        with patch.object(crawler, "scan_domain", return_value={
            "success": True, "content_summary": "x", "url": "https://example.com",
        }), patch.object(crawler, "discover_from_sitemap", return_value=many):
            results = crawl_site("https://example.com", page_cap=3,
                                 include_sitemap=True, depth=0)
        assert len(results) == 3

    def test_skips_seed_failures_for_link_expansion(self):
        # If scan_domain says success=False on the seed, we don't try to
        # extract links from it.
        with patch.object(crawler, "scan_domain", return_value={
            "success": False, "url": "https://example.com",
        }), patch.object(crawler, "discover_from_sitemap", return_value=[]), \
             patch.object(crawler, "extract_same_domain_links") as extract:
            crawl_site("https://example.com", depth=1, include_sitemap=False)
            extract.assert_not_called()

    def test_off_domain_sitemap_entries_filtered(self):
        # side_effect returns a fresh dict per call: crawl_site stamps
        # url/depth/kind onto the scan dict, so a shared return_value
        # would make every result alias the last page crawled.
        with patch.object(crawler, "scan_domain", side_effect=lambda url: {
            "success": True, "content_summary": "x",
        }), patch.object(crawler, "discover_from_sitemap", return_value=[
            "https://other.com/leak",
            "https://example.com/legit",
        ]):
            results = crawl_site("https://example.com", page_cap=5,
                                 include_sitemap=True, depth=0)
        urls = [r["url"] for r in results]
        assert "https://other.com/leak" not in urls
        assert any(u == "https://example.com" for u in urls)
