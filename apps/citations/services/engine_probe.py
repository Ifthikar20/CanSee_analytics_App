"""AI-engine lane for Brand Research.

Asks every configured LLM the scan's query the way a customer would, then
reads the answer with the same extractor the web lane uses. The result is
the part of the graph a Google search cannot give you: which engines
recommend which brands, and which pages taught them to.

Three things distinguish this from ``brand_vault``'s ``ask_all_providers``:

1. Unconfigured providers get a row with ``status="not_configured"`` instead
   of being silently dropped, so the UI can grey out an engine rather than
   pretend it does not exist.
2. Answers go through ``analyze_content`` -- the same prompt, schema and
   junk filter as a web page -- so an engine's brand mentions are directly
   comparable to a Reddit thread's.
3. Cited URLs are pulled out and cross-linked against the scan's own source
   rows, which is what lets the graph draw "this engine recommends that
   brand because of this page".

Never raises. A dead engine costs its own row, not the scan.
"""

from __future__ import annotations

import logging

from apps.citations.services.url_normalizer import normalize_url

logger = logging.getLogger("apps")


STATUS_OK = "ok"
STATUS_NOT_CONFIGURED = "not_configured"
STATUS_FAILED = "failed"
# The error of an engine left unasked because the account's prompt runs are used up.
PROMPT_RUNS_USED = "This month's free prompt runs are used up."

# Friendly names for the graph. Keys are the PROVIDERS registry keys.
ENGINE_LABELS = {
    "claude": "Claude",
    "gpt4": "ChatGPT",
    "gemini": "Gemini",
    "perplexity": "Perplexity",
    "grok": "Grok",
    "google_ai_overview": "Google AI Overview",
}

# Framed as a real customer asking, not as an analyst asking about brands.
# The point is to capture what an engine actually tells a buyer, so the
# prompt must not prime it to produce a tidy brand list it would not
# otherwise volunteer.
PROMPT_TEMPLATE = (
    "{query}\n\n"
    "Answer as you would for someone genuinely deciding what to use or buy. "
    "Name the specific products, services or companies you would actually "
    "recommend, and say briefly why for each. If you have sources, cite them."
)


def build_prompt(query: str) -> str:
    return PROMPT_TEMPLATE.format(query=(query or "").strip())


class _CitationView:
    """Adapter presenting a ProviderResult to the citation extractors.

    The extractors in ``services/extractors/`` read ``.response_text`` and
    ``.citations`` off an LLMRankingResult. Both are plain getattr lookups,
    so this two-field stand-in reuses them exactly rather than duplicating
    URL parsing here.
    """

    def __init__(self, text: str, citations: list):
        self.response_text = text or ""
        self.citations = citations or []


def extract_citations(text: str, native_citations: list) -> list[dict]:
    """Return ``[{url, domain, title}]`` for what an engine cited.

    Native citations first (a web-grounded model reports these out of band
    and they are exact), then a regex pass over the prose to catch URLs the
    model wrote inline but left off the structured list.
    """
    from apps.citations.services.extractors.perplexity import PerplexityNativeExtractor
    from apps.citations.services.extractors.regex import RegexExtractor

    view = _CitationView(text, native_citations)
    out: list[dict] = []
    seen: set[str] = set()
    for extractor in (PerplexityNativeExtractor(), RegexExtractor()):
        try:
            candidates = extractor.extract(view)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("engine_probe: %s extractor failed: %s", extractor.name, exc)
            continue
        for candidate in candidates:
            normalized, host, _ = normalize_url(candidate.url)
            if not normalized or not host or normalized in seen:
                continue
            seen.add(normalized)
            out.append({
                "url": candidate.url[:2000],
                "domain": (host[4:] if host.startswith("www.") else host)[:300],
                "title": (getattr(candidate, "title", "") or "")[:500],
            })
    return out


def probe_engine(name, provider_cls, *, query, target_brand, website, user) -> dict:
    """Ask one engine and read its answer. Never raises."""
    row = {
        "provider": name,
        "model": "",
        "status": STATUS_FAILED,
        "answer_text": "",
        "brands": [],
        "citations": [],
        "error": "",
    }
    try:
        provider = provider_cls()
        row["model"] = getattr(provider, "model", "") or ""
        if not provider.is_configured():
            row["status"] = STATUS_NOT_CONFIGURED
            row["error"] = "no API key configured"
            return row

        from apps.metering.services import prompt_runs

        # One prompt run; with none left the engine is not asked.
        result = prompt_runs.guard(
            lambda: provider.query(
                build_prompt(query),
                user=user,
                website=website,
                module="brand_research",
                role="engine_probe",
                extra_metadata={"actor": "system"},
            ),
            user=user, website=website, engine=name, feature="brand_research",
        )
        if prompt_runs.is_denial(result.error):
            row["error"] = PROMPT_RUNS_USED
            return row
        if not result.succeeded or not (result.text or "").strip():
            row["error"] = (result.error or "empty response")[:300]
            return row

        row["status"] = STATUS_OK
        row["answer_text"] = result.text
        row["citations"] = extract_citations(result.text, list(result.citations or []))
    except Exception as exc:
        logger.warning("engine_probe: %s failed: %s", name, exc)
        row["error"] = str(exc)[:300]
        return row

    # Read the answer with the same extractor the web lane uses, so an
    # engine recommendation and a Reddit thread produce comparable rows.
    try:
        from apps.citations.services import source_sentiment
        analysis = source_sentiment.analyze_content(
            row["answer_text"],
            query=query,
            target_brand=target_brand,
            website=website,
            user=user,
        )
        if analysis.get("error"):
            row["error"] = str(analysis["error"])[:300]
        else:
            from apps.citations.services.source_scan import _strip_junk_issues
            row["brands"] = _strip_junk_issues(analysis.get("brands") or [])
    except Exception as exc:
        logger.warning("engine_probe: analysis of %s answer failed: %s", name, exc)
        row["error"] = str(exc)[:300]

    return row


def is_enabled() -> bool:
    from django.conf import settings
    return bool(getattr(settings, "BRAND_RESEARCH_ENGINES_ENABLED", True))


def probe_all(*, query, target_brand, website, user, on_progress=None) -> list[dict]:
    """Ask every engine in the audit registry.

    Runs serially rather than in a thread pool. ``provider.query()`` writes
    the token-usage ledger, so a pool here would mean concurrent ORM writes
    from a Celery worker that production already runs at concurrency 2 --
    the wrong place to introduce connection-pool pressure for a saving of a
    few seconds against an analysis loop that takes minutes.

    ``on_progress(count)`` is called after each engine so the caller can
    report a live count.
    """
    if not is_enabled():
        return []

    from apps.llm_ranking.providers import PROVIDERS

    rows = []
    for name, provider_cls in PROVIDERS.items():
        rows.append(probe_engine(
            name, provider_cls,
            query=query, target_brand=target_brand, website=website, user=user,
        ))
        if on_progress:
            try:
                on_progress(sum(1 for r in rows if r["status"] == STATUS_OK))
            except Exception:  # pragma: no cover - progress must not break the lane
                pass
    return rows


def row_from_ai_overview(overview: dict, *, query, target_brand, website, user) -> dict | None:
    """Turn Google's AI Overview into an engine row.

    It is a model recommending brands to the user's customers, which is
    exactly what this lane tracks -- and it is the one engine answer that
    arrives free with a SERP call we already made.
    """
    text = (overview or {}).get("text") or ""
    references = (overview or {}).get("references") or []
    if not text.strip() and not references:
        return None

    row = {
        "provider": "google_ai_overview",
        "model": "google-ai-overview",
        "status": STATUS_OK,
        "answer_text": text,
        "brands": [],
        "citations": [
            {"url": r.get("url", "")[:2000],
             "domain": r.get("domain", "")[:300],
             "title": (r.get("title") or "")[:500]}
            for r in references if r.get("url")
        ],
        "error": "",
    }
    if not text.strip():
        # References without prose: still worth a node for the citations,
        # but there is nothing to extract brands from.
        return row
    try:
        from apps.citations.services import source_sentiment
        analysis = source_sentiment.analyze_content(
            text, query=query, target_brand=target_brand, website=website, user=user,
        )
        if analysis.get("error"):
            row["error"] = str(analysis["error"])[:300]
        else:
            from apps.citations.services.source_scan import _strip_junk_issues
            row["brands"] = _strip_junk_issues(analysis.get("brands") or [])
    except Exception as exc:
        logger.warning("engine_probe: AI Overview analysis failed: %s", exc)
        row["error"] = str(exc)[:300]
    return row


def link_citations_to_rows(engine_rows: list[dict], source_rows) -> dict[str, list[int]]:
    """Map each engine to the ranks of scan sources it cited.

    This is the cross-link that lets the graph draw an engine back to the
    page that informed it. Matching is on the normalized URL, so tracking
    params and trailing slashes do not split a match.
    """
    by_url: dict[str, int] = {}
    for row in source_rows:
        normalized, _, _ = normalize_url(getattr(row, "url", "") or "")
        if normalized:
            by_url.setdefault(normalized, getattr(row, "rank", 0))

    linked: dict[str, list[int]] = {}
    for engine in engine_rows:
        ranks = []
        for citation in engine.get("citations") or []:
            normalized, _, _ = normalize_url(citation.get("url") or "")
            rank = by_url.get(normalized)
            if rank and rank not in ranks:
                ranks.append(rank)
        if ranks:
            linked[engine["provider"]] = sorted(ranks)
    return linked
