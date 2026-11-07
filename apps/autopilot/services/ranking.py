"""Prompt ranking (contract 3c, backend 4f): the project's tracked prompts,
ranked by how often AI names the brand over the last 7 UTC days.

One service, read by the board's ranking source and by the daily text, so
both show the same numbers. Answers are every successful tracked answer of
the window (``answer_window.tracked_answers``: Autopilot's and manual ones
alike); a prompt is ranked once it has AUTOPILOT_MIN_RANK_ANSWERS of them.
A share is null with no answer (Wilson's bounds at n = 0 are not a
measurement), and a change is null with fewer than the minimum answers
before it.
"""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta

from django.utils import timezone

from apps.autopilot.models import AutopilotRun, AutopilotRunPrompt
from apps.autopilot.services import settings as ap

PAGE_ONE = 10
WINDOW = "7d"


def _pct(value: float) -> float:
    return round(100 * value, 1)


def _share(tally) -> tuple:
    """(share, low, high) in percent, None with no answer."""
    from apps.llm_ranking.services.ranking_service import wilson_ci

    if not tally.answers:
        return None, None, None
    low, high = wilson_ci(tally.mentioned, tally.answers)
    return _pct(tally.mentioned / tally.answers), _pct(low), _pct(high)


def _prev_share(prev, minimum: int) -> float | None:
    if prev.answers < minimum:
        return None
    return _pct(prev.mentioned / prev.answers)


def _change(share, prev_share) -> float | None:
    if share is None or prev_share is None:
        return None
    return round(share - prev_share, 1)


def google_cell(check) -> dict:
    """overview's Google cell, plus ``page_one`` (null when not measured)."""
    from apps.search_gap.services.overview import _google

    cell = dict(_google(check))
    page_one = None
    if cell["status"] == "ok":
        position = cell.get("position")
        if position is not None:
            page_one = position <= PAGE_ONE
        elif (cell.get("depth") or 0) >= PAGE_ONE:
            page_one = False
    cell["page_one"] = page_one
    return cell


def _engine_keys(seen: set) -> list[str]:
    """Configured engines and any engine with answers, in display order."""
    from apps.agent.tools.research import engine_catalog
    from apps.llm_ranking.services.answer_window import ENGINE_KEYS

    configured = {e["key"] for e in engine_catalog() if e["configured"]}
    return [key for key in ENGINE_KEYS if key in configured or key in seen]


def _autopilot_block(website) -> dict:
    from apps.autopilot.services import pipeline

    autopilot = ap.get_or_build(website)
    last = (
        AutopilotRun.objects.filter(website=website, status__in=AutopilotRun.TERMINAL)
        .select_related("analysis_run").order_by("-created_at").first()
    )
    return {
        "enabled": bool(ap.is_saved(autopilot) and autopilot.enabled),
        "last_run": pipeline.run_summary(last) if last is not None else None,
        "next_run_at": ap.iso(ap.next_run_at(autopilot, website)),
    }


def rank_prompts(website, *, today=None, run=None, with_autopilot: bool = True) -> dict:
    """The ranking payload (contract 3c). With ``run``, only the prompts
    that run asked are listed (the text's top and weakest)."""
    from apps.billing.services import tracked_prompts
    from apps.citations.services.url_analytics import model_key
    from apps.llm_ranking.services import answer_window
    from apps.prompt_library.models import BrandPrompt
    from apps.search_gap.services.overview import _latest_checks

    minimum = ap.min_rank_answers()
    today = today or timezone.now().astimezone(UTC).date()
    window = answer_window.Window(WINDOW, "day", today)
    end_at = datetime.combine(today + timedelta(days=1), time.min, tzinfo=UTC)
    brand_prompts = list(
        BrandPrompt.objects.filter(website=website, is_archived=False)
        .select_related("prompt").order_by("created_at", "id")
    )
    scope = brand_prompts
    if run is not None:
        asked = set(
            AutopilotRunPrompt.objects.filter(
                run=run,
                status__in=(AutopilotRunPrompt.STATUS_DONE, AutopilotRunPrompt.STATUS_FAILED),
            ).values_list("brand_prompt_id", flat=True)
        )
        scope = [bp for bp in brand_prompts if bp.id in asked]

    current = {bp.id: answer_window.Tally() for bp in brand_prompts}
    previous = {bp.id: answer_window.Tally() for bp in brand_prompts}
    engines = {bp.id: {} for bp in brand_prompts}
    seen: set = set()
    # Over every tracked prompt, so a result matched to two prompts stays
    # with the first, as on the board.
    for bp, result in answer_window.tracked_answers(
        website, brand_prompts, since=window.prev_start_at,
    ):
        if window.start_at <= result.created_at < end_at:
            current[bp.id].add(result)
            key = model_key(result.provider)
            seen.add(key)
            engines[bp.id].setdefault(key, answer_window.Tally()).add(result)
        elif result.created_at < window.start_at:
            previous[bp.id].add(result)

    checks = _latest_checks(website, scope) if scope else {}
    keys = _engine_keys(seen)
    rows = []
    for order, bp in enumerate(scope):
        tally, before = current[bp.id], previous[bp.id]
        share, low, high = _share(tally)
        prev_share = _prev_share(before, minimum)
        rows.append({
            "prompt_id": str(bp.prompt_id),
            "brand_prompt_id": str(bp.id),
            "text": bp.prompt.text,
            "rank": None,
            "ranked": tally.answers >= minimum,
            "answers": tally.answers,
            "named": tally.mentioned,
            "share_pct": share,
            "share_low_pct": low,
            "share_high_pct": high,
            "prev_share_pct": prev_share,
            "change_pts": _change(share, prev_share),
            "avg_rank": tally.avg_rank,
            "engines": {
                key: {
                    "answers": engines[bp.id][key].answers if key in engines[bp.id] else 0,
                    "named": engines[bp.id][key].mentioned if key in engines[bp.id] else 0,
                }
                for key in keys
            },
            "google": google_cell(checks.get(bp.id)),
            "_order": order,
        })
    ranked = sorted(
        (r for r in rows if r["ranked"]),
        key=lambda r: (
            -r["share_pct"], -r["answers"],
            r["avg_rank"] if r["avg_rank"] is not None else float("inf"), r["_order"],
        ),
    )
    for position, row in enumerate(ranked, start=1):
        row["rank"] = position
    unranked = [r for r in rows if not r["ranked"]]
    for row in rows:
        row.pop("_order")

    head, head_before = answer_window.Tally(), answer_window.Tally()
    for bp in scope:
        for name in ("answers", "mentioned"):
            setattr(head, name, getattr(head, name) + getattr(current[bp.id], name))
            setattr(head_before, name, getattr(head_before, name) + getattr(previous[bp.id], name))
    share = round(100 * head.mentioned / head.answers, 1) if head.answers else None
    prev_share = _prev_share(head_before, minimum)

    owner = website.user
    capacity = tracked_prompts.capacity(owner)
    _runnable, over = tracked_prompts.runnable_brand_prompts(website)
    out = {
        "window": {"days": 7, "start": window.start.isoformat(), "end": window.end.isoformat()},
        "min_answers": minimum,
        "tracked": {"count": capacity["used"], "limit": capacity["limit"], "over_limit": over},
    }
    if with_autopilot:
        out["autopilot"] = _autopilot_block(website)
    out["headline"] = {
        "answers": head.answers,
        "named": head.mentioned,
        "share_pct": share,
        "prev_share_pct": prev_share,
        "change_pts": _change(share, prev_share),
    }
    out["prompts"] = ranked + unranked
    return out
