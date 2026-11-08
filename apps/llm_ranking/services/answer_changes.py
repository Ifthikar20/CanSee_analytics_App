"""What changed since each tracked prompt's previous engine check.

For every tracked prompt and engine, the newest successful answer of the
window is compared with the one before it: whether the engine names the
brand, and at what rank. Answers are matched to prompts the way the
visibility dashboard matches them (answer_window.tracked_answers), so both
agree on what an answer counts for. A prompt and engine with one answer is
a first check, never a change; nothing is estimated.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import timedelta

from django.utils import timezone

from apps.citations.services.url_analytics import model_key
from apps.llm_ranking.services.answer_window import ENGINE_ORDER, tracked_answers
from apps.prompt_library.services.prompt_stats import MODEL_LABELS

WINDOW_DAYS = 90
NAMED_NOW, DROPPED, RANK_UP, RANK_DOWN = "named_now", "dropped", "rank_up", "rank_down"
UNCHANGED, FIRST_CHECK = "unchanged", "first_check"
# Gains first, then losses, then the rest (the order a change log lists them).
CHANGE_ORDER = (NAMED_NOW, RANK_UP, DROPPED, RANK_DOWN, FIRST_CHECK, UNCHANGED)


def classify(latest, previous) -> str:
    if previous is None:
        return FIRST_CHECK
    if latest.is_mentioned and not previous.is_mentioned:
        return NAMED_NOW
    if previous.is_mentioned and not latest.is_mentioned:
        return DROPPED
    if latest.is_mentioned and latest.mention_rank and previous.mention_rank:
        if latest.mention_rank < previous.mention_rank:
            return RANK_UP
        if latest.mention_rank > previous.mention_rank:
            return RANK_DOWN
    return UNCHANGED


def _answer(result) -> dict:
    return {
        "answered_at": result.created_at.isoformat(),
        "named": bool(result.is_mentioned),
        "rank": result.mention_rank if result.is_mentioned else None,
    }


def answer_changes(website, *, days: int = WINDOW_DAYS, now=None) -> dict:
    """``{window_days, rows, counts, latest_check_at, previous_check_at}``.

    ``rows`` holds one entry per tracked prompt and engine with an answer
    in the window, in CHANGE_ORDER, then prompt and engine order.
    ``latest_check_at`` is the newest answer compared, ``previous_check_at``
    the newest answer it was compared with (None when nothing was).
    """
    from apps.prompt_library.models import BrandPrompt

    now = now or timezone.now()
    brand_prompts = list(
        BrandPrompt.objects.filter(website=website, is_archived=False)
        .select_related("prompt").order_by("created_at")
    )
    grouped: dict[tuple, list] = defaultdict(list)
    for bp, result in tracked_answers(website, brand_prompts, since=now - timedelta(days=days)):
        grouped[(bp.id, result.provider)].append(result)
    order = {bp.id: i for i, bp in enumerate(brand_prompts)}
    by_id = {bp.id: bp for bp in brand_prompts}
    rows = []
    for (bp_id, provider), results in grouped.items():
        results.sort(key=lambda r: r.created_at, reverse=True)
        latest = results[0]
        previous = results[1] if len(results) > 1 else None
        bp = by_id[bp_id]
        rows.append({
            "prompt_id": str(bp.prompt_id),
            "brand_prompt_id": str(bp.id),
            "prompt": bp.prompt.text,
            "engine": model_key(provider),
            "label": MODEL_LABELS.get(provider, provider),
            "change": classify(latest, previous),
            "latest": _answer(latest),
            "previous": _answer(previous) if previous is not None else None,
            "_sort": (order[bp_id], ENGINE_ORDER.index(provider)),
        })
    rows.sort(key=lambda r: (CHANGE_ORDER.index(r["change"]), r["_sort"]))
    for row in rows:
        del row["_sort"]
    compared = [r for r in rows if r["previous"] is not None]
    return {
        "window_days": days,
        "rows": rows,
        "counts": {kind: sum(1 for r in rows if r["change"] == kind) for kind in CHANGE_ORDER},
        "latest_check_at": max((r["latest"]["answered_at"] for r in rows), default=None),
        "previous_check_at": max((r["previous"]["answered_at"] for r in compared), default=None),
    }
