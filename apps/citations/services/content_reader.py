"""
Type-aware content readers for Source Intelligence scans.

Facade over the sources service. The reader implementations (Reddit
JSON API, Yelp Fusion API, HTML text extraction) live in
services/sources/logic.py (framework-free, single source of truth).
Two modes:

- SOURCES_SERVICE_URL set (production): read_url() delegates to the
  internal sources service over HTTP with a bearer token; the SSRF
  guard runs inside the service.
- unset (dev, tests, CI): dispatch locally. read_page keeps the
  SSRF-guarded fetch here (core.validators.safe_http) and delegates
  text extraction to the shared logic.

Returned shape (always):
    {"status": "ok"|"blocked"|"error", "kind": "reddit"|"yelp"|"page",
     "text": str, "word_count": int, "detail": str}
"""

from __future__ import annotations

import logging

import requests
from django.conf import settings

from core.validators.safe_http import FetchError, safe_get
from services.sources import logic as sources_logic
from services.sources.logic import (  # noqa: F401
    MAX_TEXT_CHARS,
    PAGE_DEADLINE,
    PAGE_TIMEOUT,
    USER_AGENT,
)

logger = logging.getLogger("apps")


def read_reddit_thread(url: str) -> dict:
    """Fetch a Reddit thread via the public JSON API, preserving upvotes."""
    return sources_logic.read_reddit_thread(url)


def read_yelp(url: str) -> dict:
    """Read a Yelp URL through the Fusion API."""
    return sources_logic.read_yelp(
        url, api_key=getattr(settings, "YELP_API_KEY", "") or ""
    )


def read_page(url: str) -> dict:
    """Fetch a regular page through the SSRF guard and extract its text."""
    try:
        resp = safe_get(
            url, timeout=PAGE_TIMEOUT, deadline=PAGE_DEADLINE,
            headers={"User-Agent": USER_AGENT},
        )
    except FetchError as exc:
        return sources_logic.result(
            sources_logic.fetch_error_status(str(exc)), "page", detail=str(exc)
        )
    return sources_logic.page_result_from_html(resp.text)


def read_url(url: str) -> dict:
    """Dispatch to the right reader for the URL's source type."""
    service_url = getattr(settings, "SOURCES_SERVICE_URL", "") or ""
    if service_url:
        try:
            resp = requests.post(
                f"{service_url.rstrip('/')}/v1/read",
                json={"url": url},
                headers={
                    "Authorization": f"Bearer {getattr(settings, 'SOURCES_AUTH_TOKEN', '')}",
                },
                timeout=PAGE_TIMEOUT + 10,
            )
            resp.raise_for_status()
            return resp.json()
        except (requests.RequestException, ValueError) as exc:
            logger.warning("Sources service read failed for %s: %s", url, exc)
            return sources_logic.result(
                "error", "page", detail=f"sources service unavailable: {exc}"
            )

    if sources_logic.is_reddit_thread(url):
        return read_reddit_thread(url)
    if sources_logic.is_yelp(url):
        return read_yelp(url)
    return read_page(url)
