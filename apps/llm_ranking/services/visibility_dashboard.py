"""AI visibility over time for one website (pages contract section 5).

Every number comes from the answers the engines gave to the website's
tracked prompts: ``LLMRankingResult`` rows matched to the prompts through
``prompt_stats.results_by_prompt``, and the ``Citation`` rows read from
those answers. Country-check answers live in their own table, so they
never appear here.

Sparse data stays honest: a bucket without answers has 0 answers and null
rates (a gap in the chart, never 0%), a previous-window number is null
when that window had no answers, and citation numbers are null when the
answers carry no citation rows at all.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime

from django.utils import timezone

from apps.citations.services.url_analytics import model_key
from apps.llm_ranking.services.answer_window import (  # noqa: F401 - names kept for importers
    ENGINE_KEYS,
    ENGINE_ORDER,
    INTERVALS,
    PERIOD_DAYS,
    RESULT_FIELDS,
    parse_filters,
)
from apps.llm_ranking.services.answer_window import Tally as _Tally
from apps.llm_ranking.services.answer_window import Window as _Window
from apps.llm_ranking.services.answer_window import pct as _pct
from apps.llm_ranking.services.answer_window import tracked_answers as _answers
from apps.prompt_library.services.prompt_stats import MODEL_LABELS, is_configured
from apps.search_gap.services.google_rank import is_same_site, site_apex
from core.exceptions import ResourceNotFound

SENTIMENTS = ("positive", "neutral", "negative")
CITATION_KINDS = ("yours", "competitors", "other")
# You plus five other brands; every further brand folds into "other".
MAX_BRANDS = 6
OTHER_BRANDS = "Other brands"
TOP_DOMAINS = 15
CITATION_CHUNK = 1000
# Share-of-voice key for your own brand; never equal to a case-folded name.
_YOU = "\x00you"


def _providers() -> dict:
    from apps.llm_ranking.providers import PROVIDERS

    return {key: PROVIDERS[key] for key in ENGINE_ORDER if key in PROVIDERS}


def _competitor_names(website) -> list[str]:
    names = []
    for c in website.competitors or []:
        name = c.get("name") if isinstance(c, dict) else c
        name = " ".join(str(name or "").split())
        if name:
            names.append(name)
    return names


def _named(result, own_fold: str, display: dict) -> set:
    """The brands one answer names: you when it names you, plus every
    brand it lists, case-folded, minus your own name."""
    names = {_YOU} if result.is_mentioned else set()
    for item in result.competitors_mentioned or []:
        raw = item.get("name") if isinstance(item, dict) else item
        name = " ".join(str(raw or "").split())
        fold = name.casefold()
        if not name or fold == own_fold:
            continue
        display.setdefault(fold, name)
        names.add(fold)
    return names


def _share_of_voice(website, current: list, previous: list, n: int) -> tuple[dict, dict]:
    """(share_of_voice payload, {"now", "prev"} your share) for the window."""
    own_fold = " ".join((website.name or "").split()).casefold()
    display: dict = {}
    rivals_order: list[str] = []
    for name in _competitor_names(website):
        fold = name.casefold()
        if fold != own_fold and fold not in rivals_order:
            rivals_order.append(fold)
            display[fold] = name

    window_counts: Counter = Counter()
    bucket_counts = [Counter() for _ in range(n)]
    for _bp, result, index in current:
        names = _named(result, own_fold, display)
        window_counts.update(names)
        bucket_counts[index].update(names)
    prev_counts: Counter = Counter()
    for result in previous:
        prev_counts.update(_named(result, own_fold, display))

    # You first, then your configured competitors that were named, then
    # every other brand, each group by mentions in the window.
    rivals = sorted((f for f in rivals_order if window_counts[f]), key=lambda f: -window_counts[f])
    others = sorted(
        (f for f in window_counts if f != _YOU and f not in rivals_order),
        key=lambda f: (-window_counts[f], display[f].casefold()),
    )
    chosen = [_YOU, *(rivals + others)[:MAX_BRANDS - 1]]
    folded = [f for f in window_counts if f not in chosen]
    keys = {f: ("you" if f == _YOU else f"b{i}") for i, f in enumerate(chosen)}
    total = sum(window_counts.values())

    brands = [
        {
            "key": keys[f],
            "name": website.name if f == _YOU else display[f],
            "is_you": f == _YOU,
            "is_competitor": f in rivals_order,
            "mentions": window_counts[f],
            "share_pct": _pct(window_counts[f], total),
        }
        for f in chosen
    ]
    series = {
        keys[f]: [_pct(counts[f], sum(counts.values())) for counts in bucket_counts]
        for f in chosen
    }
    other_mentions = sum(window_counts[f] for f in folded)
    if other_mentions:
        brands.append({
            "key": "other", "name": OTHER_BRANDS, "is_you": False, "is_competitor": False,
            "mentions": other_mentions, "share_pct": _pct(other_mentions, total),
        })
        series["other"] = [
            _pct(sum(counts[f] for f in folded), sum(counts.values())) for counts in bucket_counts
        ]
    yours = {
        "now": _pct(window_counts[_YOU], total),
        "prev": _pct(prev_counts[_YOU], sum(prev_counts.values())),
    }
    return {"brands": brands, "series": series}, yours


def _sentiment(current: list, n: int) -> dict:
    totals = dict.fromkeys(SENTIMENTS, 0)
    series = {s: [0] * n for s in SENTIMENTS}
    for _bp, result, index in current:
        if result.is_mentioned and result.sentiment in totals:
            totals[result.sentiment] += 1
            series[result.sentiment][index] += 1
    return {"totals": totals, "series": series}


def _citation_rows(result_ids: list) -> list[dict]:
    from apps.citations.models import Citation

    rows: list[dict] = []
    for offset in range(0, len(result_ids), CITATION_CHUNK):
        rows.extend(
            Citation.objects
            .filter(result_id__in=result_ids[offset:offset + CITATION_CHUNK])
            .values("result_id", "domain", "apex_domain", "is_target", "is_competitor")
        )
    return rows


def _citations(website, current: list, n: int) -> tuple[dict, dict]:
    """(citations payload, per-provider {"total", "yours"} counts)."""
    answer_of = {result.id: (result.provider, index) for _bp, result, index in current}
    rows = _citation_rows(list(answer_of))
    per_engine: dict = {}
    if not rows:
        return {
            "has_data": False,
            "totals": None,
            "series": {kind: [None] * n for kind in CITATION_KINDS},
            "top_domains": [],
        }, per_engine

    apex = site_apex(website.url)
    totals = dict.fromkeys(CITATION_KINDS, 0)
    series = {kind: [0] * n for kind in CITATION_KINDS}
    domains: dict = {}
    for row in rows:
        provider, index = answer_of[row["result_id"]]
        yours = bool(row["is_target"]) or is_same_site(row["domain"], apex)
        kind = "yours" if yours else "competitors" if row["is_competitor"] else "other"
        totals[kind] += 1
        series[kind][index] += 1
        counts = per_engine.setdefault(provider, {"total": 0, "yours": 0})
        counts["total"] += 1
        counts["yours"] += int(yours)

        domain = row["apex_domain"] or row["domain"]
        entry = domains.setdefault(domain, {
            "citations": 0, "answers": set(), "yours": False, "competitor": False,
            "engines": set(),
        })
        entry["citations"] += 1
        entry["answers"].add(row["result_id"])
        entry["yours"] = entry["yours"] or yours
        entry["competitor"] = entry["competitor"] or bool(row["is_competitor"])
        entry["engines"].add(provider)

    top = sorted(
        domains.items(), key=lambda kv: (-kv[1]["citations"], -len(kv[1]["answers"]), kv[0]),
    )[:TOP_DOMAINS]
    return {
        "has_data": True,
        "totals": {"total": len(rows), **totals},
        "series": series,
        "top_domains": [
            {
                "domain": domain,
                "citations": entry["citations"],
                "answers": len(entry["answers"]),
                "is_yours": entry["yours"],
                "is_competitor": entry["competitor"] and not entry["yours"],
                "engines": [model_key(p) for p in ENGINE_ORDER if p in entry["engines"]],
            }
            for domain, entry in top
        ],
    }, per_engine


def _google_history(brand_prompt, since: datetime) -> list[dict]:
    from apps.search_gap.models import GoogleRankCheck

    checks = (
        GoogleRankCheck.objects
        .filter(brand_prompt=brand_prompt, checked_at__gte=since)
        .only("checked_at", "position", "depth", "location", "error")
        .order_by("checked_at")
    )
    return [
        {
            "checked_at": check.checked_at.isoformat(),
            "position": check.position,
            "depth": check.depth,
            "page": check.page,
            "location": check.location,
            "error": check.error or None,
        }
        for check in checks
    ]


def build_dashboard(website, *, period="30d", interval=None, engine=None, prompt_id=None) -> dict:
    """The visibility dashboard payload (contract section 5).

    ``engine`` is a public engine key, ``prompt_id`` a library Prompt id
    tracked by this website (404 otherwise). Both filters apply to every
    number, including the previous window.
    """
    from apps.prompt_library.models import BrandPrompt

    period, interval, engine, prompt_uuid = parse_filters(
        period=period, interval=interval, engine=engine, prompt_id=prompt_id,
    )
    window = _Window(period, interval, timezone.now().date())
    n = len(window.buckets)
    brand_prompts = list(
        BrandPrompt.objects
        .filter(website=website, is_archived=False)
        .select_related("prompt")
        .order_by("created_at")
    )
    selected = None
    if prompt_uuid is not None:
        selected = next((bp for bp in brand_prompts if bp.prompt_id == prompt_uuid), None)
        if selected is None:
            raise ResourceNotFound("Prompt not found.")
    configured = {key: is_configured(cls) for key, cls in _providers().items()}

    current, previous = [], []
    for bp, result in _answers(website, brand_prompts, since=window.prev_start_at):
        if selected is not None and bp.id != selected.id:
            continue
        if engine is not None and model_key(result.provider) != engine:
            continue
        if result.created_at >= window.start_at:
            index = window.bucket(result.created_at)
            if index is not None:
                current.append((bp, result, index))
        else:
            previous.append(result)

    # Engines drawn: the filtered one, else every configured engine and
    # every engine that answered in the window, in display order.
    answered = {result.provider for _bp, result, _i in current}
    if engine is not None:
        shown = [p for p in ENGINE_ORDER if model_key(p) == engine]
    else:
        shown = [p for p in ENGINE_ORDER if configured.get(p) or p in answered]

    overall = [_Tally() for _ in range(n)]
    per_engine = {p: [_Tally() for _ in range(n)] for p in shown}
    window_tally, prev_tally = _Tally(), _Tally()
    engine_tally = {p: _Tally() for p in shown}
    engine_sentiment = {p: dict.fromkeys(SENTIMENTS, 0) for p in shown}
    in_view = [selected] if selected is not None else brand_prompts
    prompt_tally = {bp.id: _Tally() for bp in in_view}
    prompt_engine = {bp.id: {p: _Tally() for p in shown} for bp in in_view}
    for bp, result, index in current:
        window_tally.add(result)
        overall[index].add(result)
        prompt_tally[bp.id].add(result)
        if result.provider in per_engine:
            per_engine[result.provider][index].add(result)
            engine_tally[result.provider].add(result)
            prompt_engine[bp.id][result.provider].add(result)
            if result.is_mentioned and result.sentiment in SENTIMENTS:
                engine_sentiment[result.provider][result.sentiment] += 1
    for result in previous:
        prev_tally.add(result)
    has_prev = prev_tally.answers > 0

    share_of_voice, your_share = _share_of_voice(website, current, previous, n)
    citations, engine_citations = _citations(website, current, n)
    cited = citations["has_data"]

    if not brand_prompts:
        empty_reason = "no_prompts"
    elif window_tally.answers:
        empty_reason = None
    elif not any(configured.values()):
        empty_reason = "no_engines"
    else:
        empty_reason = "no_answers"

    return {
        "website": {"id": str(website.id), "name": website.name},
        "window": {
            "period": period,
            "interval": interval,
            "start": window.start.isoformat(),
            "end": window.end.isoformat(),
            "buckets": [day.isoformat() for day in window.buckets],
        },
        "filters": {"engine": engine, "prompt_id": str(prompt_uuid) if prompt_uuid else None},
        "engines": [
            {"key": model_key(p), "label": MODEL_LABELS.get(p, p),
             "configured": configured.get(p, False)}
            for p in ENGINE_ORDER
        ],
        "prompts": [
            {"prompt_id": str(bp.prompt_id), "text": bp.prompt.text, "location": bp.location}
            for bp in brand_prompts
        ],
        "has_data": window_tally.answers > 0,
        "empty_reason": empty_reason,
        "headline": {
            "answers": window_tally.answers,
            "answers_prev": prev_tally.answers if has_prev else None,
            "visibility_pct": window_tally.visibility_pct,
            "visibility_prev_pct": prev_tally.visibility_pct,
            "avg_rank": window_tally.avg_rank,
            "avg_rank_prev": prev_tally.avg_rank,
            "share_of_voice_pct": your_share["now"],
            "share_of_voice_prev_pct": your_share["prev"] if has_prev else None,
            "citations": citations["totals"]["total"] if cited else None,
            "your_citations": citations["totals"]["yours"] if cited else None,
        },
        "visibility": {
            "by_engine": [
                {
                    "key": model_key(p),
                    "visibility_pct": [t.visibility_pct for t in per_engine[p]],
                    "answers": [t.answers for t in per_engine[p]],
                    "avg_rank": [t.avg_rank for t in per_engine[p]],
                }
                for p in shown
            ],
            "all": {
                "visibility_pct": [t.visibility_pct for t in overall],
                "answers": [t.answers for t in overall],
            },
        },
        "share_of_voice": share_of_voice,
        "sentiment": _sentiment(current, n),
        "citations": citations,
        "by_engine": [
            {
                "key": model_key(p),
                "label": MODEL_LABELS.get(p, p),
                "configured": configured.get(p, False),
                "answers": engine_tally[p].answers,
                "mentioned": engine_tally[p].mentioned,
                "visibility_pct": engine_tally[p].visibility_pct,
                "avg_rank": engine_tally[p].avg_rank,
                "sentiment": engine_sentiment[p],
                "citations": engine_citations.get(p, {}).get("total", 0) if cited else None,
                "your_citations": engine_citations.get(p, {}).get("yours", 0) if cited else None,
            }
            for p in shown
        ],
        "by_prompt": [
            {
                "prompt_id": str(bp.prompt_id),
                "text": bp.prompt.text,
                "answers": prompt_tally[bp.id].answers,
                "visibility_pct": prompt_tally[bp.id].visibility_pct,
                "engines": {
                    model_key(p): (
                        {"answers": t.answers, "visibility_pct": t.visibility_pct,
                         "avg_rank": t.avg_rank}
                        if t.answers else None
                    )
                    for p, t in prompt_engine[bp.id].items()
                },
            }
            for bp in in_view
        ],
        "google_history": (
            _google_history(selected, window.start_at) if selected is not None else None
        ),
    }
