"""
Tests for the SSRF guard.

DNS resolution is patched at the ``socket.getaddrinfo`` boundary so the
suite stays hermetic. Each test sets up a fake resolver that returns
the addresses we want to test against.
"""
from unittest.mock import patch

import pytest

from core.validators.url_safety import (
    UnsafeURLError,
    _redact_log_message,
    assert_url_safe,
    is_url_safe,
    safe_display_url,
)


def _fake_resolver(*addresses: str):
    """Patch ``socket.getaddrinfo`` to return given IPs (string form)."""
    import socket as _s

    def fake(host, *args, **kwargs):
        records = []
        for ip in addresses:
            family = _s.AF_INET6 if ":" in ip else _s.AF_INET
            sockaddr = (ip, 0, 0, 0) if family == _s.AF_INET6 else (ip, 0)
            records.append((family, _s.SOCK_STREAM, 0, "", sockaddr))
        return records
    return patch("core.validators.url_safety.socket.getaddrinfo", new=fake)


class TestSchemeAllowlist:
    def test_http_allowed(self):
        with _fake_resolver("93.184.216.34"):
            assert_url_safe("http://example.com")

    def test_https_allowed(self):
        with _fake_resolver("93.184.216.34"):
            assert_url_safe("https://example.com")

    def test_missing_scheme_normalised_to_https(self):
        with _fake_resolver("93.184.216.34"):
            url = assert_url_safe("example.com")
            assert url.startswith("https://")

    @pytest.mark.parametrize("scheme", [
        "ftp", "file", "gopher", "ldap", "dict", "jar", "ws", "wss",
    ])
    def test_other_schemes_rejected(self, scheme):
        with pytest.raises(UnsafeURLError, match="scheme"):
            assert_url_safe(f"{scheme}://example.com/")


class TestPrivateAddressBlocked:
    @pytest.mark.parametrize("ip", [
        "127.0.0.1",        # loopback
        "127.250.1.1",      # any 127.x
        "10.0.0.5",         # RFC 1918
        "172.16.5.5",       # RFC 1918
        "172.31.255.255",   # RFC 1918
        "192.168.1.1",      # RFC 1918
        "169.254.169.254",  # AWS / GCP / Azure metadata
        "169.254.0.1",      # link-local
        "0.0.0.0",          # unspecified
        "224.0.0.1",        # multicast
        "255.255.255.255",  # broadcast
    ])
    def test_blocks_ipv4_private(self, ip):
        with _fake_resolver(ip), pytest.raises(UnsafeURLError):
            assert_url_safe("https://example.com")

    @pytest.mark.parametrize("ip", [
        "::1",                              # loopback
        "fe80::1",                          # link-local
        "fc00::1",                          # unique local
        "fd00:1234::5",                     # unique local
        "::ffff:127.0.0.1",                 # IPv4-mapped loopback
        "::ffff:10.0.0.1",                  # IPv4-mapped private
        "2002:7f00:0001::",                 # 6to4-encoded loopback (127.0.0.1)
        "ff00::1",                          # multicast
        "::",                               # unspecified
    ])
    def test_blocks_ipv6_private(self, ip):
        with _fake_resolver(ip), pytest.raises(UnsafeURLError):
            assert_url_safe("https://example.com")

    def test_localhost_hostname_blocked_without_dns(self):
        # We refuse "localhost" by name even before DNS resolution.
        with pytest.raises(UnsafeURLError, match="loopback"):
            assert_url_safe("http://localhost/")

    def test_blocks_when_any_returned_ip_is_private(self):
        # DNS returns one public + one private IP — must reject (defends
        # against rebinding-style attacks where one record is a trap).
        with _fake_resolver("93.184.216.34", "10.0.0.1"), \
             pytest.raises(UnsafeURLError):
            assert_url_safe("https://multihomed.example.com")


class TestPublicAddressAllowed:
    def test_public_ipv4_passes(self):
        with _fake_resolver("93.184.216.34"):
            assert_url_safe("https://example.com")

    def test_public_ipv6_passes(self):
        with _fake_resolver("2606:2800:220:1:248:1893:25c8:1946"):
            assert_url_safe("https://example.com")

    def test_direct_public_ip_literal_passes(self):
        # No DNS lookup happens for an IP literal.
        assert_url_safe("https://93.184.216.34")


class TestEdgeCases:
    def test_empty_url_rejected(self):
        with pytest.raises(UnsafeURLError):
            assert_url_safe("")
        with pytest.raises(UnsafeURLError):
            assert_url_safe("   ")
        with pytest.raises(UnsafeURLError):
            assert_url_safe(None)

    def test_overlong_url_rejected(self):
        with pytest.raises(UnsafeURLError, match="too long"):
            assert_url_safe("https://example.com/" + "a" * 3000)

    def test_unresolvable_host_rejected(self):
        # No A or AAAA records — must reject (can't validate destination).
        with _fake_resolver(), pytest.raises(UnsafeURLError):
            assert_url_safe("https://does-not-exist.invalid")

    def test_basic_auth_in_url_does_not_bypass_check(self):
        with _fake_resolver("127.0.0.1"), pytest.raises(UnsafeURLError):
            assert_url_safe("http://user:pass@example.com")


class TestIsUrlSafe:
    def test_returns_true_on_pass(self):
        with _fake_resolver("93.184.216.34"):
            assert is_url_safe("https://example.com") is True

    def test_returns_false_instead_of_raising(self):
        assert is_url_safe("file:///etc/passwd") is False
        with _fake_resolver("127.0.0.1"):
            assert is_url_safe("https://example.com") is False


class TestRedactLogMessage:
    def test_redacts_bearer_token(self):
        out = _redact_log_message("Authorization: Bearer abc123def456ghi789")
        assert "Bearer abc123def456ghi789" not in out
        assert "[REDACTED]" in out

    def test_redacts_aws_key(self):
        out = _redact_log_message("aws_access_key_id=AKIAIOSFODNN7EXAMPLE")
        assert "AKIAIOSFODNN7EXAMPLE" not in out
        assert "[REDACTED_AWS_KEY]" in out

    def test_redacts_openai_style_key(self):
        out = _redact_log_message("api_key=sk-proj-abc123def456ghijklmnop")
        assert "sk-proj-abc123" not in out
        assert "[REDACTED_API_KEY]" in out

    def test_redacts_basic_auth_in_url(self):
        out = _redact_log_message("Failed to fetch https://user:hunter2@example.com/path")
        assert "user:hunter2@" not in out
        assert "[REDACTED]@example.com" in out

    def test_redacts_jwt(self):
        token = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIx.signaturepart"
        out = _redact_log_message(f"jwt={token}")
        assert token not in out
        assert "[REDACTED_JWT]" in out

    def test_caps_message_length(self):
        out = _redact_log_message("a" * 5000)
        assert len(out) == 1000

    def test_pass_through_when_clean(self):
        clean = "Audit completed — score 73/100"
        assert _redact_log_message(clean) == clean

    def test_handles_empty(self):
        assert _redact_log_message("") == ""
        assert _redact_log_message(None) == ""


class TestSafeDisplayURL:
    """safe_display_url is the href output-allowlist (no DNS)."""

    @pytest.mark.parametrize("url", [
        "https://example.com/page",
        "http://example.com",
        "  https://example.com/x  ",  # trimmed
        "HTTPS://EXAMPLE.COM",
    ])
    def test_allows_http_and_https(self, url):
        assert safe_display_url(url) == url.strip()

    @pytest.mark.parametrize("url", [
        "javascript:alert(1)",
        "JavaScript:alert(1)",
        "java\tscript:alert(1)",       # control-char obfuscation
        "  javascript:alert(1)",
        "data:text/html,<script>alert(1)</script>",
        "vbscript:msgbox(1)",
        "//evil.example.com",          # scheme-relative
        "example.com/no-scheme",       # schemeless
        "",
        None,
    ])
    def test_rejects_dangerous_and_ambiguous(self, url):
        assert safe_display_url(url) is None
