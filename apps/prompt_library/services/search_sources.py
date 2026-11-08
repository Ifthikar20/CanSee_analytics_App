"""Live Google search lookup for a generated prompt.

Surfaces "where this question is being asked from" AND "WHO is asking it" —
top organic results on Google for the prompt text, with best-effort
extraction of the original asker (username, name, subreddit) and the
community they're posting in.

Falls back gracefully when SERPAPI_KEY is missing so dev environments still
work; the frontend renders an explanatory empty state in that case.
"""
from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Callable
from typing import Any
from urllib.parse import urlparse

from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger("apps")


_SOURCE_RULES: list[tuple[str, str]] = [
    (r"(^|\.)reddit\.com$", "reddit"),
    (r"(^|\.)quora\.com$", "quora"),
    (r"(^|\.)stackexchange\.com$", "stackoverflow"),
    (r"(^|\.)stackoverflow\.com$", "stackoverflow"),
    (r"(^|\.)wikipedia\.org$", "wikipedia"),
    (r"(^|\.)youtube\.com$", "youtube"),
    (r"(^|\.)twitter\.com$", "social"),
    (r"(^|\.)x\.com$", "social"),
    (r"(^|\.)linkedin\.com$", "social"),
    (r"(^|\.)facebook\.com$", "social"),
    (r"(^|\.)instagram\.com$", "social"),
    (r"(^|\.)tiktok\.com$", "social"),
    (r"(^|\.)yelp\.com$", "review"),
    (r"(^|\.)tripadvisor\.com$", "review"),
    (r"(^|\.)trustpilot\.com$", "review"),
    (r"(^|\.)g2\.com$", "review"),
    (r"(^|\.)nytimes\.com$", "news"),
    (r"(^|\.)bbc\.com$", "news"),
    (r"(^|\.)theguardian\.com$", "news"),
    (r"(^|\.)techcrunch\.com$", "news"),
    (r"(^|\.)bloomberg\.com$", "news"),
    (r"(^|\.)forbes\.com$", "news"),
    (r"(^|\.)medium\.com$", "blog"),
    (r"(^|\.)substack\.com$", "blog"),
    (r"(^|\.)gov$", "gov"),
    (r"(^|\.)edu$", "edu"),
]

# Heuristics for finding the asker — Google snippets often contain
# 'Posted by u/foo · 2 years ago' (Reddit), 'Marcus B. answered' (Quora),
# 'by Author Name', etc. We rely on cheap regex on title+snippet rather
# than scraping the page.
_ASKER_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\bu/([A-Za-z0-9_\-]{3,20})\b"),
    re.compile(r"\bby\s+([A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z\.]+){0,2})\b"),
    re.compile(r"\bPosted\s+by\s+([A-Za-z0-9_\-\.]+)"),
    re.compile(r"\bAuthor:\s+([A-Za-z0-9_\-\.\s]{2,30})"),
    re.compile(r"\b@([A-Za-z0-9_]{2,15})\b"),
]
_DATE_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"(\d{1,2}\s+\w+\s+\d{4})"),
    re.compile(r"(\w+\s+\d{1,2},\s+\d{4})"),
    re.compile(r"(\d+\s+(?:hour|day|week|month|year)s?\s+ago)", re.IGNORECASE),
    re.compile(r"((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{4})"),
]


def _classify_domain(host: str) -> str:
    h = (host or "").lower().removeprefix("www.")
    for pat, label in _SOURCE_RULES:
        if re.search(pat, h):
            return label
    return "other"


def _community_from_url(host: str, path: str, source_class: str) -> str:
    """Where on the site this thread lives — subreddit, quora topic, etc."""
    if source_class == "reddit":
        m = re.search(r"/r/([A-Za-z0-9_]+)", path or "")
        if m:
            return f"r/{m.group(1)}"
    if source_class == "quora":
        m = re.search(r"/topic/([A-Za-z0-9\-]+)", path or "")
        if m:
            return m.group(1).replace("-", " ")
    if source_class == "stackoverflow":
        if "stackexchange.com" in (host or ""):
            sub = (host or "").split(".")[0]
            return sub
        return "Stack Overflow"
    if source_class == "youtube":
        m = re.search(r"/(?:c|@|user|channel)/([^/]+)", path or "")
        if m:
            return f"@{m.group(1)}"
        return "YouTube"
    if source_class == "social":
        m = re.search(r"^/([A-Za-z0-9_]+)", path or "")
        if m:
            return f"@{m.group(1)}"
    return ""


def _extract_asker(title: str, snippet: str, host: str, path: str, source_class: str) -> str:
    """Best-effort handle/name extraction. Empty string when nothing matches."""
    text = f"{title}  {snippet}"
    for pat in _ASKER_PATTERNS:
        m = pat.search(text)
        if not m:
            continue
        handle = m.group(1).strip().rstrip(".,;:")
        if handle.lower() in {"reddit", "quora", "youtube", "the", "and", "for"}:
            continue
        if pat.pattern.startswith(r"\bu/"):
            return f"u/{handle}"
        return handle
    if source_class == "youtube":
        m = re.search(r"/(?:c|@|user|channel)/([^/?]+)", path or "")
        if m:
            return f"@{m.group(1)}"
    return ""


def _extract_posted_at(snippet: str) -> str:
    for pat in _DATE_PATTERNS:
        m = pat.search(snippet or "")
        if m:
            return m.group(1).strip()
    return ""


def _row(item: dict, position: int) -> dict | None:
    title = (item.get("title") or "").strip()
    link = (item.get("link") or "").strip()
    if not title or not link:
        return None
    try:
        parsed = urlparse(link)
        host = parsed.hostname or ""
        path = parsed.path or ""
    except Exception:
        host, path = "", ""
    snippet = (item.get("snippet") or "").strip()
    source_class = _classify_domain(host)
    return {
        "title": title,
        "url": link,
        "domain": host,
        "snippet": snippet[:280],
        "position": position,
        "source_class": source_class,
        "community": _community_from_url(host, path, source_class),
        "asker": _extract_asker(title, snippet, host, path, source_class),
        "posted_at": _extract_posted_at(snippet),
    }


def _build_groups(rows: list[dict]) -> list[dict]:
    """Group rows by source_class so the UI can render 'Who's asking on
    Reddit / Quora / Yelp …' clusters cleanly."""
    by_class: dict[str, dict] = {}
    for r in rows:
        cls = r["source_class"] or "other"
        bucket = by_class.setdefault(cls, {
            "source_class": cls,
            "count": 0,
            "askers": [],
            "communities": set(),
        })
        bucket["count"] += 1
        bucket["askers"].append({
            "asker": r["asker"],
            "title": r["title"],
            "url": r["url"],
            "community": r["community"],
            "domain": r["domain"],
            "snippet": r["snippet"],
            "posted_at": r["posted_at"],
        })
        if r["community"]:
            bucket["communities"].add(r["community"])
    out: list[dict] = []
    for cls in sorted(by_class.keys(), key=lambda k: (-by_class[k]["count"], k)):
        b = by_class[cls]
        out.append({
            "source_class": b["source_class"],
            "count": b["count"],
            "communities": sorted(b["communities"]),
            "askers": b["askers"],
        })
    return out


# A SerpAPI search is paid per call, so a successful answer is reused for a
# day: the same prompt asked again (or by a teammate) costs nothing.
_RESULT_CACHE_TTL = 60 * 60 * 24
# Wall-clock budget for verifying candidate pages inside the request.
_VERIFY_SECONDS = 15


class SearchRateLimited(Exception):
    """The caller's search allowance is used up; try again later."""


def _result_cache_key(query: str, limit: int) -> str:
    normalised = " ".join(query.lower().split())
    digest = hashlib.sha256(f"{limit}\n{normalised}".encode()).hexdigest()
    return f"psrc:v1:{digest}"


def search_sources(
    query: str,
    *,
    limit: int = 8,
    allow_paid_call: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Return ``{provider, query, results, groups}``. ``groups`` clusters
    results by source_class with extracted asker info. Empty when SerpAPI
    isn't configured or the call fails — caller should treat that as a
    graceful 'no data yet' state, NOT an error.

    A successful answer is cached for a day by normalised query. On a cache
    miss ``allow_paid_call`` (when given) is asked before SerpAPI is
    called; ``SearchRateLimited`` is raised when it says no.
    """
    query = (query or "").strip()
    if not query:
        return {"provider": "fallback", "results": [], "groups": [], "query": query}

    api_key = getattr(settings, "SERPAPI_KEY", "")
    if not api_key:
        return {"provider": "unconfigured", "results": [], "groups": [], "query": query}

    key = _result_cache_key(query, limit)
    cached = cache.get(key)
    if cached is not None:
        return {**cached, "query": query, "cached": True}
    if allow_paid_call is not None and not allow_paid_call():
        raise SearchRateLimited()

    result = _search_serpapi(query, limit=limit, api_key=api_key)
    if not result.get("error"):
        cache.set(key, result, timeout=_RESULT_CACHE_TTL)
    return result


def _search_serpapi(query: str, *, limit: int, api_key: str) -> dict[str, Any]:
    try:
        import requests
    except ImportError:  # pragma: no cover
        return {"provider": "unconfigured", "results": [], "groups": [], "query": query}

    try:
        resp = requests.get(
            "https://serpapi.com/search.json",
            params={
                "q": query[:500],
                "engine": "google",
                "num": str(max(5, min(limit, 10))),
                "api_key": api_key,
            },
            timeout=12,
        )
    except Exception as exc:
        logger.warning("search_sources network error: %s", exc)
        return {
            "provider": "serpapi",
            "results": [],
            "groups": [],
            "query": query,
            "error": "network",
        }

    if resp.status_code != 200:
        logger.warning("search_sources non-200: %s", resp.status_code)
        return {
            "provider": "serpapi",
            "results": [],
            "groups": [],
            "query": query,
            "error": f"http_{resp.status_code}",
        }

    try:
        data = resp.json()
    except Exception:
        return {
            "provider": "serpapi",
            "results": [],
            "groups": [],
            "query": query,
            "error": "decode",
        }

    organic = data.get("organic_results") or []
    # Pull a wider candidate set than the caller asked for, since the
    # verifier will reject most pages that didn't actually discuss the
    # prompt. We trim back to `limit` after verification.
    candidate_cap = max(limit * 3, 12)
    candidates: list[dict] = []
    for i, item in enumerate(organic[:candidate_cap], start=1):
        row = _row(item, i)
        if row is not None:
            candidates.append(row)

    from apps.prompt_library.services.source_verifier import filter_verified
    verified = filter_verified(
        query, candidates, max_seconds=_VERIFY_SECONDS,
    )[:limit]

    return {
        "provider": "serpapi",
        "results": verified,
        "groups": _build_groups(verified),
        "query": query,
        "verification": {
            "candidates": len(candidates),
            "verified": len(verified),
            "policy": "strict_all_keywords",
        },
    }
