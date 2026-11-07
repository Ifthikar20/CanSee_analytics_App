"""safe_get(truncate=True): oversize pages are cut, not rejected.

Regression: onboarding showed a user the raw internal string
"response body exceeds 800000 bytes" for sundaygolf.com, because the
body cap aborted the fetch. Pages larger than the cap are ordinary; the
scanner only needs the head of the document.
"""
from unittest.mock import patch

import pytest

from core.validators.safe_http import DEFAULT_MAX_BYTES, FetchError, safe_get


class _FakeResp:
    """Minimal stand-in for a streamed requests.Response."""

    def __init__(self, body: bytes, status=200, ctype="text/html"):
        self._body = body
        self.status_code = status
        self.headers = {"Content-Type": ctype}
        self._content = b""
        self.closed = False

    def iter_content(self, chunk_size=8192):
        for i in range(0, len(self._body), chunk_size):
            yield self._body[i:i + chunk_size]

    @property
    def text(self):
        return self._content.decode("utf-8", "replace")

    def close(self):
        self.closed = True


def _big_page(size: int) -> bytes:
    head = b"<html><head><title>Sunday Golf</title></head><body>"
    return head + b"x" * size


@pytest.fixture(autouse=True)
def _allow_url():
    with patch("core.validators.safe_http.assert_url_safe", side_effect=lambda u: u):
        yield


class TestTruncate:
    def test_oversize_body_raises_by_default(self):
        resp = _FakeResp(_big_page(200_000))
        with patch("requests.get", return_value=resp):
            with pytest.raises(FetchError, match="exceeds"):
                safe_get("https://example.com", max_bytes=50_000)

    def test_truncate_returns_prefix_instead_of_raising(self):
        resp = _FakeResp(_big_page(200_000))
        with patch("requests.get", return_value=resp):
            out = safe_get("https://example.com", max_bytes=50_000, truncate=True)
        assert out.status_code == 200
        # Capped, and the head (what the scanner actually reads) survives.
        assert len(out.text) <= 50_000
        assert "<title>Sunday Golf</title>" in out.text
        assert resp.closed is True

    def test_truncate_leaves_small_pages_whole(self):
        body = b"<html><head><title>Tiny</title></head><body>ok</body></html>"
        with patch("requests.get", return_value=_FakeResp(body)):
            out = safe_get("https://example.com", max_bytes=DEFAULT_MAX_BYTES,
                           truncate=True)
        assert out.text == body.decode()


class TestFriendlyErrors:
    """Onboarding must never surface an internal reason string."""

    def test_maps_internal_details_to_actionable_copy(self):
        from apps.llm_ranking.services.domain_scanner import _friendly_fetch_error

        cases = {
            "response body exceeds 800000 bytes": "too large",
            "unsafe URL: private address": "public website",
            "connection timed out": "too long",
            "disallowed content-type: application/pdf": "web page",
            "Failed to resolve host": "couldn't find that domain",
            "something unexpected": "couldn't read that site",
        }
        for detail, expected in cases.items():
            msg = _friendly_fetch_error(detail)
            assert expected.lower() in msg.lower(), (detail, msg)
            # No internals leak through.
            assert "bytes" not in msg.lower()
            assert "content-type" not in msg.lower()
