"""
Fetch-and-verify layer for prompt source attribution.

`search_sources` hands us a list of SerpAPI top results — domain hits that
share keywords with the prompt. We never want to label a prompt as
'Last seen on Forum · WeddingWire Forums' unless we actually fetched
that page and saw every meaningful keyword from the prompt in its body.

This module does exactly that:
  1. Strip the prompt to a set of content keywords (drop stopwords + punctuation).
  2. GET the candidate URL with a short timeout, capped body size, and a
     polite UA. Try a HEAD probe first so we skip non-HTML responses.
  3. Extract visible text from the HTML, normalize whitespace.
  4. PASS only when every keyword from step 1 appears in the body.
  5. Cache the (prompt, url) -> verdict tuple in Redis for 30 days so we
     don't re-hammer the same pages.

This is the 'Strict: all keywords on page' policy the user picked.
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from typing import Any
from urllib.parse import urlparse

from django.core.cache import cache

logger = logging.getLogger("apps")

# Reuse a small stopword list — we want the keywords that *carry topical
# weight*, not articles. Keeping this conservative on purpose: removing
# too many words would let weak matches slip through.
_STOPWORDS = frozenset({
    "a", "an", "and", "are", "as", "at", "be", "by", "do", "does", "did",
    "for", "from", "how", "i", "in", "is", "it", "its", "me", "my",
    "of", "on", "or", "should", "so", "than", "that", "the", "this",
    "to", "was", "we", "what", "when", "where", "which", "who", "why",
    "will", "with", "you", "your", "can", "could", "would",
    # generic question filler that often appears in templates
    "best", "good", "any", "some",
})

_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9\-']{1,}")
_SCRIPT_STYLE_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")

# Hard caps so a malicious or huge page can't wedge the worker.
_MAX_BODY_BYTES = 750_000        # ~750 KB of HTML is enough for verification
_REQUEST_TIMEOUT = 8             # seconds
_CACHE_TTL = 60 * 60 * 24 * 30   # 30 days
# NB: do NOT claim "respects robots.txt" here until it is actually enforced
# (planned separately) — an untrue claim in the UA is itself a problem.
_USER_AGENT = (
    "Cansee/1.0 (+https://cansee.ai/bot; verifies prompt sources)"
)

# Skip these — they're known to be JS-rendered or block bots, and a
# keyword check on the served HTML will always fail noisily.
_SKIP_HOSTS = frozenset({
    "twitter.com", "x.com", "instagram.com", "facebook.com",
    "tiktok.com", "linkedin.com",
})


def _prompt_keywords(prompt: str) -> list[str]:
    """Lowercased content words from the prompt, minus stopwords + dupes."""
    words = _WORD_RE.findall(prompt or "")
    out: list[str] = []
    seen: set[str] = set()
    for w in words:
        lw = w.lower()
        if lw in _STOPWORDS or len(lw) < 3:
            continue
        if lw in seen:
            continue
        seen.add(lw)
        out.append(lw)
    return out


def _cache_key(prompt: str, url: str) -> str:
    h = hashlib.sha1(f"{prompt}\n{url}".encode()).hexdigest()
    return f"psv:{h}"


def _strip_html(html: str) -> str:
    """Visible text from an HTML blob. Not perfect, but good enough to
    keyword-check forum/Q&A bodies, which is what the verifier targets."""
    no_script = _SCRIPT_STYLE_RE.sub(" ", html)
    no_tags = _TAG_RE.sub(" ", no_script)
    return _WS_RE.sub(" ", no_tags).strip().lower()


def _fetch(url: str) -> str | None:
    """GET the page, capped. Returns lowercased visible text, or None on
    any failure (timeout, non-HTML, oversized, host skiplisted)."""
    from core.validators.safe_http import FetchError, safe_get

    try:
        parsed = urlparse(url)
    except Exception:
        return None

    host = (parsed.hostname or "").lower()
    if not host or host in _SKIP_HOSTS:
        return None

    headers = {
        "User-Agent": _USER_AGENT,
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "en-US,en;q=0.8",
    }

    try:
        # safe_get applies the SSRF guard and re-validates every redirect
        # (the old allow_redirects=True let a SERP-supplied URL 302 to an
        # internal address), plus caps the body and checks content-type.
        resp = safe_get(
            url, headers=headers, timeout=_REQUEST_TIMEOUT,
            max_bytes=_MAX_BODY_BYTES,
        )
    except FetchError as exc:
        logger.info("source_verifier fetch failed %s: %s", host, exc)
        return None

    if resp.status_code != 200:
        return None
    return _strip_html(resp.text)


def verify_source(prompt: str, url: str) -> dict[str, Any]:
    """
    Return ``{verified: bool, matched: int, total: int, missing: [..],
    cached: bool}``.

    'Strict' policy: every non-stopword keyword from the prompt must
    appear in the fetched page body. We surface the missing keywords so
    the caller can debug why a candidate was rejected.
    """
    prompt = (prompt or "").strip()
    url = (url or "").strip()
    if not prompt or not url:
        return {"verified": False, "matched": 0, "total": 0, "missing": [], "cached": False}

    keywords = _prompt_keywords(prompt)
    if not keywords:
        # Nothing topical to verify against — treat as unverified so we
        # don't claim a source we never checked.
        return {"verified": False, "matched": 0, "total": 0, "missing": [], "cached": False}

    ck = _cache_key(prompt, url)
    cached = cache.get(ck)
    if cached is not None:
        cached["cached"] = True
        return cached

    body = _fetch(url)
    if body is None:
        verdict = {"verified": False, "matched": 0, "total": len(keywords),
                   "missing": keywords[:], "fetch_failed": True, "cached": False}
        cache.set(ck, verdict, timeout=_CACHE_TTL)
        return verdict

    missing = [k for k in keywords if k not in body]
    matched = len(keywords) - len(missing)
    verdict = {
        "verified": len(missing) == 0,
        "matched": matched,
        "total": len(keywords),
        "missing": missing,
        "fetch_failed": False,
        "cached": False,
    }
    cache.set(ck, verdict, timeout=_CACHE_TTL)
    return verdict


def filter_verified(
    prompt: str, rows: list[dict], *, max_seconds: float | None = None,
) -> list[dict]:
    """Return only the rows whose URL strictly verifies against ``prompt``.

    Annotates each kept row with `_verification` so the UI can show
    how many keywords matched (useful for a 'verified · 6/6 terms' badge).

    ``max_seconds`` bounds the whole pass: once it is spent, the remaining
    rows are left out as unverified instead of fetching more pages, so a
    request never waits on one slow page after another.
    """
    deadline = None if max_seconds is None else time.monotonic() + max_seconds
    out: list[dict] = []
    for row in rows:
        url = row.get("url") or ""
        if not url:
            continue
        if deadline is not None and time.monotonic() >= deadline:
            break
        v = verify_source(prompt, url)
        if v["verified"]:
            row = dict(row)
            row["_verification"] = {
                "matched": v["matched"],
                "total": v["total"],
                "cached": v.get("cached", False),
            }
            out.append(row)
    return out
