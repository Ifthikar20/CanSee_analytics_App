"""
Tests for ``safe_get`` — redirect re-validation, body cap,
content-type allowlist, and the SSRF guard at the entry point.

The transport (``requests.get``) is mocked at the module boundary so
no network is touched. DNS is mocked at ``socket.getaddrinfo`` so
the URL safety check resolves to deterministic addresses.
"""
from unittest.mock import MagicMock, patch

import pytest

from core.validators import safe_http
from core.validators.safe_http import FetchError, safe_get


def _fake_resolver(*addresses: str):
    import socket as _s

    def fake(host, *args, **kwargs):
        records = []
        for ip in addresses:
            family = _s.AF_INET6 if ":" in ip else _s.AF_INET
            sockaddr = (ip, 0, 0, 0) if family == _s.AF_INET6 else (ip, 0)
            records.append((family, _s.SOCK_STREAM, 0, "", sockaddr))
        return records
    return patch("core.validators.url_safety.socket.getaddrinfo", new=fake)


def _mock_response(*, status=200, body=b"<html>ok</html>",
                   content_type="text/html", location=None):
    resp = MagicMock()
    resp.status_code = status
    resp.headers = {"Content-Type": content_type}
    if location is not None:
        resp.headers["Location"] = location
    resp.iter_content = lambda chunk_size=8192: [body]
    resp._content = body
    resp.text = body.decode("utf-8", errors="replace")
    resp.close = MagicMock()
    return resp


class TestSSRFAtEntry:
    def test_rejects_private_ip(self):
        with _fake_resolver("10.0.0.1"), pytest.raises(FetchError, match="unsafe"):
            safe_get("https://internal.example.com")

    def test_rejects_loopback_hostname(self):
        with pytest.raises(FetchError, match="unsafe"):
            safe_get("http://localhost/admin")

    def test_rejects_non_http_scheme(self):
        with pytest.raises(FetchError, match="unsafe"):
            safe_get("file:///etc/passwd")


class TestRedirectRevalidation:
    def test_follows_safe_redirect(self):
        first = _mock_response(status=302, location="https://target.example.com/")
        second = _mock_response(status=200, body=b"final")
        with _fake_resolver("93.184.216.34"), \
             patch.object(safe_http.requests, "get", side_effect=[first, second]):
            resp = safe_get("https://example.com")
        assert resp.status_code == 200
        assert resp.text == "final"
        assert resp.final_url.startswith("https://target.example.com")

    def test_refuses_redirect_to_private_address(self):
        # Redirect target resolves to a private IP — must refuse.
        first = _mock_response(status=302, location="https://internal.example.com/")
        with _fake_resolver("93.184.216.34", "10.0.0.1"), \
             patch.object(safe_http.requests, "get", side_effect=[first]):
            with pytest.raises(FetchError, match="redirect"):
                safe_get("https://example.com")

    def test_refuses_redirect_to_metadata_endpoint(self):
        first = _mock_response(status=302, location="http://169.254.169.254/latest/meta-data/")

        # Need different DNS for the initial host vs the redirect target.
        # Both URL-safe checks share the same patched resolver — but the
        # redirect target is an IP literal, so DNS isn't queried for it.
        with _fake_resolver("93.184.216.34"), \
             patch.object(safe_http.requests, "get", side_effect=[first]):
            with pytest.raises(FetchError, match="redirect"):
                safe_get("https://example.com")

    def test_refuses_too_many_redirects(self):
        loop = _mock_response(status=302, location="https://example.com/loop")
        with _fake_resolver("93.184.216.34"), \
             patch.object(safe_http.requests, "get", return_value=loop):
            with pytest.raises(FetchError, match="too many redirects"):
                safe_get("https://example.com", max_redirects=2)

    def test_refuses_redirect_with_no_location(self):
        bad = _mock_response(status=302, location=None)
        if "Location" in bad.headers:
            del bad.headers["Location"]
        with _fake_resolver("93.184.216.34"), \
             patch.object(safe_http.requests, "get", return_value=bad):
            with pytest.raises(FetchError, match="Location"):
                safe_get("https://example.com")


class TestBodyCap:
    def test_aborts_when_body_exceeds_max_bytes(self):
        big_resp = MagicMock()
        big_resp.status_code = 200
        big_resp.headers = {"Content-Type": "text/html"}
        # Stream returns an oversize chunk.
        big_resp.iter_content = lambda chunk_size=8192: [b"x" * 100_000]
        big_resp.close = MagicMock()
        with _fake_resolver("93.184.216.34"), \
             patch.object(safe_http.requests, "get", return_value=big_resp):
            with pytest.raises(FetchError, match="body exceeds"):
                safe_get("https://example.com", max_bytes=1024)


class TestContentTypeAllowlist:
    def test_rejects_binary_content(self):
        bad = _mock_response(content_type="application/octet-stream")
        with _fake_resolver("93.184.216.34"), \
             patch.object(safe_http.requests, "get", return_value=bad):
            with pytest.raises(FetchError, match="content-type"):
                safe_get("https://example.com")

    def test_rejects_image(self):
        bad = _mock_response(content_type="image/jpeg")
        with _fake_resolver("93.184.216.34"), \
             patch.object(safe_http.requests, "get", return_value=bad):
            with pytest.raises(FetchError, match="content-type"):
                safe_get("https://example.com")

    def test_accepts_text_html(self):
        ok = _mock_response(content_type="text/html; charset=utf-8")
        with _fake_resolver("93.184.216.34"), \
             patch.object(safe_http.requests, "get", return_value=ok):
            resp = safe_get("https://example.com")
        # The split-and-strip should normalise the content type.
        assert resp.content_type == "text/html"

    def test_caller_can_override_allowlist(self):
        xml = _mock_response(content_type="application/xml")
        with _fake_resolver("93.184.216.34"), \
             patch.object(safe_http.requests, "get", return_value=xml):
            # default allowlist accepts XML
            resp = safe_get("https://example.com")
            assert resp.status_code == 200

        # narrower allowlist rejects it
        with _fake_resolver("93.184.216.34"), \
             patch.object(safe_http.requests, "get", return_value=xml):
            with pytest.raises(FetchError, match="content-type"):
                safe_get("https://example.com",
                         allowed_content_types=("text/html",))


class TestNetworkErrorWrapping:
    def test_request_exception_becomes_fetch_error(self):
        with _fake_resolver("93.184.216.34"), \
             patch.object(safe_http.requests, "get",
                          side_effect=safe_http.requests.exceptions.Timeout("timed out")):
            with pytest.raises(FetchError, match="timed out"):
                safe_get("https://example.com")
