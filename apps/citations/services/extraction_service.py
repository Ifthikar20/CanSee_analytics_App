"""Citation extraction orchestrator.

Pulls the appropriate extractor(s) for the result's provider, dedupes by
normalised URL, classifies each domain, and persists Citation rows.
Idempotent: re-running on the same result is a no-op (the unique
constraint on ``(result, normalized_url)`` collapses repeat inserts).
"""
from __future__ import annotations

import logging
import re
from collections.abc import Iterable

from apps.citations.models import (
    Citation,
    CitationExtractionMethod,
    SourceClass,
)
from apps.citations.services.domain_classifier import (
    classify,
    collect_competitor_apexes,
)
from apps.citations.services.extractors import (
    CitationCandidate,
    GeminiGroundingExtractor,
    PerplexityNativeExtractor,
    RegexExtractor,
)
from apps.citations.services.url_normalizer import normalize_url

logger = logging.getLogger("apps")


def _strategy_for_provider(provider: str) -> list:
    """Return the ordered list of extractor instances for ``provider``."""
    p = (provider or "").lower()
    # PerplexityNativeExtractor is provider-agnostic despite the name: it
    # reads LLMRankingResult.citations, which every web-search-enabled
    # provider now fills (Perplexity's native list, Claude's web-search
    # citations, OpenAI/grok Responses url_citation annotations).
    if p in ("perplexity", "claude", "gpt4", "grok"):
        return [PerplexityNativeExtractor(), RegexExtractor()]
    if p == "gemini":
        return [GeminiGroundingExtractor(), RegexExtractor()]
    # Every other provider: URLs the model wrote into the answer.
    return [RegexExtractor()]


def _dedupe_candidates(candidates: Iterable[CitationCandidate]) -> list[CitationCandidate]:
    """Collapse candidates by normalised URL, keeping the highest-confidence one."""
    by_url: dict[str, CitationCandidate] = {}
    for cand in candidates:
        if not cand.url:
            continue
        normalised, _, _ = normalize_url(cand.url)
        if not normalised:
            continue
        existing = by_url.get(normalised)
        if existing is None or cand.confidence > existing.confidence:
            by_url[normalised] = cand
    return list(by_url.values())


def count_references(text: str, *, url: str, method: str, position: int | None) -> int:
    """Count how many times a retrieved source is explicitly cited in ``text``.

    Two complementary signals (a retrieval is only a *citation* when one of
    these fires; otherwise the page was merely a candidate source):

    * Native / grounding sources are referenced by numbered markers. The
      source at 0-based ``position`` is cited as ``[position + 1]``.
    * Regex hits are URLs the model wrote into the prose, so the literal URL
      occurrence count is the reference count.
    """
    if not text:
        return 0
    count = 0
    if method in ("native", "grounding") and position is not None:
        count += len(re.findall(re.escape(f"[{position + 1}]"), text))
    if url:
        count += text.count(url)
    return count


def extract_for_result(result_id: str) -> int:
    """Run the extraction strategy for one LLMRankingResult.

    Returns the number of Citation rows that exist for the result *after*
    extraction completes (not strictly the number created — re-running on
    a result that already has citations returns the same count).
    """
    from apps.llm_ranking.models import LLMRankingResult

    try:
        result = LLMRankingResult.objects.select_related("audit", "audit__website").get(
            id=result_id,
        )
    except LLMRankingResult.DoesNotExist:
        logger.warning("extract_for_result: result %s not found", result_id)
        return 0

    audit = result.audit
    website = getattr(audit, "website", None)
    competitor_apexes = collect_competitor_apexes(website) if website else set()

    extractors = _strategy_for_provider(result.provider)
    candidates: list[CitationCandidate] = []
    for extractor in extractors:
        try:
            found = extractor.extract(result) or []
        except Exception as exc:
            logger.warning(
                "extractor %s failed for result %s: %s", extractor.name, result_id, exc
            )
            continue
        candidates.extend(found)

    candidates = _dedupe_candidates(candidates)
    if not candidates:
        return 0

    method_choices = {m.value for m in CitationExtractionMethod}

    created = 0
    for position, cand in enumerate(candidates):
        normalised, host, apex = normalize_url(cand.url)
        if not normalised:
            continue
        try:
            source_class = classify(
                apex,
                host=host,
                website=website,
                competitor_apexes=competitor_apexes,
            )
        except Exception as exc:
            logger.warning("classify failed for %s: %s", apex, exc)
            source_class = SourceClass.OTHER

        method = cand.extraction_method if cand.extraction_method in method_choices else "regex"
        pos = cand.position if cand.position is not None else position
        references = count_references(
            result.response_text or "", url=cand.url, method=method, position=pos,
        )

        _, was_created = Citation.objects.update_or_create(
            result=result,
            normalized_url=normalised,
            defaults={
                "audit": audit,
                "url": cand.url[:2000],
                "domain": host[:300],
                "apex_domain": apex[:300],
                "source_class": source_class,
                "extraction_method": method,
                "confidence": float(cand.confidence),
                "position": pos,
                "reference_count": references,
                "title": (cand.title or "")[:500],
                "snippet": cand.snippet or "",
                "is_target": source_class == SourceClass.YOUR_SITE,
                "is_competitor": source_class == SourceClass.COMPETITOR_SITE,
            },
        )
        if was_created:
            created += 1

    return created
