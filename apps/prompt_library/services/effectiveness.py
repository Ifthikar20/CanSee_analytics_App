"""Compute per-Prompt effectiveness scores from past audit signals.

Effectiveness rolls four normalised axes into a 0-1 weighted overall:

  * visibility_hit_rate = mentioned_runs / total_runs
  * citation_yield      = clamp(avg_citations_per_run / 3.0, 0, 1)
  * claim_yield         = clamp(avg_claims_per_run / 5.0, 0, 1)
  * coverage_breadth    = distinct_providers_with_mention / 4.0

Overall = 0.40*v + 0.30*c + 0.20*cl + 0.10*b

A prompt needs ``MIN_RUNS_STABLE`` (3) answered result rows before its
score is considered stable; below that we still compute the components
but flag ``stable=False`` so the UI can show an "Untested" badge. Failed
calls are not runs.

Result rows are matched to a Prompt via the
``LLMRankingResult.source_prompt`` foreign key. Older rows that predate
the FK are simply ignored — they would require unreliable text matching
across filled template variants.
"""
from __future__ import annotations

import logging
from collections.abc import Iterable

logger = logging.getLogger("apps")

MIN_RUNS_STABLE = 3
WEIGHTS = {
    "visibility_hit_rate": 0.40,
    "citation_yield": 0.30,
    "claim_yield": 0.20,
    "coverage_breadth": 0.10,
}


def _clamp(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    if value <= lo:
        return lo
    if value >= hi:
        return hi
    return float(value)


def _avg_citations_per_run(results) -> float:
    if not results:
        return 0.0
    total = 0
    for r in results:
        cites = r.citations or []
        if isinstance(cites, list):
            total += len(cites)
    return total / max(len(results), 1)


def _avg_claims_per_run(prompt) -> float:
    """Supported brand claims per analyzed response, from the brand
    alignment detail.

    A response "yields claims" when its brand-scoped statements are
    backed by the brand's own facts/material — the alignment engine
    counts those in ``alignment_detail.support.supported``. Responses
    never analyzed are excluded; a prompt with no analyzed responses
    scores 0.0 (identical to the pre-alignment behavior, so historical
    effectiveness scores only ever improve).
    """
    try:
        from apps.llm_ranking.models import LLMRankingResult

        rows = (
            LLMRankingResult.objects
            .filter(source_prompt=prompt)
            .exclude(alignment_version="")
            .values_list("alignment_detail", flat=True)
        )
        counts = [
            int((d or {}).get("support", {}).get("supported", 0))
            for d in rows
            if isinstance(d, dict)
        ]
        return (sum(counts) / len(counts)) if counts else 0.0
    except Exception as exc:  # pragma: no cover — defensive
        logger.debug("avg_claims_per_run: %s", exc)
        return 0.0


def compute_effectiveness(prompt) -> dict:
    """Return the score envelope for ``prompt``.

    Always safe to call — never raises. Returns ``stable=False`` when
    fewer than :data:`MIN_RUNS_STABLE` runs have used this prompt.
    """
    from apps.llm_ranking.models import LLMRankingResult

    # Only answered queries are runs: a failed call says nothing about
    # whether the prompt surfaces the brand.
    qs = LLMRankingResult.objects.filter(source_prompt=prompt, query_succeeded=True)
    total_runs = qs.count()
    if total_runs == 0:
        return {
            "stable": False,
            "overall": 0.0,
            "runs": 0,
            "components": {
                "visibility_hit_rate": 0.0,
                "citation_yield": 0.0,
                "claim_yield": 0.0,
                "coverage_breadth": 0.0,
            },
        }

    mentioned_runs = qs.filter(is_mentioned=True).count()
    visibility = mentioned_runs / total_runs

    results = list(qs.only("citations", "provider", "is_mentioned"))
    citation_yield = _clamp(_avg_citations_per_run(results) / 3.0)
    claim_yield = _clamp(_avg_claims_per_run(prompt) / 5.0)

    distinct_providers = (
        qs.filter(is_mentioned=True).values("provider").distinct().count()
    )
    breadth = _clamp(distinct_providers / 4.0)

    components = {
        "visibility_hit_rate": round(visibility, 4),
        "citation_yield": round(citation_yield, 4),
        "claim_yield": round(claim_yield, 4),
        "coverage_breadth": round(breadth, 4),
    }
    overall = sum(components[k] * WEIGHTS[k] for k in WEIGHTS)
    return {
        "stable": total_runs >= MIN_RUNS_STABLE,
        "overall": round(overall, 4),
        "runs": total_runs,
        "components": components,
    }


def refresh_prompt(prompt) -> dict:
    """Compute and persist the score for one prompt. Returns the envelope."""
    envelope = compute_effectiveness(prompt)
    prompt.effectiveness_score = envelope["overall"]
    prompt.effectiveness_components = {
        **envelope["components"],
        "stable": envelope["stable"],
        "runs": envelope["runs"],
    }
    prompt.runs_count = envelope["runs"]
    prompt.save(update_fields=[
        "effectiveness_score",
        "effectiveness_components",
        "runs_count",
        "updated_at",
    ])
    return envelope


def refresh_all_effectiveness_scores(prompts: Iterable | None = None) -> int:
    """Iterate active prompts (or the supplied iterable) and refresh scores.

    Returns the number of prompts updated.
    """
    from apps.prompt_library.models import Prompt

    qs = prompts if prompts is not None else Prompt.objects.filter(is_active=True)
    count = 0
    for prompt in qs:
        try:
            refresh_prompt(prompt)
            count += 1
        except Exception as exc:  # pragma: no cover
            logger.warning("refresh_prompt failed for %s: %s", prompt.id, exc)
    return count
