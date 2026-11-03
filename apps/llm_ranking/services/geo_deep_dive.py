"""GEO deep-dive analytics for the dashboard's per-metric drill-downs.

Builds three graphical views from the user's last PERIOD_DAYS of completed
audits:

- visibility_matrix: every distinct prompt x provider, with mention status
  and rank — the prompt-level heatmap the dashboard renders
- position_by_provider: rank-bucket distribution per LLM
- sentiment_by_provider: positive/neutral/negative split per LLM
- sentiment_themes: optional LLM-clustered themes from positive vs.
  negative quote contexts. The model only labels themes and assigns quote
  numbers; counts are computed from those assignments and a sample quote
  is kept only when it appears verbatim in the input. Cached for
  THEMES_CACHE_TTL seconds since each call costs tokens.

All inputs come from LLMRankingResult rows already persisted by the audit
pipeline — no new data collection.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re

from django.conf import settings
from django.core.cache import cache
from django.db.models import Count
from django.utils import timezone

from apps.llm_ranking.models import LLMRankingResult

logger = logging.getLogger("apps")

PERIOD_DAYS = 30
MAX_PROMPTS = 30
MAX_AVAILABLE_PROMPTS = 200
MIN_QUOTES_FOR_THEMES = 5
MAX_QUOTES_PER_CALL = 50
THEMES_CACHE_TTL = 60 * 60  # 1 hour

POSITION_BUCKETS = ("1", "2-3", "4-10", "11+")
SENTIMENTS = ("positive", "neutral", "negative")


def build_for_user(
    user,
    *,
    start=None,
    end=None,
    prompts: list[str] | None = None,
    providers: list[str] | None = None,
    tags: list[str] | None = None,
    topics: list[str] | None = None,
    website=None,
) -> dict | None:
    """Return the deep-dive payload for a user, or None if no data.

    ``website`` scopes the matrix, breakdowns, themes and the prompt
    dropdown to one project's audits.
    """
    from apps.llm_ranking.services.geo_stats import _audits_in, _make_flt

    flt = _make_flt(prompts=prompts, providers=providers, tags=tags, topics=topics)
    end = end or timezone.now()
    qs = _audits_in(user, start, end, website=website)
    audit_ids = list(qs.order_by("-completed_at").values_list("id", flat=True))
    if not audit_ids:
        return None
    return {
        "visibility_matrix": _visibility_matrix(audit_ids, flt),
        "position_by_provider": _position_by_provider(audit_ids, flt),
        "sentiment_by_provider": _sentiment_by_provider(audit_ids, flt),
        "sentiment_themes": _sentiment_themes(audit_ids, flt),
        "available_prompts": _available_prompts(user, website=website),
    }


def _filter_results(audit_ids: list, flt: dict | None):
    from apps.llm_ranking.services._window import apply_result_filters

    qs = LLMRankingResult.objects.filter(
        audit_id__in=audit_ids, query_succeeded=True,
    )
    return apply_result_filters(qs, **(flt or {}))


def _available_prompts(user, website=None) -> list[dict]:
    """Distinct prompts audited in the user's reachable projects."""
    from apps.llm_ranking.services.geo_stats import _audits_in

    user_audit_ids = _audits_in(user, None, None, website=website).values_list(
        "id", flat=True,
    )
    rows = (
        LLMRankingResult.objects.filter(audit_id__in=user_audit_ids)
        .values("prompt")
        .annotate(count=Count("id"))
        .order_by("-count")[:MAX_AVAILABLE_PROMPTS]
    )
    return [
        {"value": r["prompt"], "label": r["prompt"], "count": r["count"]}
        for r in rows
    ]


# ── visibility ─────────────────────────────────────────────────────────────

def _visibility_matrix(audit_ids: list, flt: dict | None = None) -> dict:
    """One row per distinct prompt, one column per provider seen.

    When the same (prompt, provider) appears in multiple audits the most
    recent audit wins, since audit_ids is ordered by completed_at desc.
    """
    audit_rank = {aid: i for i, aid in enumerate(audit_ids)}
    rows = _filter_results(audit_ids, flt).values(
        "audit_id", "prompt", "provider", "is_mentioned", "mention_rank",
    )
    rows = sorted(rows, key=lambda r: audit_rank[r["audit_id"]])

    prompts: dict[str, dict] = {}
    providers: set[str] = set()
    for r in rows:
        providers.add(r["provider"])
        entry = prompts.setdefault(r["prompt"], {"cells": {}})
        if r["provider"] not in entry["cells"]:
            entry["cells"][r["provider"]] = {
                "is_mentioned": r["is_mentioned"],
                "mention_rank": r["mention_rank"],
            }

    providers_ordered = sorted(providers)
    prompt_list = []
    for text, entry in list(prompts.items())[:MAX_PROMPTS]:
        prompt_list.append({
            "prompt": text,
            "cells": [entry["cells"].get(p) for p in providers_ordered],
        })
    return {"providers": providers_ordered, "prompts": prompt_list}


# ── position ───────────────────────────────────────────────────────────────

def _position_by_provider(audit_ids: list, flt: dict | None = None) -> list[dict]:
    rows = (
        _filter_results(audit_ids, flt)
        .filter(is_mentioned=True, mention_rank__isnull=False)
        .values("provider", "mention_rank")
    )

    by_provider: dict[str, dict[str, int]] = {}
    for r in rows:
        rank = r["mention_rank"]
        bucket = (
            "1" if rank == 1
            else "2-3" if rank <= 3
            else "4-10" if rank <= 10
            else "11+"
        )
        buckets = by_provider.setdefault(
            r["provider"], {b: 0 for b in POSITION_BUCKETS},
        )
        buckets[bucket] += 1

    out = []
    for provider, buckets in by_provider.items():
        total = sum(buckets.values())
        out.append({
            "provider": provider,
            "total": total,
            "buckets": [
                {
                    "range": b,
                    "count": buckets[b],
                    "pct": round(100.0 * buckets[b] / total, 1) if total else 0.0,
                }
                for b in POSITION_BUCKETS
            ],
        })
    out.sort(key=lambda x: x["provider"])
    return out


# ── sentiment ──────────────────────────────────────────────────────────────

def _sentiment_by_provider(audit_ids: list, flt: dict | None = None) -> list[dict]:
    rows = (
        _filter_results(audit_ids, flt)
        .filter(is_mentioned=True)
        .values("provider", "sentiment")
    )

    by_provider: dict[str, dict[str, int]] = {}
    for r in rows:
        counts = by_provider.setdefault(
            r["provider"], {s: 0 for s in SENTIMENTS},
        )
        if r["sentiment"] in counts:
            counts[r["sentiment"]] += 1

    out = []
    for provider, counts in by_provider.items():
        total = sum(counts.values())
        out.append({
            "provider": provider,
            "total": total,
            "split": [
                {
                    "sentiment": s,
                    "count": counts[s],
                    "pct": round(100.0 * counts[s] / total, 1) if total else 0.0,
                }
                for s in SENTIMENTS
            ],
        })
    out.sort(key=lambda x: x["provider"])
    return out


# ── sentiment themes (LLM-clustered, cached) ───────────────────────────────

def _sentiment_themes(audit_ids: list, flt: dict | None = None) -> dict:
    """Cluster positive and negative quote contexts into named themes.

    Returns {"positive": [...], "negative": [...]}. Each list is at most
    three themes shaped {label, count, sample_quote}; ``count`` is the
    number of input quotes assigned to the theme. Empty if the feature
    is disabled, the provider isn't configured, or there are too few
    quotes to cluster meaningfully.
    """
    if not settings.GEO_SENTIMENT_THEMES_ENABLED:
        return {"positive": [], "negative": []}

    cache_key = _themes_cache_key(audit_ids, flt)
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    out = {
        "positive": _cluster_one(audit_ids, flt, LLMRankingResult.SENTIMENT_POSITIVE),
        "negative": _cluster_one(audit_ids, flt, LLMRankingResult.SENTIMENT_NEGATIVE),
    }
    cache.set(cache_key, out, THEMES_CACHE_TTL)
    return out


def _cluster_one(audit_ids: list, flt: dict | None, sentiment_label: str) -> list[dict]:
    qs = (
        _filter_results(audit_ids, flt)
        .filter(is_mentioned=True, sentiment=sentiment_label)
        .exclude(mention_context="")
    )
    raw_quotes = qs.order_by("-created_at").values_list(
        "mention_context", flat=True,
    )[:MAX_QUOTES_PER_CALL]
    quotes = [q.strip()[:280] for q in raw_quotes if q.strip()]
    if len(quotes) < MIN_QUOTES_FOR_THEMES:
        return []

    try:
        from apps.llm_ranking.providers import get_provider
        provider = get_provider("claude")
    except Exception:
        return []
    if provider is None or not provider.is_configured():
        return []

    numbered = "\n".join(f"{i}. {q}" for i, q in enumerate(quotes, start=1))
    user_prompt = (
        f"Group the following numbered {sentiment_label} brand mentions into "
        "at most 3 distinct themes. Return ONLY a JSON array (no preamble), "
        "where each element has: label (short noun phrase, max 6 words), "
        "quotes (array of the numbers of the quotes that belong to this "
        "theme), sample (one quote copied exactly from the list). Quotes:\n"
        f"{numbered}"
    )
    result = provider.query(
        user_prompt,
        system_prompt="You output only valid JSON. No prose, no markdown.",
        role="enrichment",
    )
    if not result.succeeded:
        logger.info("theme clustering failed (%s): %s", sentiment_label, result.error[:120])
        return []
    return _parse_themes(result.text, quotes)


def _collapse_ws(text: str) -> str:
    return " ".join((text or "").split())


def _parse_themes(text: str, quotes: list[str]) -> list[dict]:
    """Turn the model's theme assignments into computed themes.

    ``count`` is the number of distinct valid quote numbers assigned to the
    theme; themes with none are dropped. ``sample_quote`` is the model's
    sample when it appears verbatim in one of the input quotes, otherwise
    the first assigned quote.
    """
    match = re.search(r"\[.*\]", text or "", re.DOTALL)
    if not match:
        return []
    try:
        data = json.loads(match.group(0))
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    normalised = [_collapse_ws(q) for q in quotes]
    cleaned: list[dict] = []
    for entry in data[:3]:
        if not isinstance(entry, dict):
            continue
        label = str(entry.get("label", "")).strip()[:80]
        if not label:
            continue
        indexes: list[int] = []
        for raw in entry.get("quotes") or []:
            try:
                number = int(raw)
            except (TypeError, ValueError):
                continue
            if 1 <= number <= len(quotes) and number - 1 not in indexes:
                indexes.append(number - 1)
        if not indexes:
            continue
        sample = _collapse_ws(str(entry.get("sample", "")))
        if not sample or not any(sample in quote for quote in normalised):
            sample = normalised[indexes[0]]
        cleaned.append({"label": label, "count": len(indexes), "sample_quote": sample[:280]})
    return cleaned


def _themes_cache_key(audit_ids: list, flt: dict | None = None) -> str:
    audit_part = ",".join(str(a) for a in sorted(audit_ids))
    # Every active filter participates in the key so a filtered view never
    # serves another filter combination's cached clusters.
    parts = []
    for name in ("prompts", "providers", "tags", "topics"):
        values = (flt or {}).get(name)
        if values:
            parts.append(name + "=" + "|".join(sorted(values)))
    prompt_part = "&".join(parts)
    digest = hashlib.sha1(f"{audit_part}::{prompt_part}".encode()).hexdigest()[:16]
    return f"geo:deep_dive:themes:{digest}"
