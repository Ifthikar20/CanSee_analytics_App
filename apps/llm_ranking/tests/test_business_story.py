"""
Tests for ``apps.llm_ranking.services.business_story``.

The HTML parser and JSON-LD walker run synchronously and have no
external deps, so we exercise them directly. ``gather_story`` is
tested with ``safe_get`` patched out.
"""
from __future__ import annotations

from unittest.mock import patch

from apps.llm_ranking.services import business_story as bs


def _parse(html: str) -> bs._StructuredDataExtractor:
    parser = bs._StructuredDataExtractor()
    parser.feed(html)
    return parser


def test_extracts_open_graph_and_twitter_meta():
    html = """
    <html><head>
      <meta property="og:title" content="Acme — buy widgets fast">
      <meta property="og:description" content="Best widgets in town.">
      <meta name="twitter:card" content="summary_large_image">
      <meta name="twitter:site" content="@acme">
      <meta name="author" content="Jane Doe">
      <meta name="generator" content="Hugo 0.120">
      <meta name="theme-color" content="#FF6A2C">
      <link rel="canonical" href="https://acme.example/">
    </head></html>
    """
    p = _parse(html)
    assert p.og["title"].startswith("Acme")
    assert p.og["description"] == "Best widgets in town."
    assert p.twitter["card"] == "summary_large_image"
    assert p.twitter["site"] == "@acme"
    assert p.author == "Jane Doe"
    assert p.generator == "Hugo 0.120"
    assert p.theme_color == "#FF6A2C"
    assert p.canonical == "https://acme.example/"


def test_captures_social_email_and_tel_links():
    html = """
    <html><body>
      <a href="mailto:hello@acme.example">Email us</a>
      <a href="tel:+15551234567">Call</a>
      <a href="https://twitter.com/acme">Twitter</a>
      <a href="https://www.linkedin.com/company/acme">LinkedIn</a>
      <a href="https://github.com/acme/widget">GitHub</a>
      <a href="https://example.com/not-a-social">Random</a>
    </body></html>
    """
    p = _parse(html)
    assert "hello@acme.example" in p.contact_emails
    assert "+15551234567" in p.contact_phones
    socials = " ".join(p.social_links)
    assert "twitter.com/acme" in socials
    assert "linkedin.com" in socials
    assert "github.com" in socials
    # Non-social link must NOT be captured
    assert "example.com/not-a-social" not in socials


def test_captures_pricing_snippets():
    html = """
    <html><body>
      <p>Pro plan is $49/month for unlimited audits.</p>
      <p>Annual: $490/year — save 16%.</p>
      <p>Just text with $99 mentioned.</p>
    </body></html>
    """
    p = _parse(html)
    blob = " ".join(p.pricing_snippets)
    assert "$49" in blob
    assert "$490" in blob


def test_jsonld_organization_product_faq_review():
    html = """
    <html><head><script type="application/ld+json">
    {
      "@context": "https://schema.org",
      "@graph": [
        {"@type": "Organization", "name": "Acme",
         "description": "Widget maker", "url": "https://acme.example",
         "sameAs": ["https://twitter.com/acme"]},
        {"@type": "Product", "name": "Widget Pro",
         "description": "Premium widget",
         "offers": {"@type": "Offer", "price": "49", "priceCurrency": "USD"}},
        {"@type": "FAQPage", "mainEntity": [
          {"@type": "Question", "name": "How fast?",
           "acceptedAnswer": {"@type": "Answer", "text": "Within 24 hours."}}
        ]},
        {"@type": "AggregateRating", "ratingValue": 4.7, "reviewCount": 312}
      ]
    }
    </script></head></html>
    """
    p = _parse(html)
    assert len(p.json_ld_blocks) == 1
    parsed = bs._parse_jsonld(p.json_ld_blocks)
    assert parsed["organization"][0]["name"] == "Acme"
    assert parsed["products"][0]["name"] == "Widget Pro"
    assert parsed["products"][0]["offers"][0]["price"] == "49"
    assert parsed["faq"][0]["q"] == "How fast?"
    assert "24 hours" in parsed["faq"][0]["a"]
    assert parsed["rating"]["value"] == 4.7
    assert parsed["rating"]["count"] == 312


def test_jsonld_handles_malformed_blocks_without_raising():
    html = """
    <html><head>
      <script type="application/ld+json">{ this is not json </script>
      <script type="application/ld+json">{"@type": "Organization", "name": "OK"}</script>
    </head></html>
    """
    p = _parse(html)
    parsed = bs._parse_jsonld(p.json_ld_blocks)
    # The valid block must still come through.
    assert any(o["name"] == "OK" for o in parsed["organization"])


def test_render_story_assembles_sections():
    story = {
        "url": "https://acme.example/",
        "canonical": "https://acme.example/",
        "og": {"title": "Acme", "description": "Best widgets"},
        "twitter": {"site": "@acme"},
        "author": "Jane",
        "generator": "Hugo",
        "theme_color": "#FF6A2C",
        "structured": {
            "organization": [{"name": "Acme", "description": "Widgets",
                              "url": "https://acme.example", "logo": "",
                              "sameAs": []}],
            "products": [{"name": "Widget Pro", "description": "Premium",
                          "category": "", "offers": []}],
            "offers": [],
            "faq": [{"q": "How fast?", "a": "Within 24h."}],
            "reviews": [],
            "rating": {"value": 4.7, "count": 100, "best": 5},
        },
        "contacts": {"emails": ["hello@acme.example"], "phones": []},
        "social_links": ["https://twitter.com/acme"],
        "pricing_snippets": ["$49/month"],
    }
    rendered = bs.render_story(story, business_name="Acme", industry="SaaS")
    assert "Acme" in rendered
    assert "Widget Pro" in rendered
    assert "How fast?" in rendered
    assert "$49/month" in rendered
    assert "hello@acme.example" in rendered
    assert "twitter.com/acme" in rendered


def test_render_story_caps_total_length():
    story = {
        "url": "https://x.example/",
        "canonical": "", "og": {}, "twitter": {}, "author": "", "generator": "",
        "theme_color": "",
        "structured": {
            "organization": [], "products": [], "offers": [],
            "faq": [{"q": "Q" * 300, "a": "A" * 400} for _ in range(40)],
            "reviews": [], "rating": None,
        },
        "contacts": {"emails": [], "phones": []},
        "social_links": [],
        "pricing_snippets": [],
    }
    rendered = bs.render_story(story)
    assert len(rendered) <= bs._MAX_STORY_CHARS


def test_gather_story_returns_error_dict_on_fetch_failure():
    from core.validators.safe_http import FetchError

    with patch("core.validators.safe_http.safe_get",
               side_effect=FetchError("blocked: private IP")):
        out = bs.gather_story("http://10.0.0.1/")
    assert "error" in out
    assert "blocked" in out["error"]


def test_gather_story_returns_error_dict_on_http_4xx():
    class _R:
        text = ""
        status_code = 404
        final_url = "https://x.example/"

    with patch("core.validators.safe_http.safe_get", return_value=_R()):
        out = bs.gather_story("https://x.example/")
    assert out["error"] == "HTTP 404"


def test_gather_story_extracts_from_html():
    html = """
    <html><head>
      <meta property="og:title" content="Acme">
      <script type="application/ld+json">
        {"@type": "Organization", "name": "Acme Inc"}
      </script>
    </head><body>
      <a href="https://twitter.com/acme">x</a>
      <a href="mailto:hi@acme.example">e</a>
      <p>Pro $49/month plan</p>
    </body></html>
    """

    class _R:
        text = html
        status_code = 200
        final_url = "https://acme.example/"

    with patch("core.validators.safe_http.safe_get", return_value=_R()):
        out = bs.gather_story("https://acme.example/")

    assert out["og"]["title"] == "Acme"
    assert out["structured"]["organization"][0]["name"] == "Acme Inc"
    assert "https://twitter.com/acme" in out["social_links"]
    assert "hi@acme.example" in out["contacts"]["emails"]
    assert any("$49" in s for s in out["pricing_snippets"])


def test_dedupe_preserve_order_keeps_first_occurrence():
    items = ["a", "b", "a", "c", "b"]
    assert bs._dedupe_preserve_order(items) == ["a", "b", "c"]
