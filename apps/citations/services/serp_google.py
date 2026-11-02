"""Google SERP lane for Brand Research, via SerpAPI.

The scan's other discovery lane (``web_search`` / Perplexity) returns an
AI-era index: good recall on things Google buries, but the response carries
nothing beyond rank/url/title/snippet. Google's SERP has structure the
pipeline never saw — the forum block, People Also Ask, related searches, the
AI Overview, the knowledge panel — and that structure is most of what makes
a Brand Research scan more than a manual Google session.

One request per scan. Everything is best-effort: a failure here degrades the
scan to Perplexity-only discovery rather than failing it, so every entry
point returns a well-formed empty payload instead of raising.

Returns::

    {"configured": bool,
     "error": str,                     # "" when fine
     "organic":     [{rank, url, domain, title, snippet, date}],
     "discussions": [{rank, url, domain, title, snippet, source_label,
                      comment_count, date}],
     "questions":   [{question, snippet, url, domain}],
     "related_searches": [str],
     "ai_overview": {"text": str, "references": [{url, domain, title}]},
     "knowledge_graph": {"title", "type", "website", "description", "thumbnail"}}
"""

from __future__ import annotations

import logging
import re

from django.conf import settings

from apps.citations.services.url_normalizer import normalize_url

logger = logging.getLogger("apps")

SEARCH_ENDPOINT = "https://serpapi.com/search.json"
DEFAULT_MAX_RESULTS = 10
TIMEOUT = 15.0

# Caps. The SERP can return a lot of PAA/related-search chaff; past these
# counts it stops informing the graph and starts crowding it.
MAX_ORGANIC = 20
MAX_DISCUSSIONS = 10
MAX_QUESTIONS = 8
MAX_RELATED = 8
MAX_AI_REFERENCES = 10


def is_configured() -> bool:
    return bool(getattr(settings, "SERPAPI_KEY", ""))


def _empty(error: str = "", configured: bool = True) -> dict:
    return {
        "configured": configured,
        "error": error,
        "organic": [],
        "discussions": [],
        "questions": [],
        "related_searches": [],
        "ai_overview": {},
        "knowledge_graph": {},
    }


def _clean(value, limit: int = 500) -> str:
    return str(value or "").strip()[:limit]


def _host_of(url: str) -> str:
    """Normalized display host, or "" if the URL is unusable.

    Routing every URL through normalize_url also rejects non-http(s)
    schemes, which matters because these end up as clickable links.
    """
    _, host, _ = normalize_url(url)
    return host[4:] if host.startswith("www.") else host


def _parse_comment_count(value) -> int:
    """SerpAPI reports thread size inconsistently: sometimes an int, often a
    string like "42 comments" or "1.2K answers". Pull out what we can."""
    if isinstance(value, int):
        return max(0, value)
    text = str(value or "").strip().lower()
    if not text:
        return 0
    m = re.search(r"([\d.,]+)\s*([km])?", text)
    if not m:
        return 0
    try:
        number = float(m.group(1).replace(",", ""))
    except ValueError:
        return 0
    suffix = m.group(2)
    if suffix == "k":
        number *= 1_000
    elif suffix == "m":
        number *= 1_000_000
    return int(number)


def search(
    query: str,
    *,
    max_results: int = DEFAULT_MAX_RESULTS,
    timeout: float = TIMEOUT,
    start: int = 0,
    gl: str = "",
    hl: str = "",
) -> dict:
    """Run one SerpAPI Google search and parse every block we use.

    ``start`` pages through results (Google serves 10 per page since it
    stopped honouring ``num=100``); ``gl``/``hl`` set the country and
    interface language. Brand Research uses none of them: one first-page
    call. The search-gap rank check walks pages until it finds the site.

    Never raises. An unconfigured key, a network error, a non-200 or a
    non-JSON body all come back as an empty payload with ``error`` set.
    """
    query = (query or "").strip()
    if not query:
        return _empty("empty_query")
    if not is_configured():
        return _empty("serpapi_not_configured", configured=False)

    try:
        import requests
    except ImportError:  # pragma: no cover - requests is a hard dependency
        return _empty("requests_unavailable")

    params = {
        "q": query[:500],
        "engine": "google",
        "num": str(max(5, min(int(max_results), MAX_ORGANIC))),
        "api_key": settings.SERPAPI_KEY,
    }
    if start:
        params["start"] = str(max(0, int(start)))
    if gl:
        params["gl"] = gl.strip().lower()[:2]
    if hl:
        params["hl"] = hl.strip().lower()[:5]

    try:
        resp = requests.get(SEARCH_ENDPOINT, params=params, timeout=timeout)
    except Exception as exc:
        logger.warning("serp_google network error for %r: %s", query, exc)
        return _empty("network")

    if resp.status_code != 200:
        logger.warning("serp_google %r -> HTTP %s", query, resp.status_code)
        return _empty(f"http_{resp.status_code}")

    try:
        data = resp.json()
    except Exception:
        logger.warning("serp_google %r -> non-JSON body", query)
        return _empty("decode")

    if not isinstance(data, dict):
        return _empty("decode")

    payload = _empty()
    payload["organic"] = _parse_organic(data, max_results)
    payload["discussions"] = _parse_discussions(data)
    payload["questions"] = _parse_questions(data)
    payload["related_searches"] = _parse_related_searches(data)
    payload["ai_overview"] = _parse_ai_overview(data)
    payload["knowledge_graph"] = _parse_knowledge_graph(data)
    return payload


# -- block parsers ------------------------------------------------------------
# Each is defensive about shape: SerpAPI's schema varies by query and these
# blocks are absent far more often than they are present.


def _parse_organic(data: dict, max_results: int) -> list[dict]:
    rows = []
    for item in (data.get("organic_results") or [])[:MAX_ORGANIC]:
        if not isinstance(item, dict):
            continue
        url = _clean(item.get("link"), 2000)
        host = _host_of(url)
        if not host:
            continue
        # SerpAPI's own position is absolute across pages (11-20 on page
        # two); ``rank`` stays the 1-based index among the rows kept here.
        position = item.get("position")
        rows.append({
            "rank": len(rows) + 1,
            "position": position if isinstance(position, int) and position > 0 else None,
            "url": url,
            "domain": host,
            "title": _clean(item.get("title")),
            "snippet": _clean(item.get("snippet"), 1000),
            "date": _clean(item.get("date"), 40),
        })
        if len(rows) >= max_results:
            break
    return rows


def _parse_discussions(data: dict) -> list[dict]:
    """Google's forum/Reddit block.

    This is the highest-value block for Brand Research: Google has already
    decided which community threads answer the query, and it hands over the
    comment count, which is the best available proxy for whether a thread is
    worth joining.
    """
    raw = data.get("discussions_and_forums") or data.get("discussions") or []
    rows = []
    for item in raw[:MAX_DISCUSSIONS]:
        if not isinstance(item, dict):
            continue
        url = _clean(item.get("link"), 2000)
        host = _host_of(url)
        if not host:
            continue
        extensions = item.get("extensions") or {}
        comment_count = _parse_comment_count(
            item.get("comment_count")
            or item.get("comments")
            or (extensions.get("comments") if isinstance(extensions, dict) else None)
        )
        rows.append({
            "rank": len(rows) + 1,
            "url": url,
            "domain": host,
            "title": _clean(item.get("title")),
            "snippet": _clean(item.get("snippet") or item.get("answer"), 1000),
            "source_label": _clean(item.get("source") or item.get("displayed_link"), 120),
            "comment_count": comment_count,
            "date": _clean(item.get("date"), 40),
        })
    return rows


def _parse_questions(data: dict) -> list[dict]:
    """People Also Ask. These are literal questions real users type, so they
    double as content opportunities: a question with no good answer ranking
    is a gap the user can fill."""
    rows = []
    for item in (data.get("related_questions") or [])[:MAX_QUESTIONS]:
        if not isinstance(item, dict):
            continue
        question = _clean(item.get("question"), 300)
        if not question:
            continue
        url = _clean(item.get("link"), 2000)
        rows.append({
            "question": question,
            "snippet": _clean(item.get("snippet"), 600),
            "url": url,
            "domain": _host_of(url),
        })
    return rows


def _parse_related_searches(data: dict) -> list[str]:
    out = []
    seen = set()
    for item in (data.get("related_searches") or [])[:MAX_RELATED * 2]:
        query = ""
        if isinstance(item, dict):
            query = _clean(item.get("query") or item.get("name"), 200)
        elif isinstance(item, str):
            query = _clean(item, 200)
        fold = query.lower()
        if query and fold not in seen:
            seen.add(fold)
            out.append(query)
        if len(out) >= MAX_RELATED:
            break
    return out


def _parse_ai_overview(data: dict) -> dict:
    """Google's own generated answer, plus what it cited.

    Treated as a sixth AI engine downstream — it is a model recommending
    brands to the user's customers, which is exactly what the engine lane
    tracks. SerpAPI sometimes returns only a `page_token` here (the overview
    needs a second billed request); we deliberately do not chase it.
    """
    block = data.get("ai_overview")
    if not isinstance(block, dict):
        return {}

    chunks = []
    for part in block.get("text_blocks") or []:
        if not isinstance(part, dict):
            continue
        if part.get("snippet"):
            chunks.append(_clean(part["snippet"], 2000))
        for nested in part.get("list") or []:
            if isinstance(nested, dict) and nested.get("snippet"):
                chunks.append(_clean(nested["snippet"], 2000))

    references = []
    for ref in (block.get("references") or [])[:MAX_AI_REFERENCES]:
        if not isinstance(ref, dict):
            continue
        url = _clean(ref.get("link"), 2000)
        host = _host_of(url)
        if not host:
            continue
        references.append({
            "url": url,
            "domain": host,
            "title": _clean(ref.get("title")),
        })

    text = "\n\n".join(c for c in chunks if c)[:8000]
    if not text and not references:
        return {}
    return {"text": text, "references": references}


def _parse_knowledge_graph(data: dict) -> dict:
    """The right-hand brand panel. Gives the detail panel a canonical site and
    description without spending a Perplexity brand-lookup call per click."""
    block = data.get("knowledge_graph")
    if not isinstance(block, dict):
        return {}
    website = _clean(block.get("website"), 2000)
    out = {
        "title": _clean(block.get("title"), 200),
        "type": _clean(block.get("type"), 120),
        "website": website if _host_of(website) else "",
        "description": _clean(block.get("description"), 1000),
        "thumbnail": _clean(block.get("thumbnail"), 2000),
    }
    return out if any(out.values()) else {}


# -- discovery merge ----------------------------------------------------------


def merge_discovery(perplexity_rows: list[dict], google_rows: list[dict], *, limit: int) -> list[dict]:
    """Union two ranked result lists into one, deduped by normalized URL.

    A URL both indexes surface is a stronger signal than one only a single
    index found, so agreement is the primary sort key and best rank the
    tiebreaker. Each surviving row carries ``discovered_by`` so the UI can
    show where it came from.
    """
    merged: dict[str, dict] = {}
    order: list[str] = []

    def absorb(rows: list[dict], source: str) -> None:
        for row in rows or []:
            url = (row.get("url") or "").strip()
            normalized, host, _ = normalize_url(url)
            if not normalized or not host:
                continue
            existing = merged.get(normalized)
            if existing is None:
                entry = dict(row)
                entry["url"] = url
                entry["domain"] = row.get("domain") or (
                    host[4:] if host.startswith("www.") else host
                )
                entry["discovered_by"] = [source]
                merged[normalized] = entry
                order.append(normalized)
                continue
            if source not in existing["discovered_by"]:
                existing["discovered_by"].append(source)
            # Keep the better (lower) rank, and backfill any field the
            # first index left empty.
            if (row.get("rank") or 999) < (existing.get("rank") or 999):
                existing["rank"] = row["rank"]
            for field in ("title", "snippet", "date", "last_updated"):
                if not existing.get(field) and row.get(field):
                    existing[field] = row[field]

    absorb(perplexity_rows, "perplexity")
    absorb(google_rows, "google")

    rows = [merged[key] for key in order]
    rows.sort(key=lambda r: (-len(r.get("discovered_by") or []), r.get("rank") or 999))
    rows = rows[:limit]
    # Rank is positional in the merged list from here on; downstream code
    # (rank weighting, unique_together on (scan, rank)) requires 1..N dense.
    for index, row in enumerate(rows, start=1):
        row["rank"] = index
    return rows
