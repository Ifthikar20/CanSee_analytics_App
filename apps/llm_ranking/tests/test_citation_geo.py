"""Tests for ccTLD-based citation country attribution."""
from apps.llm_ranking.services.citation_geo import (
    aggregate_countries,
    attribute_country,
    merge_country_counts,
)


class TestAttributeCountry:
    def test_simple_cctld(self):
        assert attribute_country("https://example.in/article") == "IN"
        assert attribute_country("https://example.de") == "DE"
        assert attribute_country("https://example.fr/foo/bar") == "FR"

    def test_multipart_cctld(self):
        assert attribute_country("https://shop.example.com.au") == "AU"
        assert attribute_country("https://news.example.co.uk/article") == "GB"

    def test_known_global_domain(self):
        assert attribute_country("https://www.g2.com/products/analytics") == "US"
        assert attribute_country("https://medium.com/@user") == "US"
        assert attribute_country("https://yourstory.com/2024/article") == "IN"

    def test_known_subdomain_resolves_to_root(self):
        # A subdomain of a known global domain resolves to the parent's country.
        assert attribute_country("https://blog.g2.com/post") == "US"

    def test_strips_www(self):
        assert attribute_country("https://www.example.in") == "IN"

    def test_unknown_returns_none(self):
        # ``.com`` alone — we don't guess US for everything.
        assert attribute_country("https://example.com") is None

    def test_empty_returns_none(self):
        assert attribute_country("") is None
        assert attribute_country(None) is None


class TestAggregateCountries:
    def test_counts_per_country(self):
        urls = [
            "https://g2.com/x",
            "https://example.in/y",
            "https://example.in/z",
            "https://example.com",  # unknown — skipped
        ]
        result = aggregate_countries(urls)
        assert result == {"US": 1, "IN": 2}

    def test_empty_returns_empty_dict(self):
        assert aggregate_countries([]) == {}
        assert aggregate_countries(None) == {}


class TestMergeCountryCounts:
    def test_sums_counts_across_results(self):
        merged = merge_country_counts(
            {"US": 3, "IN": 1},
            {"US": 2, "DE": 1},
            {"IN": 4},
        )
        assert merged == {"US": 5, "IN": 5, "DE": 1}

    def test_handles_empty_inputs(self):
        assert merge_country_counts() == {}
        assert merge_country_counts({}, None, {"US": 1}) == {"US": 1}
