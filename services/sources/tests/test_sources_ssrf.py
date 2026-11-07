"""SSRF guard of the sources service's local url_safety/safe_http copies.

Mirrors the core/validators test coverage so drift between the copies
and the originals surfaces here.
"""

from unittest.mock import MagicMock, patch

import pytest

from services.sources import safe_http, url_safety


@pytest.mark.parametrize("url", [
    "http://169.254.169.254/latest/meta-data/",
    "http://localhost/",
    "http://127.0.0.1:8000/",
    "http://10.0.0.5/",
    "http://192.168.1.1/",
    "http://[::1]/",
])
def test_private_and_loopback_refused(url):
    with pytest.raises(url_safety.UnsafeURLError):
        url_safety.assert_url_safe(url)


@pytest.mark.parametrize("url", [
    "file:///etc/passwd",
    "ftp://example.com/x",
    "gopher://example.com/",
])
def test_non_http_schemes_refused(url):
    with pytest.raises(url_safety.UnsafeURLError):
        url_safety.assert_url_safe(url)


def test_error_is_plain_exception_not_django():
    # The service copy must not depend on Django's ValidationError.
    assert issubclass(url_safety.UnsafeURLError, Exception)
    assert "django" not in url_safety.UnsafeURLError.__mro__[1].__module__


def test_redirect_to_private_refused():
    redirect = MagicMock()
    redirect.status_code = 302
    redirect.headers = {"Location": "http://169.254.169.254/"}
    with patch.object(safe_http, "assert_url_safe",
                      side_effect=["https://ok.example.com/", url_safety.UnsafeURLError("private")]), \
         patch.object(safe_http, "_do_request", return_value=redirect):
        with pytest.raises(safe_http.FetchError, match="redirect refused"):
            safe_http.safe_get("https://ok.example.com/")


def test_read_page_refuses_metadata_endpoint():
    from services.sources import logic
    out = logic.read_page("http://169.254.169.254/latest/meta-data/")
    assert out["status"] == "error"
    assert "unsafe URL" in out["detail"]


@pytest.mark.parametrize("url,expected", [
    ("https://www.reddit.com/r/x/comments/abc/title/", True),
    ("https://reddit.com/r/x/comments/abc/", True),
    ("https://old.reddit.com/r/x/comments/abc/", True),
    # The bug: endswith("reddit.com") matched attacker-registered hosts,
    # letting a SERP-supplied URL route this fetch anywhere.
    ("https://evilreddit.com/r/x/comments/abc/", False),
    ("https://reddit.com.attacker.example/r/x/comments/abc/", False),
    ("https://notreddit.com/comments/abc/", False),
])
def test_is_reddit_thread_host_is_dot_anchored(url, expected):
    from services.sources import logic
    assert logic.is_reddit_thread(url) is expected


@pytest.mark.parametrize("url,expected", [
    ("https://www.yelp.com/biz/x", True),
    ("https://yelp.com/biz/x", True),
    ("https://evilyelp.com/biz/x", False),
    ("https://yelp.com.attacker.example/biz/x", False),
])
def test_is_yelp_host_is_dot_anchored(url, expected):
    from services.sources import logic
    assert logic.is_yelp(url) is expected


def test_read_reddit_thread_goes_through_safe_get(monkeypatch):
    # Regression: read_reddit_thread used a raw requests.get. It must now go
    # through safe_get, so a guard refusal surfaces as a non-ok result rather
    # than an unguarded fetch.
    from services.sources import logic

    def refuse(*_a, **_kw):
        raise safe_http.FetchError("unsafe URL: private address")

    monkeypatch.setattr(logic, "safe_get", refuse)
    out = logic.read_reddit_thread("https://www.reddit.com/r/x/comments/abc/")
    assert out["status"] != "ok"
    assert "unsafe URL" in out["detail"]
