"""Shared helpers for OpenAI-compatible Responses API providers.

OpenAI and xAI both expose web search through the Responses API: the
response's ``output`` list holds ``web_search_call`` items (the queries
the model ran) and ``message`` items whose ``output_text`` content parts
carry ``annotations`` — ``url_citation`` entries naming the exact source
behind each cited span. This module turns that structure into the flat,
ordered URL list ProviderResult.citations expects, and the searches into
ProviderResult.search_queries and web_searches.

Every read tolerates both typed SDK objects and plain dicts: pinned SDK
versions parse unknown-to-them fields as dicts (the same trap the Claude
provider hit), and test doubles are dicts too.
"""
from __future__ import annotations

MAX_CITATIONS = 20
# Query fan-outs kept per answer, and the characters kept per query.
MAX_SEARCH_QUERIES = 20
MAX_QUERY_CHARS = 300


def _get(item, key, default=None):
    if isinstance(item, dict):
        return item.get(key, default)
    return getattr(item, key, default)


def clean_search_queries(queries) -> list[str]:
    """Query texts as stored: whitespace collapsed, empty ones dropped,
    de-duplicated in order (case kept), at most MAX_SEARCH_QUERIES, each
    clipped to MAX_QUERY_CHARS."""
    out: list[str] = []
    seen: set[str] = set()
    for query in queries or []:
        if not isinstance(query, str):
            continue
        text = " ".join(query.split())[:MAX_QUERY_CHARS]
        if text and text not in seen:
            seen.add(text)
            out.append(text)
            if len(out) >= MAX_SEARCH_QUERIES:
                break
    return out


def extract_search_queries(resp) -> tuple[list[str], int]:
    """(query texts, searches run) from a Responses API response.

    Each ``web_search_call`` output item is one search. Its ``action`` says
    what it did: a ``search`` carries ``queries`` (a list) or ``query``;
    ``open_page`` and ``find_in_page`` count as searches but carry no query.
    OpenAI documents ``action`` and its query fields as optional, so a
    search can come without its text.
    """
    queries: list = []
    searches = 0
    for item in _get(resp, "output", None) or []:
        if _get(item, "type") != "web_search_call":
            continue
        searches += 1
        action = _get(item, "action", None)
        if action is None or _get(action, "type") != "search":
            continue
        many = _get(action, "queries", None)
        if isinstance(many, list | tuple) and many:
            queries.extend(many)
        else:
            queries.append(_get(action, "query", None))
    return clean_search_queries(queries), searches


def extract_responses_citations(resp) -> list[str]:
    """Ordered, deduped source URLs from a Responses API response.

    Cited pages (url_citation annotations, in answer order) come first;
    a provider-level ``citations`` list of URL strings (xAI's legacy
    shape) is appended after. Capped at MAX_CITATIONS.
    """
    urls: list[str] = []
    seen: set[str] = set()

    def _add(url) -> None:
        if isinstance(url, str) and url and url not in seen and len(urls) < MAX_CITATIONS:
            seen.add(url)
            urls.append(url)

    for item in _get(resp, "output", None) or []:
        if _get(item, "type") != "message":
            continue
        for part in _get(item, "content", None) or []:
            if _get(part, "type") != "output_text":
                continue
            for ann in _get(part, "annotations", None) or []:
                if _get(ann, "type") == "url_citation":
                    _add(_get(ann, "url"))

    for url in _get(resp, "citations", None) or []:
        _add(url)

    return urls
