"""Query fan-outs (contract 8): the web searches the engines ran while
answering the tracked prompts, and where the project ranks on Google for
them.

ChatGPT and Claude report their searches (Grok only when its response
carries search items); the providers store them on each answer
(``LLMRankingResult.search_queries`` and ``web_searches``, None when not
recorded). Searches are grouped per prompt by ``text.idea_key``, the rule
the agent and Prompt research use for "the same search". A
``fanout_google`` run reads Google page one for chosen searches in the
prompt's country; it records a ``FanoutGoogleCheck``, never a rank check.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from django.conf import settings
from django.utils import timezone

from apps.citations.services import serp_google
from apps.citations.services.url_analytics import model_key
from apps.citations.services.url_normalizer import extract_apex_domain
from apps.insights.models import FanoutGoogleCheck
from apps.insights.services import runs
from apps.insights.services.runs import iso
from apps.insights.services.text import collapse, idea_key
from apps.llm_ranking.services import answer_window
from apps.search_gap.services.google_rank import is_same_site, site_apex
from core.exceptions import CanseeException, ResourceNotFound

KEY = "fanout_google"
# Public keys of the engines whose providers report their searches.
REPORTING = ("chatgpt", "claude")
GOOGLE_DAYS = 30
RECORDED_DAYS = 90
MAX_QUERIES_PER_PROMPT = 100
MAX_TOP_RESULTS = 10
CONCURRENCY = 3
CHECK_CHUNK = 500
FIELDS = (*answer_window.RESULT_FIELDS, "search_queries", "web_searches")
NOT_RECORDED = "This search wasn't recorded for this prompt."


@dataclass
class _Query:
    key: str
    text: str
    seen_at: datetime
    engines: set = field(default_factory=set)
    answers: set = field(default_factory=set)


def _location(bp) -> str:
    """The prompt's country as checks store it (ISO-2, "" for global)."""
    return (bp.location or "")[:2]


def _brand_prompts(website) -> list:
    from apps.prompt_library.models import BrandPrompt

    return list(
        BrandPrompt.objects.filter(website=website, is_archived=False)
        .select_related("prompt").order_by("created_at")
    )


def _group(pairs) -> dict:
    """{brand prompt id: {query key: _Query}} of the answers' searches."""
    out: dict = {}
    for bp, result in pairs:
        queries = out.setdefault(bp.id, {})
        for text in result.search_queries or []:
            key = idea_key(text)
            if not key:
                continue
            query = queries.get(key)
            if query is None:
                query = queries[key] = _Query(key, text, result.created_at)
            elif result.created_at > query.seen_at:
                query.text, query.seen_at = text, result.created_at
            query.engines.add(result.provider)
            query.answers.add(result.id)
    return out


def _latest_checks(website, keys) -> dict:
    """{(query key, location): the newest FanoutGoogleCheck within 30 days}."""
    keys = sorted(set(keys))
    since = timezone.now() - timedelta(days=GOOGLE_DAYS)
    found: dict = {}
    for offset in range(0, len(keys), CHECK_CHUNK):
        checks = (
            FanoutGoogleCheck.objects
            .filter(website=website, query_key__in=keys[offset:offset + CHECK_CHUNK],
                    checked_at__gte=since)
            .order_by("-checked_at")
        )
        for check in checks:
            found.setdefault((check.query_key, check.location), check)
    return found


def google_block(check) -> dict:
    """A search's Google state (contract 8) from its newest check."""
    if check is None:
        return {"state": "not_checked", "position": None, "depth": None, "checked_at": None,
                "url": None}
    if check.error_code:
        return {"state": "failed", "position": None, "depth": None,
                "checked_at": iso(check.checked_at), "url": None}
    found = check.position is not None
    return {
        "state": "found" if found else "not_found",
        "position": check.position,
        "depth": check.depth,
        "checked_at": iso(check.checked_at),
        "url": (check.url or None) if found else None,
    }


def _engines(providers) -> list[str]:
    return [model_key(p) for p in answer_window.ENGINE_ORDER if p in providers]


def _query_row(query: _Query, check) -> dict:
    return {
        "key": query.key,
        "text": query.text,
        "engines": _engines(query.engines),
        "answers": len(query.answers),
        "last_seen_at": iso(query.seen_at),
        "google": google_block(check),
    }


def _ranked(queries) -> list:
    return sorted(queries, key=lambda q: (-len(q.answers), -q.seen_at.timestamp(), q.key))


def _counts(results) -> dict:
    return {
        "answers": len(results),
        "recorded": sum(1 for r in results if r.search_queries is not None),
        "searched": sum(1 for r in results if (r.web_searches or 0) > 0),
        "searches": sum(r.web_searches or 0 for r in results),
        "queries": sum(len(r.search_queries or []) for r in results),
    }


def _not_reported(results) -> int:
    """Searches an engine ran without sending their text."""
    return sum(max(0, (r.web_searches or 0) - len(r.search_queries or [])) for r in results)


def _window(window) -> dict:
    return {
        "period": window.period,
        "interval": window.interval,
        "start": window.start.isoformat(),
        "end": window.end.isoformat(),
        "buckets": [day.isoformat() for day in window.buckets],
    }


def build(website, *, period: str = "30d", engine=None, prompt_id=None) -> dict:
    """The Query fan-outs payload (contract 8)."""
    period, interval, engine, prompt_uuid = answer_window.parse_filters(
        period=period, engine=engine, prompt_id=prompt_id,
    )
    window = answer_window.Window(period, interval, timezone.now().date())
    brand_prompts = _brand_prompts(website)
    selected = None
    if prompt_uuid is not None:
        selected = next((bp for bp in brand_prompts if bp.prompt_id == prompt_uuid), None)
        if selected is None:
            raise ResourceNotFound("Prompt not found.")
    pairs = [
        (bp, result)
        for bp, result in answer_window.tracked_answers(
            website, brand_prompts, since=window.start_at, fields=FIELDS,
        )
        if (selected is None or bp.id == selected.id)
        and (engine is None or model_key(result.provider) == engine)
    ]
    results = [result for _bp, result in pairs]
    grouped = _group(pairs)
    checks = _latest_checks(website, [key for queries in grouped.values() for key in queries])
    by_prompt: dict = {}
    for bp, result in pairs:
        by_prompt.setdefault(bp.id, (bp, []))[1].append(result)

    order = {bp.id: i for i, bp in enumerate(brand_prompts)}
    rows = []
    for bp_id, (bp, answers) in by_prompt.items():
        counts = _counts(answers)
        queries = _ranked(grouped.get(bp_id, {}).values())[:MAX_QUERIES_PER_PROMPT]
        rows.append((order[bp_id], {
            "prompt_id": str(bp.prompt_id),
            "text": bp.prompt.text,
            "location": bp.location,
            "answers": counts["answers"],
            "searched": counts["searched"],
            "searches": counts["searches"],
            "not_reported": _not_reported(answers),
            "queries": [_query_row(q, checks.get((q.key, _location(bp)))) for q in queries],
        }))
    rows.sort(key=lambda item: (-item[1]["searched"], -item[1]["answers"], item[0]))
    prompts = [row for _i, row in rows]

    enabled = bool(getattr(settings, "LLM_WEBSEARCH_ENABLED", False))
    if not results:
        empty_reason = "no_answers"
    elif not enabled and not any((r.web_searches or 0) > 0 for r in results):
        empty_reason = "web_search_off"
    elif enabled and not any(r.search_queries is not None for r in results):
        empty_reason = "none_recorded"
    else:
        empty_reason = None

    unique = {idea_key(text) for r in results for text in r.search_queries or []} - {""}
    return {
        "window": _window(window),
        "filters": {"engine": engine, "prompt_id": str(prompt_uuid) if prompt_uuid else None},
        "web_search": {
            "enabled": enabled,
            "engines": [{"key": key, "reports_queries": key in REPORTING}
                        for key in answer_window.ENGINE_KEYS],
        },
        "empty_reason": empty_reason,
        "totals": {**_counts(results), "unique_queries": len(unique)},
        "by_engine": [
            {"key": model_key(provider), **_counts([r for r in results if r.provider == provider])}
            for provider in answer_window.ENGINE_ORDER
            if any(r.provider == provider for r in results)
        ],
        "prompts": prompts,
        **runs.run_state(website, KEY),
    }


def _prompt_uuids(prompt_ids) -> set:
    out = set()
    for value in prompt_ids or []:
        try:
            out.add(value if isinstance(value, uuid.UUID) else uuid.UUID(str(value)))
        except ValueError:
            continue
    return out


def queries_for_prompts(website, prompt_ids, *, period: str = "30d", limit: int = 10) -> list[dict]:
    """The searches recorded for these tracked prompts in the period, most
    answers first: ``[{prompt_id, prompt_text, query, engines, answers,
    last_seen_at, google}]``, at most ``limit``."""
    wanted = _prompt_uuids(prompt_ids)
    if not wanted or limit <= 0:
        return []
    period, interval, _engine, _prompt = answer_window.parse_filters(period=period)
    window = answer_window.Window(period, interval, timezone.now().date())
    brand_prompts = _brand_prompts(website)
    pairs = [
        (bp, result)
        for bp, result in answer_window.tracked_answers(
            website, brand_prompts, since=window.start_at, fields=FIELDS,
        )
        if bp.prompt_id in wanted
    ]
    by_id = {bp.id: bp for bp, _r in pairs}
    rows = [
        (by_id[bp_id], query)
        for bp_id, queries in _group(pairs).items()
        for query in queries.values()
    ]
    rows.sort(key=lambda row: (-len(row[1].answers), -row[1].seen_at.timestamp(), row[1].key))
    rows = rows[:limit]
    checks = _latest_checks(website, [query.key for _bp, query in rows])
    return [
        {
            "prompt_id": str(bp.prompt_id),
            "prompt_text": bp.prompt.text,
            "query": query.text,
            "engines": _engines(query.engines),
            "answers": len(query.answers),
            "last_seen_at": iso(query.seen_at),
            "google": google_block(checks.get((query.key, _location(bp)))),
        }
        for bp, query in rows
    ]


# -- kind fanout_google ------------------------------------------------------------


@dataclass(frozen=True)
class Search:
    """One search to read on Google for a tracked prompt."""

    brand_prompt: object
    text: str
    key: str

    @property
    def location(self) -> str:
        return _location(self.brand_prompt)


def _invalid(message: str, field_name: str = "queries") -> CanseeException:
    return CanseeException(message, code="invalid", status_code=400,
                           details={"field": field_name, "message": message})


def _recorded(website, brand_prompts) -> dict:
    """{prompt id: {query key: newest text}} of the last RECORDED_DAYS."""
    since = timezone.now() - timedelta(days=RECORDED_DAYS)
    pairs = answer_window.tracked_answers(website, brand_prompts, since=since, fields=FIELDS)
    by_bp = {bp.id: bp.prompt_id for bp in brand_prompts}
    return {
        by_bp[bp_id]: {key: query.text for key, query in queries.items()}
        for bp_id, queries in _group(pairs).items()
    }


def validate(website, params) -> list[Search]:
    raw = params.get("queries")
    if not serp_google.is_configured():
        raise CanseeException("Google results aren't configured on this server.",
                              code="not_configured", status_code=400)
    if raw in (None, []):
        raise CanseeException("Choose at least one search.", code="nothing_to_run",
                              status_code=400)
    if not isinstance(raw, list):
        raise _invalid("Send the searches as a list.")
    brand_prompts = _brand_prompts(website)
    by_prompt = {bp.prompt_id: bp for bp in brand_prompts}
    recorded = _recorded(website, brand_prompts)
    plan: list[Search] = []
    seen: set = set()
    for item in raw:
        if not isinstance(item, dict):
            raise _invalid(NOT_RECORDED)
        try:
            prompt_uuid = uuid.UUID(str(item.get("prompt_id")))
        except ValueError:
            raise _invalid(NOT_RECORDED) from None
        key = idea_key(collapse(item.get("text")))
        text = recorded.get(prompt_uuid, {}).get(key)
        bp = by_prompt.get(prompt_uuid)
        if bp is None or not key or text is None:
            raise _invalid(NOT_RECORDED)
        if (prompt_uuid, key) not in seen:
            seen.add((prompt_uuid, key))
            plan.append(Search(bp, text, key))
    limit = settings.INSIGHTS_FANOUT_MAX_QUERIES
    if len(plan) > limit:
        raise CanseeException(f"This run is over the limit of {limit} searches.",
                              code="too_many", status_code=400)
    return plan


def estimate(website, user, plan: list[Search]) -> dict:
    n = len(plan)
    return {
        "summary": f"{n} {'search' if n == 1 else 'searches'} checked on Google",
        "units": {"queries": n},
        "ai": {"calls_max": 0, "usd_max": "0"},
        "google": {"pages_max": n},
        "fetches": {"pages_max": 0},
        "warnings": [],
    }


def choices(website, user) -> dict:
    return {}


def plan_items(run, plan: list[Search]) -> int:
    # One FanoutGoogleCheck per search is written as it is read.
    return len(plan)


def _searches(website, params) -> list[Search]:
    """The run's searches as confirmed (validated when it was created)."""
    from apps.prompt_library.models import BrandPrompt

    by_prompt = {
        bp.prompt_id: bp
        for bp in BrandPrompt.objects.filter(website=website).select_related("prompt")
    }
    out, seen = [], set()
    for item in params.get("queries") or []:
        try:
            bp = by_prompt.get(uuid.UUID(str(item.get("prompt_id"))))
        except (AttributeError, ValueError):
            continue
        text = collapse(item.get("text"))[:300]
        key = idea_key(text)
        if bp is not None and key and (bp.id, key) not in seen:
            seen.add((bp.id, key))
            out.append(Search(bp, text, key))
    return out


def _competitor_apexes(website) -> set:
    out = set()
    for item in website.competitors or []:
        domain = item.get("domain") if isinstance(item, dict) else ""
        apex = extract_apex_domain(str(domain or "").strip().lower())
        if apex:
            out.add(apex)
    return out


def _check(ctx, search: Search, apex: str, competitors: set) -> None:
    from apps.insights.services import google

    if ctx.cancelled():
        # Left unfinished: finalize ends it as stopped.
        return
    ctx.start_item()
    payload = google.page_one(ctx, search.text, country=search.location)
    error = str(payload.get("error") or "")
    if error == "cancelled":
        ctx.end_item("cancelled")
        return
    if error == "daily_limit":
        ctx.end_item("skipped", error="daily_limit")
        return
    fields = {
        "website": ctx.website, "run": ctx.run, "prompt": search.brand_prompt.prompt,
        "query": search.text[:300], "query_key": search.key[:300],
        "location": search.location, "checked_at": timezone.now(),
    }
    if error:
        FanoutGoogleCheck.objects.create(**fields, error_code="serp_failed")
        ctx.end_item("failed", error="serp_failed")
        return
    organic = [row for row in payload.get("organic") or [] if isinstance(row, dict)]
    top = []
    for i, row in enumerate(organic[:MAX_TOP_RESULTS], start=1):
        domain = row.get("domain") or ""
        top.append({
            "position": row.get("position") or row.get("rank") or i,
            "url": row.get("url") or "",
            "domain": domain,
            "title": row.get("title") or "",
            "is_yours": is_same_site(domain, apex),
            "is_competitor": extract_apex_domain(domain) in competitors,
        })
    yours = next((row for row in top if row["is_yours"]), None)
    FanoutGoogleCheck.objects.create(
        **fields,
        position=yours["position"] if yours else None,
        depth=len(organic),
        url=(yours["url"] if yours else "")[:2000],
        top_results=top,
    )
    ctx.end_item("done")


def execute(ctx) -> None:
    searches = _searches(ctx.website, ctx.run.params or {})
    apex = site_apex(ctx.website.url)
    competitors = _competitor_apexes(ctx.website)
    with ctx.pool(CONCURRENCY) as pool:
        for search in searches:
            pool.submit(_check, ctx, search, apex, competitors)


KIND = runs.Kind(
    key=KEY, label="Fan-out Google check", queue="integrations", validate=validate,
    estimate=estimate, choices=choices, plan_items=plan_items, execute=execute,
)
runs.register(KIND)
