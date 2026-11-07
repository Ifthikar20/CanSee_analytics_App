"""Tests for the Brand Research discovery lanes.

Covers the Google SerpAPI block parsing, the two-index discovery merge, and
the Reddit community lane. All offline: every client is mocked, and
config/settings/test.py blanks SERPAPI_KEY and disables the community lane
so an un-mocked path fails loudly instead of making a real request.
"""

from unittest.mock import MagicMock, patch

from apps.citations.services import community, serp_google


def _resp(payload, status_code=200):
    resp = MagicMock()
    resp.status_code = status_code
    resp.raise_for_status = MagicMock()
    resp.json = MagicMock(return_value=payload)
    return resp


FULL_SERP = {
    "organic_results": [
        {"link": "https://dallasobserver.com/best-bagels", "title": "Ranked",
         "snippet": "Our picks.", "date": "2025-05-10"},
        {"link": "https://example.com/guide", "title": "Guide", "snippet": "..."},
        # Dropped: a non-http scheme must never reach the UI as a link.
        {"link": "javascript:alert(1)", "title": "bad"},
        {"title": "no link at all"},
    ],
    "discussions_and_forums": [
        {"link": "https://www.reddit.com/r/Dallas/comments/abc/bagels/",
         "title": "Best bagels??", "snippet": "Starship.",
         "source": "Reddit", "comment_count": "1.2K comments"},
        {"link": "https://www.quora.com/best-bagels", "title": "Quora thread",
         "extensions": {"comments": 42}},
    ],
    "related_questions": [
        {"question": "What is the best bagel in Dallas?", "snippet": "Starship.",
         "link": "https://example.com/paa"},
        {"question": ""},
    ],
    "related_searches": [
        {"query": "best bagels dallas reddit"},
        {"query": "Best Bagels Dallas Reddit"},  # case-duplicate, dropped
        "boiled bagels dallas",
    ],
    "ai_overview": {
        "text_blocks": [
            {"snippet": "Dallas has several standout bagel shops."},
            {"list": [{"snippet": "Starship Bagel is widely recommended."}]},
        ],
        "references": [
            {"link": "https://starshipbagel.com", "title": "Starship Bagel"},
            {"link": "not a url", "title": "junk"},
        ],
    },
    "knowledge_graph": {
        "title": "Starship Bagel", "type": "Bagel shop",
        "website": "https://starshipbagel.com", "description": "Wood-fired bagels.",
    },
}


def _search(payload, status_code=200, *, settings):
    settings.SERPAPI_KEY = "serp-test"
    with patch("requests.get", return_value=_resp(payload, status_code)):
        return serp_google.search("best bagels in dallas")


# -- serp_google: block parsing ------------------------------------------------


def test_parses_every_block(settings):
    out = _search(FULL_SERP, settings=settings)

    assert out["error"] == ""
    # Unusable URLs are dropped and the surviving ranks stay dense.
    assert [r["rank"] for r in out["organic"]] == [1, 2]
    assert out["organic"][0]["domain"] == "dallasobserver.com"

    assert len(out["discussions"]) == 2
    assert out["discussions"][0]["comment_count"] == 1200  # "1.2K comments"
    assert out["discussions"][1]["comment_count"] == 42    # nested in extensions

    assert out["questions"] == [{
        "question": "What is the best bagel in Dallas?",
        "snippet": "Starship.",
        "url": "https://example.com/paa",
        "domain": "example.com",
    }]
    assert out["related_searches"] == ["best bagels dallas reddit", "boiled bagels dallas"]

    assert "standout bagel shops" in out["ai_overview"]["text"]
    assert "widely recommended" in out["ai_overview"]["text"]
    assert [r["url"] for r in out["ai_overview"]["references"]] == ["https://starshipbagel.com"]

    assert out["knowledge_graph"]["website"] == "https://starshipbagel.com"
    assert out["knowledge_graph"]["type"] == "Bagel shop"


def test_organic_only_leaves_other_blocks_empty(settings):
    out = _search({"organic_results": FULL_SERP["organic_results"]}, settings=settings)
    assert len(out["organic"]) == 2
    assert out["discussions"] == []
    assert out["questions"] == []
    assert out["related_searches"] == []
    assert out["ai_overview"] == {}
    assert out["knowledge_graph"] == {}


def test_http_error_returns_empty_not_raise(settings):
    out = _search({}, 500, settings=settings)
    assert out["error"] == "http_500"
    assert out["organic"] == []
    assert out["configured"] is True


def test_unconfigured_is_distinguishable_from_failed(settings):
    settings.SERPAPI_KEY = ""
    out = serp_google.search("q")
    # The UI greys out "not configured" and shows "failed" as an error, so
    # the two must not collapse into one state.
    assert out["configured"] is False
    assert out["error"] == "serpapi_not_configured"


def test_network_error_returns_empty_not_raise(settings):
    settings.SERPAPI_KEY = "serp-test"
    with patch("requests.get", side_effect=OSError("connection reset")):
        out = serp_google.search("q")
    assert out["error"] == "network"
    assert out["organic"] == []


def test_non_json_body_returns_empty(settings):
    settings.SERPAPI_KEY = "serp-test"
    resp = MagicMock()
    resp.status_code = 200
    resp.json = MagicMock(side_effect=ValueError("not json"))
    with patch("requests.get", return_value=resp):
        out = serp_google.search("q")
    assert out["error"] == "decode"


# -- discovery merge -----------------------------------------------------------


def test_merge_dedupes_and_credits_both_indexes():
    perplexity = [
        {"rank": 1, "url": "https://example.com/a", "domain": "example.com",
         "title": "A", "snippet": "from pplx"},
        {"rank": 2, "url": "https://only-pplx.com/b", "domain": "only-pplx.com",
         "title": "B", "snippet": ""},
    ]
    google = [
        # Same page: different rank, plus tracking params and a trailing slash.
        {"rank": 3, "url": "https://example.com/a/?utm_source=google", "domain": "example.com",
         "title": "A", "snippet": "", "date": "2025-01-01"},
        {"rank": 1, "url": "https://only-google.com/c", "domain": "only-google.com",
         "title": "C", "snippet": ""},
    ]

    merged = serp_google.merge_discovery(perplexity, google, limit=10)

    assert len(merged) == 3, "the same page from both indexes must collapse to one row"

    both = next(r for r in merged if "example.com" in r["url"])
    assert sorted(both["discovered_by"]) == ["google", "perplexity"]
    # Agreement between indexes outranks position: this row leads.
    assert merged[0] is both
    assert both["rank"] == 1
    # A field one index left empty is backfilled from the other.
    assert both["date"] == "2025-01-01"
    assert both["snippet"] == "from pplx"

    # Dense 1..N: (scan, rank) is unique_together and rank weighting
    # assumes no gaps.
    assert [r["rank"] for r in merged] == [1, 2, 3]

    solo = next(r for r in merged if "only-google" in r["url"])
    assert solo["discovered_by"] == ["google"]


def test_merge_respects_limit_and_survives_junk():
    rows = [{"rank": i, "url": f"https://example.com/{i}", "domain": "example.com"}
            for i in range(1, 21)]
    junk = [{"rank": 1, "url": "javascript:alert(1)"}, {"rank": 2, "url": ""}, {}]
    merged = serp_google.merge_discovery(rows, junk, limit=5)
    assert len(merged) == 5
    assert all(r["url"].startswith("https://") for r in merged)


# -- community lane ------------------------------------------------------------


REDDIT_POSTS = [
    {"title": "Quiet link post", "url": "https://www.reddit.com/r/Dallas/comments/q/quiet/",
     "subreddit": "Dallas", "score": 900, "num_comments": 1, "created_utc": 1.0},
    {"title": "Busy thread", "url": "https://www.reddit.com/r/Dallas/comments/b/busy/",
     "subreddit": "Dallas", "score": 40, "num_comments": 220, "created_utc": 2.0},
    {"title": "Offsite link", "url": "https://someblog.com/post",
     "subreddit": "Dallas", "score": 500, "num_comments": 90, "created_utc": 3.0},
]

SEARCH_MENTIONS = "apps.brand_vault.services.security.sources.reddit.search_mentions"


def test_community_ranks_by_discussion_not_upvotes(settings):
    settings.BRAND_RESEARCH_COMMUNITY_ENABLED = True
    with patch(SEARCH_MENTIONS, return_value=REDDIT_POSTS):
        rows = community.discover_reddit("best bagels in dallas")

    urls = [r["url"] for r in rows]
    # The 220-comment / 40-upvote thread beats the 900-upvote / 1-comment
    # one: only the talking produces brand mentions and sentiment.
    assert "busy" in urls[0]
    # Below MIN_COMMENTS; and off-site link posts belong to the web lane.
    assert not any("quiet" in u for u in urls)
    assert not any("someblog" in u for u in urls)
    assert rows[0]["platform_meta"]["num_comments"] == 220
    assert rows[0]["platform_meta"]["subreddit"] == "Dallas"
    assert rows[0]["rank"] == 1


def test_community_lane_disabled_is_a_no_op(settings):
    settings.BRAND_RESEARCH_COMMUNITY_ENABLED = False

    def _boom(*args, **kwargs):  # pragma: no cover - must never be reached
        raise AssertionError("the disabled lane must not call out")

    with patch(SEARCH_MENTIONS, side_effect=_boom):
        assert community.discover_reddit("q") == []


def test_community_survives_reddit_failure(settings):
    settings.BRAND_RESEARCH_COMMUNITY_ENABLED = True
    with patch(SEARCH_MENTIONS, side_effect=OSError("429 blocked")):
        assert community.discover_reddit("q") == []


def test_community_from_serp_discussions():
    rows = community.from_serp_discussions([
        {"url": "https://www.quora.com/x", "domain": "quora.com", "title": "T",
         "comment_count": 42, "source_label": "Quora"},
        {"url": "javascript:alert(1)", "title": "junk"},
    ])
    assert len(rows) == 1
    assert rows[0]["platform_meta"]["num_comments"] == 42
    assert rows[0]["platform_meta"]["source_label"] == "Quora"
