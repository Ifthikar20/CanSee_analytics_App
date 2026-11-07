"""Tests for the website logo crawler."""
from unittest.mock import MagicMock, patch

import pytest
from django.core.cache import cache

from apps.prompt_library.services import logo_crawler


@pytest.fixture(autouse=True)
def _clear_cache():
    cache.clear()
    yield
    cache.clear()


class TestNormaliseDomain:
    def test_strips_scheme_path_www(self):
        assert logo_crawler.normalise_domain("https://www.Tassels.com/shop") == "tassels.com"

    def test_bare_domain(self):
        assert logo_crawler.normalise_domain("indiabazaar.com") == "indiabazaar.com"

    def test_rejects_garbage(self):
        assert logo_crawler.normalise_domain("not a domain") is None
        assert logo_crawler.normalise_domain("") is None


class TestExtractLogoCandidates:
    def test_prefers_apple_touch_icon(self):
        head = (
            '<link rel="icon" href="/favicon.ico">'
            '<meta property="og:image" content="https://x.com/og.png">'
            '<link rel="apple-touch-icon" sizes="180x180" href="/touch.png">'
        )
        out = logo_crawler._extract_logo_candidates(head)
        assert out[0] == "/touch.png"
        assert "https://x.com/og.png" in out
        assert "/favicon.ico" in out

    def test_picks_img_with_logo_class(self):
        head = '<img class="site-logo" src="/img/logo.svg" alt="Acme">'
        out = logo_crawler._extract_logo_candidates(head)
        assert "/img/logo.svg" in out


@pytest.mark.django_db
class TestFetchSiteLogo:
    def test_invalid_domain_returns_empty(self):
        assert logo_crawler.fetch_site_logo("nope") == ""

    def test_resolves_and_caches(self):
        # The crawler now fetches through the SSRF-guarded safe_get, which
        # returns a SafeResponse (.status_code / .text / .final_url).
        html = '<html><head><link rel="apple-touch-icon" href="/logo.png"></head>'
        fake_resp = MagicMock(status_code=200, text=html, final_url="https://acme.com/")

        with patch.object(logo_crawler, "_is_public_host", return_value=True), \
             patch("core.validators.safe_http.safe_get", return_value=fake_resp) as mock_get:
            first = logo_crawler.fetch_site_logo("acme.com")
            # Second call is served from cache (no extra fetch).
            second = logo_crawler.fetch_site_logo("acme.com")

        assert first == "https://acme.com/logo.png"
        assert second == first
        assert mock_get.call_count == 1

    def test_refuses_private_host(self):
        with patch.object(logo_crawler, "_is_public_host", return_value=False):
            assert logo_crawler.fetch_site_logo("internal.example") == ""
