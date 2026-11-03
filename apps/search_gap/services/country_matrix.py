"""Country check payloads: options, runs, cells and the country matrix
(contract sections 2d, 2f and 2g).

The matrix keeps, per (prompt, country, engine), the newest finished
answer within WINDOW_DAYS (a measured one beats a newer failure), sums it
up per country, and compares Google with the engines per prompt using the
same scoring as the global gap. A Google miss is a range, not a zero: the
site may rank just past the depth searched, so a gap is reported only
when both ends of that range agree.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import UTC, timedelta

from django.conf import settings
from django.db.models import Count
from django.utils import timezone

from apps.citations.services import serp_google
from apps.llm_ranking.services import regions
from apps.search_gap.models import CountryCheckResult, CountryCheckRun
from apps.search_gap.services import country_checks, country_geo
from apps.search_gap.services.google_rank import PAGE_SIZE
from apps.search_gap.services.scoring import NO_DATA, Answer, ai_score, overall, search_score
from core.exceptions import ResourceNotFound

WINDOW_DAYS = 120
GOOGLE = CountryCheckResult.GOOGLE
CELL_STATUSES = ("queued", "running", "done", "failed", "skipped", "cancelled")
GAP_CLASSES = ("ai_ahead", "google_ahead", "strong_both", "invisible_both", "even", "no_data")
_LIGHT_FIELDS = ("answer_text", "top_results", "brands", "citations")


def iso_time(value) -> str | None:
    """ISO 8601 UTC with a Z, to the millisecond."""
    if value is None:
        return None
    text = value.astimezone(UTC).isoformat()
    if value.microsecond:
        text = text[:23] + text[26:]
    return text.replace("+00:00", "Z")


def tracked_prompts(website) -> list:
    from apps.prompt_library.models import BrandPrompt

    return list(
        BrandPrompt.objects
        .filter(website=website, is_archived=False)
        .select_related("prompt")
        .order_by("created_at")
    )


def prompt_payload(bp) -> dict:
    return {"prompt_id": str(bp.prompt_id), "text": bp.prompt.text, "location": bp.location}


def _engine_order(engine: str) -> int:
    if engine == GOOGLE:
        return 0
    try:
        return country_geo.ENGINE_KEYS.index(engine) + 1
    except ValueError:
        return 99


# -- runs ------------------------------------------------------------------


def status_counts(run_ids) -> dict:
    """``{run_id: {status: cells}}`` in one query."""
    out: dict = defaultdict(dict)
    rows = (
        CountryCheckResult.objects.filter(run_id__in=list(run_ids))
        .values("run_id", "status").annotate(n=Count("id"))
    )
    for row in rows:
        out[row["run_id"]][row["status"]] = row["n"]
    return out


def progress(counts: dict) -> dict:
    out = {"total": sum(counts.get(s, 0) for s in CELL_STATUSES)}
    out.update({s: counts.get(s, 0) for s in CELL_STATUSES})
    return out


def _user_name(user) -> str | None:
    if user is None:
        return None
    return (user.full_name or "").strip() or user.email.split("@")[0]


def _duration_ms(run) -> int | None:
    if not (run.started_at and run.finished_at):
        return None
    return max(0, int((run.finished_at - run.started_at).total_seconds() * 1000))


def run_summary(run, *, counts: dict | None = None, names: dict | None = None) -> dict:
    """``RunSummary`` (contract 2f)."""
    names = names if names is not None else country_geo.country_names()
    if counts is None:
        counts = status_counts([run.id]).get(run.id, {})
    pages = run.google_pages_fetched
    scraper = run.provider == serp_google.PROVIDER_SCRAPERAPI
    countries = []
    for country in run.countries:
        method = country_geo.google_method(country, run.provider) if run.include_google else None
        countries.append({
            "country": country,
            "name": names.get(country, country.upper()),
            "status": (run.country_status or {}).get(country, "queued"),
            "google_method": method["method"] if method else None,
            "google_method_label": method["label"] if method else None,
        })
    # Stored when the run ended; read live from the usage ledger while it runs.
    finished = run.status in CountryCheckRun.TERMINAL_STATUSES
    ai_usd = run.ai_cost_usd if finished else country_checks.ledger_cost(run)
    return {
        "id": str(run.id),
        "status": run.status,
        "created_at": iso_time(run.created_at),
        "started_at": iso_time(run.started_at),
        "finished_at": iso_time(run.finished_at),
        "duration_ms": _duration_ms(run),
        "created_by_name": _user_name(run.created_by),
        "countries": countries,
        "include_google": run.include_google,
        "google_pages": run.google_pages,
        "provider": run.provider or None,
        "confirmed": run.confirmed,
        "progress": progress(counts),
        "cost": {
            "google_pages_fetched": pages,
            "scraperapi_credits": pages * country_checks.credits_per_search() if scraper else None,
            "google_usd": country_checks.usd4(
                serp_google.cost_per_search_usd(run.provider) * pages if run.provider else 0
            ),
            "ai_usd": country_checks.usd4(ai_usd),
        },
        "cancel_requested": run.cancel_requested,
        "error": {"code": run.error_code, "message": run.error_message} if run.error_code else None,
    }


def _run_engines(run) -> list[dict]:
    """The engines asked, with the method planned when the check was created."""
    planned = {e["key"]: e for e in ((run.estimate or {}).get("engines") or {}).get("by_engine") or []}
    out = []
    for key in run.engines:
        row = planned.get(key)
        if row:
            method, label = row.get("method"), row.get("method_label")
        else:
            method = regions.planned_country_method(country_geo.provider_key(key))
            label = regions.engine_method_label(method, None)
        out.append({
            "key": key, "label": country_geo.ENGINE_LABELS.get(key, key),
            "method": method, "method_label": label,
        })
    return out


def run_payload(run, *, with_results: bool = False) -> dict:
    """``CountryRun`` (contract 2f), plus ``results`` for the run detail."""
    from apps.prompt_library.models import BrandPrompt

    names = country_geo.country_names()
    payload = run_summary(run, names=names)
    by_bp = {
        str(bp.id): bp
        for bp in BrandPrompt.objects.filter(id__in=run.brand_prompt_ids).select_related("prompt")
    }
    prompts = [by_bp[bp_id] for bp_id in run.brand_prompt_ids if bp_id in by_bp]
    out = {}
    for key, value in payload.items():
        out[key] = value
        if key == "countries":
            out["prompts"] = [{"prompt_id": str(bp.prompt_id), "text": bp.prompt.text} for bp in prompts]
            out["engines"] = _run_engines(run)
    if with_results:
        prompt_ids = {bp.id: str(bp.prompt_id) for bp in prompts}
        order = {bp.id: i for i, bp in enumerate(prompts)}
        countries = {c: i for i, c in enumerate(run.countries)}
        cells = sorted(
            run.results.defer(*_LIGHT_FIELDS),
            key=lambda r: (
                order.get(r.brand_prompt_id, 999), countries.get(r.country, 99), _engine_order(r.engine),
            ),
        )
        out["results"] = [
            cell_payload(r, prompt_id=prompt_ids.get(r.brand_prompt_id)) for r in cells
        ]
    return out


def runs_list(website, limit: int = 20) -> list[dict]:
    runs = list(
        CountryCheckRun.objects.filter(website=website)
        .select_related("created_by").order_by("-created_at")[:limit]
    )
    counts = status_counts(r.id for r in runs)
    names = country_geo.country_names()
    return [run_summary(r, counts=counts.get(r.id, {}), names=names) for r in runs]


def last_run(website):
    return (
        CountryCheckRun.objects
        .filter(website=website, status__in=CountryCheckRun.TERMINAL_STATUSES)
        .select_related("created_by").order_by("-created_at").first()
    )


# -- options ---------------------------------------------------------------


def options_payload(website, user) -> dict:
    """``CountryOptions`` (contract 2d)."""
    which = serp_google.provider()
    quota = country_checks.google_quota()
    key = str(website.id)
    daily_limit, daily_left = quota.limit(key), quota.remaining(key)
    countries = []
    for row in country_geo.catalog():
        method = country_geo.google_method(row["country"])
        countries.append({
            **row,
            "google_method": method["method"] if method else None,
            "google_method_label": method["label"] if method else None,
        })
    active, last = country_checks.active_run(website), last_run(website)
    return {
        "enabled": bool(settings.COUNTRY_CHECKS_ENABLED),
        "limits": country_checks.limits(),
        "google": {
            "configured": bool(which),
            "provider": which or None,
            "provider_label": country_geo.PROVIDER_LABELS.get(which),
            "credits_per_page": (
                (country_checks.credits_per_search() if which == serp_google.PROVIDER_SCRAPERAPI else 0)
                if which else None
            ),
            "usd_per_page": country_checks.usd4(serp_google.cost_per_search_usd()) if which else None,
            "daily": {"limit": daily_limit, "used": max(0, daily_limit - daily_left), "left": daily_left},
        },
        "engines": country_geo.engine_options(),
        "countries": countries,
        "prompts": [prompt_payload(bp) for bp in tracked_prompts(website)],
        "allowance": country_checks.allowance_payload(user),
        "active_run": run_summary(active) if active else None,
        "last_run": run_summary(last) if last else None,
    }


# -- cells -----------------------------------------------------------------


def cell_state(result) -> str:
    """What to show for a cell (contract 2g ``state``)."""
    if result.status == CountryCheckResult.STATUS_DONE:
        measured = result.position if result.engine == GOOGLE else result.mentioned
        return "ok" if measured else "not_found"
    return result.status


def _method_label(result) -> str | None:
    if not result.method:
        return None
    if result.engine == GOOGLE:
        return serp_google.geo_label(result.method, result.country, result.provider)
    return regions.engine_method_label(result.method, result.country)


def _overview(block) -> dict | None:
    if not block:
        return None
    return {k: block.get(k) for k in ("shown", "content_captured", "mentions_you", "cites_you")}


def cell_payload(result, *, prompt_id: str | None = None) -> dict:
    """``CountryCell`` (contract 2g)."""
    google = result.engine == GOOGLE
    if prompt_id is None:
        prompt_id = str(result.brand_prompt.prompt_id)
    return {
        "id": str(result.id),
        "run_id": str(result.run_id),
        "prompt_id": prompt_id,
        "country": result.country,
        "engine": result.engine,
        "status": result.status,
        "state": cell_state(result),
        "method": result.method or None,
        "method_label": _method_label(result),
        "position": result.position if google else None,
        "depth": result.depth if google else None,
        "url": (result.url or None) if google else None,
        "pages_fetched": result.pages_fetched if google else 0,
        "ai_overview": _overview(result.ai_overview) if google else None,
        "mentioned": None if google else result.mentioned,
        "rank": None if google else result.rank,
        "sentiment": None if google else (result.sentiment or None),
        "error": {"code": result.error_code, "message": result.error_message} if result.error_code else None,
        "checked_at": iso_time(result.finished_at),
    }


def cell_detail_payload(result) -> dict:
    """``CountryCellDetail``: the cell plus what it was measured from."""
    google = result.engine == GOOGLE
    bp = result.brand_prompt
    payload = cell_payload(result, prompt_id=str(bp.prompt_id))
    payload.update({
        "top_results": list(result.top_results or []) if google else None,
        "answer_text": None if google else (result.answer_text or None),
        "brands": None if google else list(result.brands or []),
        "citations": None if google else list(result.citations or []),
        "model_id": None if google else (result.model_id or None),
        "extraction_model": None if google else (result.extraction_model or None),
        "prompt_text": bp.prompt.text,
        "country_name": country_geo.country_names().get(result.country),
    })
    return payload


# -- the matrix ------------------------------------------------------------


def _latest(website, brand_prompts) -> dict:
    """``{(brand_prompt_id, country, engine): result}``: the newest done
    result within the window, else the newest failed or skipped one."""
    since = timezone.now() - timedelta(days=WINDOW_DAYS)
    latest: dict = {}
    rows = (
        CountryCheckResult.objects
        .filter(
            website=website, brand_prompt__in=brand_prompts, finished_at__gte=since,
            status__in=(
                CountryCheckResult.STATUS_DONE, CountryCheckResult.STATUS_FAILED,
                CountryCheckResult.STATUS_SKIPPED,
            ),
        )
        .defer(*_LIGHT_FIELDS)
        .order_by("-finished_at")
    )
    for r in rows:
        key = (r.brand_prompt_id, r.country, r.engine)
        current = latest.get(key)
        if current is None or (current.status != CountryCheckResult.STATUS_DONE
                               and r.status == CountryCheckResult.STATUS_DONE):
            latest[key] = r
    return latest


def _summary_state(measured: list, cells: list, configured: bool) -> str:
    if measured:
        return "ok"
    if any(c.status == CountryCheckResult.STATUS_FAILED for c in cells):
        return "failed"
    return "not_checked" if configured else "not_configured"


def _google_summary(cells: list, configured: bool) -> dict:
    measured = [c for c in cells if c.status == CountryCheckResult.STATUS_DONE]
    found = [c for c in measured if c.position]
    failed = [c for c in cells if c.status == CountryCheckResult.STATUS_FAILED]
    state = _summary_state(measured, cells, configured)
    positions = [c.position for c in found]
    depths = [c.depth for c in measured if c.depth]
    newest = max(measured or cells, key=lambda c: c.finished_at, default=None)
    return {
        "state": state,
        "checked": len(measured),
        "found": len(found),
        "not_found": len(measured) - len(found),
        "failed": len(failed),
        "avg_position": round(sum(positions) / len(positions), 1) if positions else None,
        "best_position": min(positions) if positions else None,
        "depth": min(depths) if depths else None,
        "method": (newest.method or None) if newest else None,
        "method_label": _method_label(newest) if newest else None,
    }


def _engine_summary(cells: list, configured: bool, country: str) -> dict:
    answered = [c for c in cells if c.status == CountryCheckResult.STATUS_DONE]
    state = _summary_state(answered, cells, configured)
    if state == "failed":
        return {"state": state, "answered": 0, "mentioned": 0, "visibility_pct": None,
                "avg_rank": None, "method": None, "method_label": None}
    if state != "ok":
        return {"state": state}
    named = [c for c in answered if c.mentioned]
    ranks = [c.rank for c in named if c.rank]
    newest = max(answered, key=lambda c: c.finished_at)
    return {
        "state": "ok",
        "answered": len(answered),
        "mentioned": len(named),
        "visibility_pct": round(100 * len(named) / len(answered), 1),
        "avg_rank": round(sum(ranks) / len(ranks), 1) if ranks else None,
        "method": newest.method or None,
        "method_label": regions.engine_method_label(newest.method, country) if newest.method else None,
    }


def _gap(bp, country: str, latest: dict, engines) -> dict:
    """Google against the engines for one prompt in one country."""
    scores = []
    for engine in engines:
        cell = latest.get((bp.id, country, engine))
        if cell is not None and cell.status == CountryCheckResult.STATUS_DONE:
            scores.append(ai_score([Answer(bool(cell.mentioned), cell.rank)]))
    ai = overall(scores, None)["ai_score"]
    entry = {
        "prompt_id": str(bp.prompt_id), "country": country, "class": NO_DATA, "value": None,
        "definite": False, "ai_score": ai, "search_score": None, "search_score_max": None,
    }
    google = latest.get((bp.id, country, GOOGLE))
    if google is None or google.status != CountryCheckResult.STATUS_DONE:
        return entry
    if google.position:
        search = search_score(google.position)
        entry["search_score"] = search
        if ai is not None:
            gap = overall(scores, search)
            entry.update({"class": gap["class"], "value": gap["value"], "definite": True})
        return entry
    # Not within ``depth``: anywhere from just past it to not ranked at all.
    high = search_score((google.depth or PAGE_SIZE) + 1)
    entry["search_score_max"] = high
    if ai is not None:
        upper, lower = overall(scores, high)["class"], overall(scores, 0)["class"]
        if upper == lower:
            entry.update({"class": upper, "definite": True})
    return entry


def build_matrix(website, *, prompt_id=None, engine: str | None = None) -> dict:
    """``CountryMatrix`` (contract 2g). ``prompt_id`` (a tracked library
    prompt) limits prompts and cells; ``engine`` ("google" or a public key)
    limits the country summaries' engines and the cells."""
    brand_prompts = tracked_prompts(website)
    if prompt_id is not None:
        brand_prompts = [bp for bp in brand_prompts if str(bp.prompt_id) == str(prompt_id)]
        if not brand_prompts:
            raise ResourceNotFound("Prompt not found.")
    latest = _latest(website, brand_prompts)
    names = country_geo.country_names()
    configured = {key: country_geo.engine_configured(key) for key in country_geo.ENGINE_KEYS}
    google_configured = serp_google.is_configured()

    if engine == GOOGLE:
        summary_engines: tuple = ()
    elif engine:
        summary_engines = (engine,)
    else:
        summary_engines = country_geo.ENGINE_KEYS
    gap_engines = (engine,) if engine and engine != GOOGLE else country_geo.ENGINE_KEYS

    by_country: dict = defaultdict(list)
    for (_bp_id, country, _engine), cell in latest.items():
        by_country[country].append(cell)
    order = sorted(by_country, key=lambda c: (names.get(c, c.upper()), c))

    countries, gaps = [], []
    for country in order:
        cells = by_country[country]
        engines = {}
        answered = mentioned = 0
        for key in summary_engines:
            summary = _engine_summary([c for c in cells if c.engine == key], configured[key], country)
            engines[key] = summary
            answered += summary.get("answered") or 0
            mentioned += summary.get("mentioned") or 0
        checked_prompts = {c.brand_prompt_id for c in cells}
        country_gaps = [
            _gap(bp, country, latest, gap_engines) for bp in brand_prompts if bp.id in checked_prompts
        ]
        gaps.extend(country_gaps)
        classes = Counter(g["class"] for g in country_gaps)
        countries.append({
            "country": country,
            "name": names.get(country, country.upper()),
            "last_checked_at": iso_time(max(c.finished_at for c in cells)),
            "google": _google_summary([c for c in cells if c.engine == GOOGLE], google_configured),
            "engines": engines,
            "ai_visibility_pct": round(100 * mentioned / answered, 1) if answered else None,
            "gap_counts": {name: classes.get(name, 0) for name in GAP_CLASSES},
        })

    prompt_order = {bp.id: i for i, bp in enumerate(brand_prompts)}
    country_order = {c: i for i, c in enumerate(order)}
    prompt_ids = {bp.id: str(bp.prompt_id) for bp in brand_prompts}
    cells = [
        c for c in latest.values()
        if engine is None or c.engine == engine
    ]
    cells.sort(key=lambda c: (
        prompt_order.get(c.brand_prompt_id, 999), country_order.get(c.country, 999),
        _engine_order(c.engine),
    ))
    active = country_checks.active_run(website)
    last_checked = max((c.finished_at for c in latest.values()), default=None)
    return {
        "google": {"configured": google_configured, "provider": serp_google.provider() or None},
        "engines": [
            {"key": key, "label": country_geo.ENGINE_LABELS[key], "configured": configured[key]}
            for key in country_geo.ENGINE_KEYS
        ],
        "prompts": [prompt_payload(bp) for bp in brand_prompts],
        "countries": countries,
        "cells": [cell_payload(c, prompt_id=prompt_ids[c.brand_prompt_id]) for c in cells],
        "gaps": gaps,
        "last_checked_at": iso_time(last_checked),
        "active_run": run_summary(active) if active else None,
    }
