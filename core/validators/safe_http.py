"""
Safe HTTP fetch helpers.

A single ``safe_get`` wrapper around ``requests`` that every code
path which fetches a user-supplied URL must use. Hardening covered:

    1. SSRF guard via ``url_safety.assert_url_safe`` (scheme allowlist,
       private/loopback/metadata/non-global blocking, IPv4 + IPv6,
       multi-record DNS resolution, optional port allowlist).
    2. Connected-peer check: every socket the fetch opens is checked
       with ``url_safety.assert_peer_public`` right after connect and
       before a byte is sent. A name that resolved to a public address
       at check time but to a private one at connect time (DNS
       rebinding) is refused, on every hop.
    3. Redirect re-validation: ``allow_redirects=False`` + manual hop
       walking. Every ``Location`` is fed back through the SSRF guard
       before we follow it. Defeats the "scrape an attacker-controlled
       domain that 302s to 169.254.169.254" trick.
    4. Content-Type allowlist, checked on the response headers before
       the body is read, so a tenant cannot make us download large
       binaries to fill disk.
    5. Body size cap: stream and abort once we exceed ``max_bytes``.
    6. Header sanitisation: environment proxies, ``.netrc`` credentials
       and cookies are never used, and once a redirect leaves the first
       URL's origin the caller's Authorization / Cookie headers are
       dropped for the rest of the chain.
    7. Optional total wall-clock ``deadline``. ``requests`` timeouts apply
       per socket operation, so a server that drips its response one byte
       at a time could otherwise hold a fetch open indefinitely.

What this does not defeat: hostile content. Callers treat what comes
back as untrusted data.
"""
from __future__ import annotations

import logging
import socket
import threading
import time
from urllib.parse import urljoin, urlsplit

import requests
from requests.adapters import HTTPAdapter
from urllib3.connection import HTTPConnection, HTTPSConnection
from urllib3.connectionpool import HTTPConnectionPool, HTTPSConnectionPool

from core.validators.url_safety import (
    UnsafeURLError,
    assert_peer_public,
    assert_url_safe,
)

logger = logging.getLogger("apps")

DEFAULT_MAX_BYTES = 1_000_000
DEFAULT_MAX_REDIRECTS = 4
DEFAULT_TIMEOUT = 10
ALLOWED_CONTENT_TYPES = (
    "text/html", "application/xhtml+xml", "text/plain",
    "application/xml", "text/xml", "application/json",
    "application/rss+xml", "application/atom+xml",
)
_REDIRECT_STATUSES = (301, 302, 303, 307, 308)
# Never sent once a redirect chain has left the first URL's origin.
_CREDENTIAL_HEADERS = frozenset({"authorization", "proxy-authorization", "cookie"})
_DEADLINE_MESSAGE = "timed out (deadline reached)"

# The deadline watchdog of the fetch running on this thread. Connections
# register their sockets with it as they open (see _PublicPeerMixin).
_local = threading.local()


class FetchError(Exception):
    """Wraps any fetch-time failure (network, body cap, content-type, SSRF).

    ``code`` classifies it: ``blocked_url``, ``timeout``, ``too_large``,
    ``content_type`` or ``fetch_failed``.
    """

    def __init__(self, message: str = "", *, code: str = "fetch_failed"):
        super().__init__(message)
        self.code = code


class SafeResponse:
    __slots__ = (
        "status_code", "final_url", "content_type", "headers", "content",
        "_response", "_text",
    )

    def __init__(self, *, status_code, final_url, content_type, headers, content, response):
        self.status_code = status_code
        self.final_url = final_url
        self.content_type = content_type
        self.headers = headers
        # The body as read (decompressed), at most max_bytes.
        self.content = content
        self._response = response
        self._text = None

    @property
    def text(self) -> str:
        """The body decoded the way ``requests`` decodes it."""
        if self._text is None:
            self._text = self._response.text
        return self._text


def safe_get(
    url: str,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    max_bytes: int = DEFAULT_MAX_BYTES,
    max_redirects: int = DEFAULT_MAX_REDIRECTS,
    headers: dict | None = None,
    allowed_content_types: tuple[str, ...] = ALLOWED_CONTENT_TYPES,
    truncate: bool = False,
    allowed_ports: tuple[int, ...] | None = None,
    deadline: float | None = None,
) -> SafeResponse:
    """
    Hardened replacement for ``requests.get``.

    Returns a ``SafeResponse``. Raises ``FetchError`` for any failure
    (network, SSRF rejection, body too large, disallowed content type).
    Treat any exception as "do not use this URL".

    ``truncate=True`` returns the first ``max_bytes`` instead of raising
    when a page is larger than the cap. Memory is bounded identically —
    reading still stops at the cap — so this is no less safe; it is the
    right mode for callers that only need the head of a document (meta
    tags, hero copy) and would otherwise reject perfectly ordinary pages.
    Modern JS-heavy sites routinely ship >1MB of inline HTML.

    ``allowed_ports`` restricts every hop's port. ``deadline`` (seconds)
    bounds the whole fetch, redirects and body included; ``timeout``
    still bounds each connect and read.
    """
    url = _checked(url, allowed_ports, prefix="unsafe URL")
    watchdog = _Watchdog(deadline) if deadline else None
    session = _new_session()
    _local.watchdog = watchdog
    try:
        return _fetch(
            session, url, timeout=timeout, max_bytes=max_bytes,
            max_redirects=max_redirects, headers=headers,
            allowed_content_types=allowed_content_types, truncate=truncate,
            allowed_ports=allowed_ports, watchdog=watchdog,
        )
    finally:
        _local.watchdog = None
        if watchdog is not None:
            watchdog.cancel()
        session.close()


# ── Internal helpers ──────────────────────────────────────────────────────


def _fetch(
    session, url, *, timeout, max_bytes, max_redirects, headers,
    allowed_content_types, truncate, allowed_ports, watchdog,
) -> SafeResponse:
    origin = _origin(url)
    left_origin = False
    final_url = url
    redirects_left = max_redirects

    while True:
        left_origin = left_origin or _origin(final_url) != origin
        response = _do_request(
            session, final_url,
            timeout=_hop_timeout(timeout, watchdog),
            headers=_hop_headers(headers, left_origin),
            watchdog=watchdog,
        )
        try:
            if response.status_code in _REDIRECT_STATUSES:
                location = response.headers.get("Location") or ""
                if not location:
                    raise FetchError("redirect without Location header")
                if redirects_left <= 0:
                    raise FetchError("too many redirects")
                # The new URL goes through the full guard — public IP,
                # http/https only, etc. A redirect chain that ever points
                # at a private address is fully refused.
                final_url = _checked(
                    urljoin(final_url, location), allowed_ports, prefix="redirect refused",
                )
                redirects_left -= 1
                continue

            ct = (response.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if allowed_content_types and ct and not any(
                ct == ok for ok in allowed_content_types
            ):
                raise FetchError(f"disallowed content-type: {ct}", code="content_type")

            body = _read_body(response, max_bytes=max_bytes, truncate=truncate, watchdog=watchdog)
        finally:
            response.close()

        # Replace the streamed content so .text decodes it normally.
        response._content = body  # noqa: SLF001
        return SafeResponse(
            status_code=response.status_code,
            final_url=final_url,
            content_type=ct,
            headers=dict(response.headers),
            content=body,
            response=response,
        )


def _do_request(session, url: str, *, timeout, headers: dict, watchdog) -> requests.Response:
    """One hop, headers only: the body stays on the wire for _read_body."""
    # Cookies a previous hop set are never replayed.
    session.cookies.clear()
    try:
        return session.get(
            url,
            timeout=timeout,
            headers=headers,
            allow_redirects=False,
            stream=True,
        )
    except UnsafeURLError as exc:
        # The connected peer was not public (see _PublicPeerMixin).
        raise FetchError(f"unsafe URL: {exc}", code="blocked_url") from exc
    except requests.exceptions.RequestException as exc:
        raise _network_error(exc, watchdog) from exc


def _read_body(response, *, max_bytes: int, truncate: bool, watchdog) -> bytes:
    """Stream the body, stopping at ``max_bytes``.

    Past the cap we either abort (default) or keep the bytes read so far
    (``truncate``). Either way reading stops, so memory stays bounded.
    """
    body = bytearray()
    try:
        for chunk in response.iter_content(chunk_size=8192):
            if watchdog is not None and watchdog.expired:
                raise FetchError(_DEADLINE_MESSAGE, code="timeout")
            if not chunk:
                continue
            body += chunk
            if len(body) > max_bytes:
                if truncate:
                    del body[max_bytes:]
                    break
                raise FetchError(f"response body exceeds {max_bytes} bytes", code="too_large")
    except requests.exceptions.RequestException as exc:
        raise _network_error(exc, watchdog) from exc
    # A watchdog shutdown can also surface as an early end of the body.
    if watchdog is not None and watchdog.expired:
        raise FetchError(_DEADLINE_MESSAGE, code="timeout")
    return bytes(body)


def _network_error(exc: requests.exceptions.RequestException, watchdog) -> FetchError:
    if watchdog is not None and watchdog.expired:
        return FetchError(_DEADLINE_MESSAGE, code="timeout")
    code = "timeout" if isinstance(exc, requests.exceptions.Timeout) else "fetch_failed"
    return FetchError(str(exc)[:200], code=code)


def _checked(url: str, allowed_ports, *, prefix: str) -> str:
    try:
        return assert_url_safe(url, allowed_ports=allowed_ports)
    except UnsafeURLError as exc:
        raise FetchError(f"{prefix}: {exc}", code="blocked_url") from exc


def _origin(url: str) -> tuple[str, str, int]:
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    try:
        port = parts.port
    except ValueError:
        port = None
    return scheme, (parts.hostname or "").lower(), port or (443 if scheme == "https" else 80)


def _hop_headers(headers: dict | None, left_origin: bool) -> dict:
    out = dict(headers or {})
    if left_origin:
        out = {k: v for k, v in out.items() if k.lower() not in _CREDENTIAL_HEADERS}
    return out


def _hop_timeout(timeout, watchdog):
    if watchdog is None:
        return timeout
    remaining = watchdog.remaining()
    if remaining <= 0:
        raise FetchError(_DEADLINE_MESSAGE, code="timeout")
    if isinstance(timeout, tuple):
        return tuple(min(t, remaining) for t in timeout)
    return min(timeout, remaining)


def _new_session() -> requests.Session:
    session = requests.Session()
    # No HTTP(S)_PROXY / NO_PROXY / .netrc from the environment: a proxy
    # would make the connected-peer check meaningless.
    session.trust_env = False
    adapter = _PublicPeerAdapter()
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


class _PublicPeerMixin:
    """Refuses a connection whose peer is not public, then registers the
    socket with the running fetch's deadline watchdog."""

    def _new_conn(self):
        sock = super()._new_conn()
        try:
            assert_peer_public(sock)
        except UnsafeURLError:
            sock.close()
            raise
        watchdog = getattr(_local, "watchdog", None)
        if watchdog is not None:
            watchdog.track(sock)
        return sock


class _PublicHTTPConnection(_PublicPeerMixin, HTTPConnection):
    pass


class _PublicHTTPSConnection(_PublicPeerMixin, HTTPSConnection):
    pass


class _PublicHTTPConnectionPool(HTTPConnectionPool):
    ConnectionCls = _PublicHTTPConnection


class _PublicHTTPSConnectionPool(HTTPSConnectionPool):
    ConnectionCls = _PublicHTTPSConnection


class _PublicPeerAdapter(HTTPAdapter):
    def init_poolmanager(self, *args, **kwargs):
        super().init_poolmanager(*args, **kwargs)
        self.poolmanager.pool_classes_by_scheme = {
            "http": _PublicHTTPConnectionPool,
            "https": _PublicHTTPSConnectionPool,
        }


class _Watchdog:
    """A fetch's total wall-clock deadline.

    When it passes, every socket the fetch opened is shut down, which
    wakes a read blocked on a slow server.
    """

    def __init__(self, seconds: float):
        self._ends = time.monotonic() + seconds
        self._lock = threading.Lock()
        self._sockets: list = []
        self._fired = False
        self._timer = threading.Timer(max(seconds, 0.0), self._fire)
        self._timer.daemon = True
        self._timer.start()

    @property
    def expired(self) -> bool:
        return self._fired or time.monotonic() >= self._ends

    def remaining(self) -> float:
        return self._ends - time.monotonic()

    def track(self, sock) -> None:
        with self._lock:
            if not self._fired:
                self._sockets.append(sock)
                return
        _shutdown(sock)

    def cancel(self) -> None:
        self._timer.cancel()

    def _fire(self) -> None:
        with self._lock:
            self._fired = True
            sockets, self._sockets = self._sockets, []
        for sock in sockets:
            _shutdown(sock)


def _shutdown(sock) -> None:
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
