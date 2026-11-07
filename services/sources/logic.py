"""
Framework-free content acquisition logic for the sources service.

Single source of truth for the Perplexity web search client and the
type-aware content readers (Reddit JSON API, Yelp Fusion API,
SSRF-guarded generic pages). Imported two ways:

- by services.sources.main (the FastAPI app) in production, and
- by apps/citations/services/{web_search,content_reader}.py directly
  when SOURCES_SERVICE_URL is unset (dev, tests, CI).

No Django imports. Credentials are plain arguments. Quotas and circuit
breakers stay with the Django callers; this module raises typed errors
(SearchRequestError / SearchParseError) so callers can map failures to
their own resilience machinery.

Reader return shape (always):
    {"status": "ok"|"blocked"|"error", "kind": "reddit"|"yelp"|"page",
     "text": str, "word_count": int, "detail": str}
"""

from __future__ import annotations

import json
import logging
from html.parser import HTMLParser
from urllib.parse import parse_qs, unquote, urlparse

import requests

from services.sources.safe_http import FetchError, safe_get

logger = logging.getLogger(__name__)

USER_AGENT = "Cansee/1.0 (source intelligence; +https://cansee.ai)"
MAX_TEXT_CHARS = 20000
MAX_REDDIT_COMMENTS = 40
PAGE_TIMEOUT = 12.0
# Whole-fetch bound (redirects and body included). PAGE_TIMEOUT applies per
# socket operation, so a server dripping its body could otherwise hold a
# worker indefinitely. Kept under the PAGE_TIMEOUT + 10 s the Django facade
# waits for the sources service, so the service answers before it gives up.
PAGE_DEADLINE = 20.0

SEARCH_ENDPOINT = "https://api.perplexity.ai/search"
CHAT_ENDPOINT = "https://api.perplexity.ai/chat/completions"
SUMMARIZE_MODEL = "sonar"
DEFAULT_MAX_RESULTS = 10


class SearchRequestError(Exception):
    """The Perplexity request failed at the transport level."""


class SearchParseError(Exception):
    """The Perplexity response was not valid JSON."""


def domain_of(url: str) -> str:
    host = urlparse(url).hostname or ""
    return host[4:] if host.startswith("www.") else host


def perplexity_search(
    query: str,
    *,
    api_key: str,
    max_results: int = DEFAULT_MAX_RESULTS,
    timeout: float = 20.0,
) -> list[dict]:
    """
    Run one Perplexity web search. Returns:

        [{"rank": 1, "url": ..., "domain": ..., "title": ..., "snippet": ...,
          "date": ..., "last_updated": ...}, ...]

    Raises SearchRequestError / SearchParseError; the caller owns
    breaker bookkeeping and quota checks.
    """
    try:
        resp = requests.post(
            SEARCH_ENDPOINT,
            json={"query": query, "max_results": max(1, min(int(max_results), 20))},
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            timeout=timeout,
        )
        resp.raise_for_status()
        body = resp.json()
    except requests.RequestException as exc:
        raise SearchRequestError(str(exc)) from exc
    except ValueError as exc:
        raise SearchParseError(str(exc)) from exc

    raw = body.get("results") or body.get("web_results") or []
    results = []
    for idx, item in enumerate(raw[:max_results], start=1):
        url = (item.get("url") or item.get("link") or "").strip()
        if not url:
            continue
        results.append({
            "rank": idx,
            "url": url,
            "domain": domain_of(url),
            "title": item.get("title", "") or "",
            "snippet": (item.get("snippet") or item.get("description") or "")[:1000],
            "date": (item.get("date") or "").strip(),
            "last_updated": (item.get("last_updated") or "").strip(),
        })
    return results


def perplexity_summarize_url(
    url: str,
    *,
    query: str,
    api_key: str,
    timeout: float = 20.0,
) -> dict | None:
    """Ask Perplexity to visit a URL and return a factual, brand-focused summary.

    Returns {"text": <summary>, "word_count": N} or None when the API
    is disabled/quotaless. Raises SearchRequestError / SearchParseError
    on transport / decode failure — the caller owns breaker bookkeeping.

    We steer the model toward name-preserving prose so the downstream
    brand extractor has verbatim names to work with, and cap tokens so
    a single fallback doesn't blow the quota on a 500-item scan.
    """
    if not api_key:
        return None

    system = (
        "You visit the given URL and produce a factual summary of the "
        "page's content that a brand analyst can use. Preserve brand, "
        "product, and place names verbatim. Include any ratings, "
        "complaints, and direct quotes from users if present. If the "
        "page is inaccessible or contains no relevant content, reply "
        "with only the word NONE."
    )
    user_msg = (
        f"URL: {url}\n"
        f"Query context: {query or '(no query)'}\n"
        "Summary:"
    )

    try:
        resp = requests.post(
            CHAT_ENDPOINT,
            json={
                "model": SUMMARIZE_MODEL,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user_msg},
                ],
                "max_tokens": 700,
                "temperature": 0.2,
            },
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            timeout=timeout,
        )
        resp.raise_for_status()
        body = resp.json()
    except requests.RequestException as exc:
        raise SearchRequestError(str(exc)) from exc
    except ValueError as exc:
        raise SearchParseError(str(exc)) from exc

    choices = body.get("choices") or []
    if not choices:
        return None
    text = ((choices[0].get("message") or {}).get("content") or "").strip()
    if not text or text.upper() == "NONE":
        return None
    return {"text": text[:MAX_TEXT_CHARS], "word_count": len(text.split())}


# -- Shared result shaping ------------------------------------------------------

def result(status: str, kind: str, text: str = "", detail: str = "") -> dict:
    return {
        "status": status,
        "kind": kind,
        "text": text[:MAX_TEXT_CHARS],
        "word_count": len(text.split()),
        "detail": detail[:300],
    }


def fetch_error_status(detail: str) -> str:
    return "blocked" if "403" in detail or "401" in detail else "error"


# -- Reddit -------------------------------------------------------------------

def is_reddit_thread(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    # Dot-anchor the host check: a bare ``endswith("reddit.com")`` also
    # matches ``evilreddit.com``, letting an attacker route this fetch at an
    # arbitrary host. Only reddit.com and its subdomains qualify.
    is_reddit_host = host == "reddit.com" or host.endswith(".reddit.com")
    return is_reddit_host and "/comments/" in url


def _flatten_comments(children: list, out: list) -> None:
    for child in children:
        data = child.get("data") or {}
        if child.get("kind") != "t1":
            continue
        body = (data.get("body") or "").strip()
        if body and body not in ("[deleted]", "[removed]"):
            out.append({"text": body, "score": int(data.get("score") or 0)})
        replies = data.get("replies")
        if isinstance(replies, dict):
            _flatten_comments((replies.get("data") or {}).get("children") or [], out)


def read_reddit_thread(url: str) -> dict:
    """Fetch a Reddit thread via the public JSON API, preserving upvotes."""
    json_url = url.split("?")[0].rstrip("/") + ".json"
    try:
        # safe_get enforces the SSRF guard (scheme allowlist, private/metadata
        # IP blocking, redirect re-validation) — the URL is user-influenceable
        # (it arrives from SERP results), so a raw fetch here was an SSRF hole.
        resp = safe_get(
            json_url,
            headers={"User-Agent": USER_AGENT},
            timeout=PAGE_TIMEOUT,
            deadline=PAGE_DEADLINE,
            max_bytes=3_000_000,
            allowed_content_types=("application/json", "text/plain"),
        )
    except FetchError as exc:
        return result(fetch_error_status(str(exc)), "reddit", detail=str(exc))

    if resp.status_code >= 400:
        return result(
            fetch_error_status(str(resp.status_code)), "reddit",
            detail=f"HTTP {resp.status_code}",
        )
    try:
        payload = json.loads(resp.text)
    except ValueError as exc:
        return result("error", "reddit", detail=f"non-JSON: {exc}")

    try:
        post = payload[0]["data"]["children"][0]["data"]
        comment_children = payload[1]["data"]["children"]
    except (IndexError, KeyError, TypeError) as exc:
        return result("error", "reddit", detail=f"unexpected shape: {exc}")

    comments: list[dict] = []
    _flatten_comments(comment_children, comments)
    comments.sort(key=lambda c: c["score"], reverse=True)
    comments = comments[:MAX_REDDIT_COMMENTS]

    lines = [
        f"REDDIT THREAD: {post.get('title', '')}",
        f"Subreddit: r/{post.get('subreddit', '')}  Post score: {post.get('score', 0)}",
    ]
    selftext = (post.get("selftext") or "").strip()
    if selftext:
        lines.append(f"Post body: {selftext[:2000]}")
    lines.append("Comments (sorted by upvotes; score = community agreement):")
    for comment in comments:
        lines.append(f"[+{comment['score']}] {comment['text'][:1500]}")

    return result("ok", "reddit", "\n".join(lines))


# -- Yelp (official Fusion API) ------------------------------------------------

YELP_API_BASE = "https://api.yelp.com/v3"
MAX_YELP_BUSINESSES = 10


def is_yelp(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    # Dot-anchored (see is_reddit_thread): avoid matching e.g. evilyelp.com.
    return host == "yelp.com" or host.endswith(".yelp.com")


def _yelp_get(path: str, *, api_key: str, params: dict | None = None) -> dict | None:
    try:
        resp = requests.get(
            f"{YELP_API_BASE}{path}",
            params=params or {},
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=PAGE_TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json()
    except (requests.RequestException, ValueError) as exc:
        logger.warning("Yelp API call %s failed: %s", path, exc)
        return None


def _yelp_business_lines(biz: dict) -> str:
    categories = ", ".join(
        c.get("title", "") for c in (biz.get("categories") or [])[:3]
    )
    return (
        f"- {biz.get('name', '?')}: {biz.get('rating', '?')} stars from "
        f"{biz.get('review_count', 0)} reviews"
        + (f", price {biz.get('price')}" if biz.get("price") else "")
        + (f" ({categories})" if categories else "")
    )


def read_yelp(url: str, *, api_key: str) -> dict:
    """Read a Yelp URL through the Fusion API.

    Star ratings and review counts are explicit, quantified sentiment;
    the text block states that so the extraction stage uses them.
    """
    if not api_key:
        return result("blocked", "yelp", detail="YELP_API_KEY not configured")

    parsed = urlparse(url)
    parts = [p for p in parsed.path.split("/") if p]

    # Business page: yelp.com/biz/<alias>
    if parts and parts[0] == "biz" and len(parts) > 1:
        alias = unquote(parts[1])
        biz = _yelp_get(f"/businesses/{alias}", api_key=api_key)
        if biz is None:
            return result("error", "yelp", detail="business lookup failed")
        lines = [
            "YELP BUSINESS (official API; star ratings are aggregate customer sentiment):",
            _yelp_business_lines(biz),
        ]
        reviews = _yelp_get(f"/businesses/{alias}/reviews", api_key=api_key) or {}
        for review in (reviews.get("reviews") or [])[:3]:
            lines.append(
                f"[{review.get('rating', '?')} stars] {(review.get('text') or '').strip()}"
            )
        return result("ok", "yelp", "\n".join(lines))

    # Search page: yelp.com/search?find_desc=Bagels&find_loc=Dallas, TX
    # Category page: yelp.com/c/<city>/<category>
    term = location = ""
    if parts and parts[0] == "search":
        qs = parse_qs(parsed.query)
        term = (qs.get("find_desc") or [""])[0]
        location = (qs.get("find_loc") or [""])[0]
    elif parts and parts[0] == "c" and len(parts) > 2:
        location = unquote(parts[1]).replace("-", " ")
        term = unquote(parts[2]).replace("-", " ")

    if not (term and location):
        return result("blocked", "yelp", detail="unrecognized yelp url shape")

    data = _yelp_get(
        "/businesses/search",
        api_key=api_key,
        params={"term": term, "location": location, "limit": MAX_YELP_BUSINESSES,
                "sort_by": "best_match"},
    )
    if data is None:
        return result("error", "yelp", detail="business search failed")
    businesses = data.get("businesses") or []
    if not businesses:
        return result("error", "yelp", detail="no businesses returned")

    lines = [
        f"YELP RANKING for {term!r} in {location!r} (official API; order is "
        "Yelp's relevance ranking, star ratings are aggregate customer sentiment):",
    ]
    lines.extend(_yelp_business_lines(b) for b in businesses)
    return result("ok", "yelp", "\n".join(lines))


# -- Generic pages ------------------------------------------------------------

class _TextExtractor(HTMLParser):
    _SKIP = {"script", "style", "noscript", "svg", "nav", "footer", "header"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self.chunks: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skip_depth += 1

    def handle_endtag(self, tag):
        if tag in self._SKIP and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data):
        if not self._skip_depth:
            stripped = data.strip()
            if stripped:
                self.chunks.append(stripped)


def page_result_from_html(html: str) -> dict:
    """Reduce fetched HTML to the standard visible-text result dict."""
    extractor = _TextExtractor()
    try:
        extractor.feed(html)
    except Exception as exc:
        return result("error", "page", detail=f"parse error: {exc}")
    return result("ok", "page", "\n".join(extractor.chunks))


def read_page(url: str) -> dict:
    """Fetch a regular page through the SSRF guard and extract its text."""
    try:
        resp = safe_get(
            url, timeout=PAGE_TIMEOUT, deadline=PAGE_DEADLINE,
            headers={"User-Agent": USER_AGENT},
        )
    except FetchError as exc:
        return result(fetch_error_status(str(exc)), "page", detail=str(exc))
    return page_result_from_html(resp.text)


def read_url(url: str, *, yelp_api_key: str = "") -> dict:
    """Dispatch to the right reader for the URL's source type."""
    if is_reddit_thread(url):
        return read_reddit_thread(url)
    if is_yelp(url):
        return read_yelp(url, api_key=yelp_api_key)
    return read_page(url)
