"""P0.6: citation URLs must be scheme-filtered before they are stored and
later rendered as clickable links (stored-XSS prevention)."""
from apps.citations.services.url_normalizer import normalize_url
from apps.llm_ranking.services.ranking_service import _merge_citation_lists


def test_merge_drops_dangerous_schemes():
    provider = [
        "https://good.example/a",
        "javascript:alert(1)",
        "data:text/html,<script>alert(1)</script>",
    ]
    extracted = ["http://good.example/b", "vbscript:x"]
    merged = _merge_citation_lists(provider, extracted)
    assert merged == ["https://good.example/a", "http://good.example/b"]


def test_merge_dedupes_and_preserves_provider_order():
    merged = _merge_citation_lists(
        ["https://a.example", "https://b.example"],
        ["https://a.example", "https://c.example"],
    )
    assert merged == ["https://a.example", "https://b.example", "https://c.example"]


def test_merge_handles_empty_and_non_strings():
    assert _merge_citation_lists(None, None) == []
    assert _merge_citation_lists([None, 123, ""], []) == []


def test_normalize_url_rejects_non_http_schemes():
    # normalize_url is the documented single normalization point for citation
    # URLs; a dangerous scheme must collapse to the empty triple.
    assert normalize_url("javascript:alert(1)") == ("", "", "")
    assert normalize_url("data:text/html,x") == ("", "", "")


def test_normalize_url_keeps_http_and_https():
    normalized, host, apex = normalize_url("https://Example.com/Path/")
    assert normalized == "https://example.com/Path"
    assert host == "example.com"
    assert apex == "example.com"
