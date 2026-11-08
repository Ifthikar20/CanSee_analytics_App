"""Sentiment (contract 3c): how the engines describe the brand when they
name it.

``tone`` counts the sentiment label the existing extraction stored with
each answer that names you, so it needs no analysis. Themes come from the
current-version answer analyses of the window's answers; a value that was
not measured (a previous window without analyzed answers, an engine with
no analyzed answer) is null, never 0.
"""

from __future__ import annotations

from apps.citations.services.url_analytics import model_key
from apps.insights.models import AnswerAnalysis, AnswerTheme
from apps.insights.services import answer_analysis as aa
from apps.insights.services import runs
from apps.llm_ranking.services import answer_window

TONES = ("positive", "neutral", "negative")
MAX_THEMES = 30
MAX_QUOTES = 3


def _done_ids(results: list, status: dict) -> list:
    return [r.id for r in results if status.get(r.id) == AnswerAnalysis.STATUS_DONE]


def _themes(result_ids: list) -> list:
    rows = []
    for offset in range(0, len(result_ids), 1000):
        rows.extend(
            AnswerTheme.objects.filter(
                analysis__result_id__in=result_ids[offset:offset + 1000],
                analysis__version=aa.ANALYSIS_VERSION,
                analysis__status=AnswerAnalysis.STATUS_DONE,
            ).select_related("analysis__result").only(
                "label", "label_key", "tone", "quote", "engine", "answered_at", "analysis_id",
                "prompt_id", "analysis__result__public_id",
            )
        )
    return rows


def _group(rows: list) -> dict:
    groups: dict = {}
    for row in rows:
        group = groups.setdefault((row.label_key, row.tone), {
            "rows": [], "analyses": set(), "engines": set(), "prompts": set(),
        })
        group["rows"].append(row)
        group["analyses"].add(row.analysis_id)
        group["engines"].add(row.engine)
        if row.prompt_id:
            group["prompts"].add(row.prompt_id)
    return groups


def build(website, *, period="30d", interval=None, engine=None, prompt_id=None) -> dict:
    """The Sentiment payload (contract 3c); ``prompt_id`` must be a tracked
    prompt of this project (404 otherwise)."""
    period, interval, engine, prompt_uuid = answer_window.parse_filters(
        period=period, interval=interval, engine=engine, prompt_id=prompt_id,
    )
    brand_prompts = aa.tracked_prompts(website)
    selected = aa.selected_prompt(brand_prompts, prompt_uuid)
    window, current, previous = aa.window_answers(
        website, brand_prompts, period=period, interval=interval, engine=engine,
        prompt_bp=selected,
    )
    n = len(window.buckets)
    configured = aa.engine_configured()
    shown = aa.shown_engines(configured, current, engine)

    named = [(bp, r, i) for bp, r, i in current if r.is_mentioned]
    named_prev = [r for _bp, r in previous if r.is_mentioned]
    status = aa.analysis_status([r.id for _bp, r, _i in named] + [r.id for r in named_prev])
    named_results = [r for _bp, r, _i in named]

    tone = {t: 0 for t in TONES}
    series = {t: [0] * n for t in TONES}
    for _bp, result, index in named:
        if result.sentiment in tone:
            tone[result.sentiment] += 1
            series[result.sentiment][index] += 1

    done_now = _done_ids(named_results, status)
    done_prev = _done_ids(named_prev, status)
    rows = _themes(done_now)
    groups = _group(rows)
    prev_groups = _group(_themes(done_prev)) if done_prev else None

    ranked = []
    for (key, tone_key), group in groups.items():
        newest = sorted(group["rows"], key=lambda r: r.answered_at, reverse=True)
        prev = None
        if prev_groups is not None:
            prev = len(prev_groups.get((key, tone_key), {}).get("analyses", ()))
        ranked.append((len(group["analyses"]), newest[0].answered_at, newest[0].label, {
            "key": f"{key}|{tone_key}",
            "label": newest[0].label,
            "tone": tone_key,
            "answers": len(group["analyses"]),
            "prev_answers": prev,
            "engines": [e for e in answer_window.ENGINE_KEYS if e in group["engines"]],
            "prompts": len(group["prompts"]),
            "last_seen_at": runs.iso(newest[0].answered_at),
            "quotes": [
                {"quote": r.quote, "engine": r.engine,
                 "result_id": str(r.analysis.result.public_id),
                 "answered_at": runs.iso(r.answered_at)}
                for r in newest[:MAX_QUOTES]
            ],
        }))
    ranked.sort(key=lambda t: (-t[0], -t[1].timestamp(), t[2]))
    themes = [item[3] for item in ranked[:MAX_THEMES]]

    by_engine = []
    analyzed_by_engine: dict = {}
    for result in named_results:
        if status.get(result.id) == AnswerAnalysis.STATUS_DONE:
            key = model_key(result.provider)
            analyzed_by_engine[key] = analyzed_by_engine.get(key, 0) + 1
    theme_tones: dict = {}
    for row in rows:
        counts = theme_tones.setdefault(row.engine, {t: 0 for t in TONES})
        counts[row.tone] = counts.get(row.tone, 0) + 1
    for key in shown:
        engine_named = [r for r in named_results if model_key(r.provider) == key]
        analyzed = analyzed_by_engine.get(key, 0)
        counts = theme_tones.get(key, {t: 0 for t in TONES})
        by_engine.append({
            "key": key,
            "named": len(engine_named),
            **{t: sum(1 for r in engine_named if r.sentiment == t) for t in TONES},
            "themes_positive": counts["positive"] if analyzed else None,
            "themes_negative": counts["negative"] if analyzed else None,
        })

    in_view = [selected] if selected is not None else brand_prompts
    by_prompt = []
    for bp in in_view:
        prompt_named = [r for b, r, _i in named if b.id == bp.id]
        by_prompt.append({
            "prompt_id": str(bp.prompt_id),
            "text": bp.prompt.text,
            "named": len(prompt_named),
            **{t: sum(1 for r in prompt_named if r.sentiment == t) for t in TONES},
        })

    return {
        "window": aa.window_payload(window),
        "filters": {"engine": engine, "prompt_id": str(prompt_uuid) if prompt_uuid else None},
        "engines": aa.engines_payload(configured),
        "prompts": [{"prompt_id": str(bp.prompt_id), "text": bp.prompt.text} for bp in brand_prompts],
        "empty_reason": None if named else "no_answers",
        "coverage": aa.coverage(named_results, status),
        "tone": {**tone, "series": series},
        "themes": themes,
        "by_engine": by_engine,
        "by_prompt": by_prompt,
        **runs.run_state(website, aa.KEY),
    }

