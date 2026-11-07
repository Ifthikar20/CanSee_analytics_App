"""Unit tests for the Google Custom Search JSON API client."""

from unittest.mock import MagicMock, patch

import pytest

from apps.llm_ranking.services import google_search


@pytest.fixture(autouse=True)
def _clear_cache():
    from django.core.cache import cache
    cache.clear()
    yield
    cache.clear()


def _stub_response(items):
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json = MagicMock(return_value={"items": items})
    return resp


@pytest.mark.django_db
def test_search_returns_empty_when_not_configured(settings):
    settings.GOOGLE_API_KEY = ""
    settings.GOOGLE_CSE_ID = ""
    assert google_search.search("anything") == []


@pytest.mark.django_db
def test_search_parses_publisher_url_and_domain(settings):
    settings.GOOGLE_API_KEY = "k"
    settings.GOOGLE_CSE_ID = "cx"

    items = [
        {
            "link":    "https://www.nytimes.com/2026/01/01/article.html",
            "title":   "Some headline",
            "snippet": "lede here",
        }
    ]
    with patch.object(google_search.requests, "get", return_value=_stub_response(items)) as mock_get:
        out = google_search.search("nyt thing", num=3)

    assert len(out) == 1
    assert out[0]["url"].startswith("https://www.nytimes.com/")
    assert out[0]["domain"] == "nytimes.com"
    assert out[0]["title"] == "Some headline"
    assert out[0]["snippet"] == "lede here"
    # Hit the API with the right CSE params.
    _, kwargs = mock_get.call_args
    params = kwargs["params"]
    assert params["q"] == "nyt thing"
    assert params["num"] == 3
    assert params["key"] == "k"
    assert params["cx"] == "cx"


@pytest.mark.django_db
def test_search_caches_repeat_calls(settings):
    settings.GOOGLE_API_KEY = "k"
    settings.GOOGLE_CSE_ID = "cx"

    with patch.object(
        google_search.requests, "get",
        return_value=_stub_response([{"link": "https://a.com", "title": "A"}]),
    ) as mock_get:
        google_search.search("q1")
        google_search.search("q1")

    assert mock_get.call_count == 1


@pytest.mark.django_db
def test_search_swallows_network_errors(settings):
    settings.GOOGLE_API_KEY = "k"
    settings.GOOGLE_CSE_ID = "cx"
    err = google_search.requests.RequestException("boom")
    with patch.object(google_search.requests, "get", side_effect=err):
        assert google_search.search("q") == []


@pytest.mark.django_db
def test_search_many_dedupes_and_credits_queries(settings):
    settings.GOOGLE_API_KEY = "k"
    settings.GOOGLE_CSE_ID = "cx"

    def fake_get(url, params=None, timeout=None):
        q = params["q"]
        if q == "q1":
            return _stub_response([
                {"link": "https://a.com", "title": "A"},
                {"link": "https://b.com", "title": "B"},
            ])
        return _stub_response([
            {"link": "https://b.com", "title": "B"},
            {"link": "https://c.com", "title": "C"},
        ])

    with patch.object(google_search.requests, "get", side_effect=fake_get):
        envelope = google_search.search_many(["q1", "q2"], num_per_query=2, max_total=10)

    cites = envelope["citations"]
    urls = {r["url"] for r in cites}
    assert urls == {"https://a.com", "https://b.com", "https://c.com"}
    by_url = {r["url"]: r for r in cites}
    assert by_url["https://b.com"]["queries"] == ["q1", "q2"]
    assert by_url["https://a.com"]["queries"] == ["q1"]
    assert by_url["https://c.com"]["queries"] == ["q2"]
    assert envelope["api_calls"] == 2
    assert envelope["queries_made"] == 2


@pytest.mark.django_db
def test_search_many_respects_max_total(settings):
    settings.GOOGLE_API_KEY = "k"
    settings.GOOGLE_CSE_ID = "cx"
    items = [{"link": f"https://x{i}.com", "title": f"X{i}"} for i in range(10)]
    with patch.object(google_search.requests, "get", return_value=_stub_response(items)):
        envelope = google_search.search_many(["q"], num_per_query=10, max_total=3)
    assert len(envelope["citations"]) == 3
    assert envelope["capped"] is True


@pytest.mark.django_db
def test_search_many_respects_max_queries(settings):
    """Hard cap on CSE calls regardless of how many prompts came in."""
    settings.GOOGLE_API_KEY = "k"
    settings.GOOGLE_CSE_ID = "cx"
    # Each query returns one unique URL so we never hit max_total.
    def fake_get(url, params=None, timeout=None):
        q = params["q"]
        return _stub_response([{"link": f"https://{q}.com", "title": q}])

    with patch.object(google_search.requests, "get", side_effect=fake_get) as mock_get:
        envelope = google_search.search_many(
            [f"q{i}" for i in range(150)],
            num_per_query=5,
            max_total=200,
            max_queries=15,
        )

    assert mock_get.call_count == 15
    assert envelope["queries_made"] == 15
    assert envelope["capped"] is True


@pytest.mark.django_db
def test_per_user_daily_quota_blocks_excess(settings):
    settings.GOOGLE_API_KEY = "k"
    settings.GOOGLE_CSE_ID = "cx"
    settings.GOOGLE_CSE_DAILY_LIMIT_PER_USER = 3
    with patch.object(google_search.requests, "get") as mock_get:
        mock_get.return_value = _stub_response([{"link": "https://x.com", "title": "X"}])
        envelope = google_search.search_many(
            [f"q{i}" for i in range(10)],
            num_per_query=2,
            max_total=50,
            max_queries=50,
            user_id=42,
        )

    # After 3 successful calls the user runs out; we then break.
    assert mock_get.call_count == 3
    assert envelope["api_calls"] == 3
    assert envelope["quota_blocks"] == 1  # the 4th attempt was refused
    assert envelope["quota_exceeded"] is True
    assert envelope["quota_remaining"] == 0
    assert envelope["daily_limit"] == 3


@pytest.mark.django_db
def test_quota_remaining_counts_down(settings):
    settings.GOOGLE_API_KEY = "k"
    settings.GOOGLE_CSE_ID = "cx"
    settings.GOOGLE_CSE_DAILY_LIMIT_PER_USER = 10
    with patch.object(google_search.requests, "get",
                      return_value=_stub_response([{"link": "https://x.com", "title": "X"}])):
        google_search.search("q1", user_id=7)
        google_search.search("q2", user_id=7)
    assert google_search.quota_remaining(7) == 8


@pytest.mark.django_db
def test_daily_limit_default_is_100_when_unset(settings):
    if hasattr(settings, "GOOGLE_CSE_DAILY_LIMIT_PER_USER"):
        del settings.GOOGLE_CSE_DAILY_LIMIT_PER_USER
    assert google_search.quota_for_user(123) == google_search.DEFAULT_DAILY_LIMIT_PER_USER


@pytest.mark.django_db
def test_search_attaches_one_indexed_serp_rank(settings):
    settings.GOOGLE_API_KEY = "k"
    settings.GOOGLE_CSE_ID = "cx"
    items = [
        {"link": "https://first.com",  "title": "F"},
        {"link": "https://second.com", "title": "S"},
        {"link": "https://third.com",  "title": "T"},
    ]
    with patch.object(google_search.requests, "get", return_value=_stub_response(items)):
        out = google_search.search("q")
    assert [r["serp_rank"] for r in out] == [1, 2, 3]


@pytest.mark.django_db
def test_search_many_keeps_best_serp_rank_across_queries(settings):
    """A URL seen at rank 4 for q1 and rank 1 for q2 must keep rank 1."""
    settings.GOOGLE_API_KEY = "k"
    settings.GOOGLE_CSE_ID = "cx"

    def fake_get(url, params=None, timeout=None):
        q = params["q"]
        if q == "q1":
            return _stub_response([
                {"link": "https://other.com",  "title": "O"},
                {"link": "https://other2.com", "title": "O2"},
                {"link": "https://other3.com", "title": "O3"},
                {"link": "https://target.com", "title": "T"},  # rank 4
            ])
        return _stub_response([
            {"link": "https://target.com", "title": "T"},      # rank 1
            {"link": "https://other4.com", "title": "O4"},
        ])

    with patch.object(google_search.requests, "get", side_effect=fake_get):
        envelope = google_search.search_many(
            ["q1", "q2"], num_per_query=10, max_total=20,
        )

    by_url = {r["url"]: r for r in envelope["citations"]}
    assert by_url["https://target.com"]["best_serp_rank"] == 1
    assert by_url["https://other.com"]["best_serp_rank"] == 1
