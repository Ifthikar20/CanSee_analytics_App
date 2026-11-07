"""
SSRF protection for the sources service.

Django-free copy of core/validators/url_safety.py: UnsafeURLError
subclasses Exception instead of django.core.exceptions.ValidationError,
and the display-URL and log-redaction helpers (unused here) are dropped.
The core module cannot move here because rag/onboarding/llm_ranking
depend on it and on the ValidationError hierarchy. Keep the two files in
sync when the guard logic changes;
apps/agent/tests/test_fix_be_platform_sources_ssrf.py runs the same
checks against both copies to catch drift.

Enforces:

    1. Scheme allowlist (http / https only).
    2. Hostname must resolve to a routable public address. Loopback,
       private RFC 1918, carrier-grade NAT (100.64.0.0/10), link-local
       (including the cloud metadata endpoint 169.254.169.254), reserved,
       multicast, broadcast, unspecified and every other non-global
       address are rejected.
    3. IPv6 equivalents of all the above.
    4. Host length and basic format checks.
    5. Optional port allowlist (``allowed_ports``).

DNS rebinding is not caught here: ``safe_http.safe_get`` checks the
connected peer of every socket it opens (``assert_peer_public``).
"""
from __future__ import annotations

import ipaddress
import re
import socket
from urllib.parse import urlparse

ALLOWED_SCHEMES = {"http", "https"}
MAX_HOSTNAME_LENGTH = 253


class UnsafeURLError(Exception):
    """Raised when a URL fails SSRF safety checks."""


def assert_url_safe(url: str, *, allowed_ports: tuple[int, ...] | None = None) -> str:
    """
    Raise ``UnsafeURLError`` if ``url`` would be unsafe to fetch.

    Returns the (possibly normalised) URL on success — callers should
    use the returned value rather than the input so a missing scheme
    becomes ``https://`` consistently.

    ``allowed_ports`` (optional) restricts the effective port; a URL
    without one counts as 80 (http) or 443 (https).
    """
    if not url or not isinstance(url, str):
        raise UnsafeURLError("URL is required.")
    url = url.strip()
    if not url:
        raise UnsafeURLError("URL is required.")
    if len(url) > 2000:
        raise UnsafeURLError("URL is too long.")

    if not re.match(r"^[A-Za-z][A-Za-z0-9+.\-]*://", url):
        # No "scheme://" authority prefix. A bare "host:port/..." is still
        # schemeless, but "ftp:...", "javascript:...", "mailto:..." carry a
        # real scheme — blindly prepending https:// to those would hide the
        # scheme from the allowlist check below ("https://ftp://..." parses
        # with hostname "ftp"). The (?!\d) lookahead keeps host:port intact.
        m = re.match(r"^([A-Za-z][A-Za-z0-9+.\-]*):(?!\d)", url)
        if m:
            raise UnsafeURLError(f"URL scheme '{m.group(1).lower()}' is not allowed.")
        url = f"https://{url}"

    try:
        parsed = urlparse(url)
    except Exception as exc:
        raise UnsafeURLError("Invalid URL format.") from exc

    if parsed.scheme not in ALLOWED_SCHEMES:
        raise UnsafeURLError(
            f"URL scheme '{parsed.scheme}' is not allowed.",
        )

    hostname = (parsed.hostname or "").lower()
    if not hostname:
        raise UnsafeURLError("URL must include a host.")
    if len(hostname) > MAX_HOSTNAME_LENGTH:
        raise UnsafeURLError("Hostname is too long.")

    if allowed_ports is not None:
        try:
            port = parsed.port
        except ValueError as exc:
            raise UnsafeURLError("Invalid port.") from exc
        if port is None:
            port = 443 if parsed.scheme == "https" else 80
        if port not in allowed_ports:
            raise UnsafeURLError(f"Port {port} is not allowed.")

    # Reject obvious local names without bothering DNS.
    if hostname in {"localhost", "ip6-localhost", "ip6-loopback"}:
        raise UnsafeURLError("Refusing to fetch a loopback address.")

    # If hostname is already an IP, validate directly. Otherwise resolve
    # and validate every address it points at — DNS may return multiple
    # records and we treat the URL as unsafe if ANY is private.
    addresses = _resolve_addresses(hostname)
    if not addresses:
        raise UnsafeURLError(
            f"We couldn't find a website at {hostname}. Check the address and try again."
        )
    for ip in addresses:
        if not _is_public_address(ip):
            raise UnsafeURLError(
                f"Refusing to fetch '{hostname}' — resolves to non-public address.",
            )

    return url


def is_url_safe(url: str) -> bool:
    """Boolean variant — returns False on any failure, never raises."""
    try:
        assert_url_safe(url)
    except UnsafeURLError:
        return False
    return True


def _resolve_addresses(hostname: str) -> list[ipaddress._BaseAddress]:
    """Return all IP addresses ``hostname`` resolves to, IPv4 + IPv6."""
    # Direct IP literal — short-circuit DNS.
    try:
        return [ipaddress.ip_address(hostname.strip("[]"))]
    except ValueError:
        pass

    try:
        records = socket.getaddrinfo(
            hostname, None,
            type=socket.SOCK_STREAM,
        )
    except socket.gaierror:
        return []

    out: list[ipaddress._BaseAddress] = []
    seen: set[str] = set()
    for family, _type, _proto, _canonname, sockaddr in records:
        if family == socket.AF_INET:
            ip_str = sockaddr[0]
        elif family == socket.AF_INET6:
            ip_str = sockaddr[0].split("%", 1)[0]  # strip zone id
        else:
            continue
        if ip_str in seen:
            continue
        seen.add(ip_str)
        try:
            out.append(ipaddress.ip_address(ip_str))
        except ValueError:
            continue
    return out


def _is_public_address(ip: ipaddress._BaseAddress) -> bool:
    """
    Return True only if ``ip`` is safe to fetch (a public unicast address).

    Excludes loopback, private (RFC 1918), link-local (incl. cloud
    metadata 169.254.169.254 / fe80::/10), reserved, multicast,
    unspecified (0.0.0.0), broadcast, anything else that is not globally
    reachable (carrier-grade NAT 100.64.0.0/10, benchmarking, documentation
    and other special-purpose ranges), and the IPv6 mapped-IPv4 forms of
    each of those.
    """
    # Normalise IPv4-mapped IPv6 (::ffff:1.2.3.4) and 6to4 prefixes back
    # to their underlying IPv4 so the public-address check applies.
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        elif ip.sixtofour is not None:
            ip = ip.sixtofour

    if ip.is_loopback:
        return False
    if ip.is_private:
        return False
    if ip.is_link_local:
        return False
    if ip.is_reserved:
        return False
    if ip.is_multicast:
        return False
    if ip.is_unspecified:
        return False
    if isinstance(ip, ipaddress.IPv4Address) and str(ip) == "255.255.255.255":
        return False
    if not ip.is_global:
        return False
    return True


def assert_peer_public(sock) -> None:
    """Raise ``UnsafeURLError`` unless ``sock`` is connected to a public address.

    Runs on the socket itself, after connect and before any byte is sent,
    so it holds whatever DNS answered at connect time (rebinding included)
    and on every redirect hop.
    """
    try:
        host = sock.getpeername()[0]
        ip = ipaddress.ip_address(str(host).split("%", 1)[0])
    except (OSError, ValueError, IndexError, TypeError) as exc:
        raise UnsafeURLError("Could not verify the connected address.") from exc
    if not _is_public_address(ip):
        raise UnsafeURLError("Refusing to talk to a non-public address.")
