"""Country checks: Google and the AI engines for tracked prompts, per country.

A check is estimated first (``estimate``: pages, credits, answers and an
upper bound on AI spend) and runs only after the user confirms that
estimate (``create_run``). It then runs one country at a time
(``execute_country``), Google pages and engine answers side by side, and
ends in ``finalize_if_done``. Answers are kept on CountryCheckResult only:
LLMRankingResult and GoogleRankCheck (the global numbers) never change.

Spend stays bounded three ways: the per-check caps (COUNTRY_CHECK_MAX_*),
the per-website daily Google page quota, and the user's monthly AI
allowance, which create checks up front and every provider call enforces
again (LLMProvider.query with ``user``).
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass
from datetime import timedelta
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal, InvalidOperation

from django.conf import settings
from django.db import close_old_connections, connection, transaction
from django.db.models import F, Sum
from django.db.models.functions import Coalesce
from django.utils import timezone

from apps.citations.services import serp_google
from apps.citations.services.url_normalizer import extract_apex_domain
from apps.llm_ranking.services import regions
from apps.llm_ranking.services.extraction_service import HaikuExtractionService
from apps.search_gap.models import CountryCheckResult, CountryCheckRun
from apps.search_gap.services import country_geo, google_rank
from core.exceptions import CanseeException
from core.quota import DailyQuota

logger = logging.getLogger("apps")

# Planning upper bounds per engine answer, including its extraction.
# An answer is capped at 4,096 output tokens: Claude Haiku 4.5 ($5 per million
# out) can reach about $0.021 before its extraction, and Grok 4 ($15 per
# million out) about $0.062, so each gets a bound it cannot exceed.
ENGINE_ANSWER_USD = Decimal("0.0300")          # answer + Haiku extraction
GROK_ANSWER_USD = Decimal("0.0700")            # Grok 4: 4,096 tokens at $15 per million + extraction
ENGINE_ANSWER_SEARCH_USD = Decimal("0.0800")   # ChatGPT / Claude searching (5 searches + tokens)
PERPLEXITY_ANSWER_USD = Decimal("0.0300")      # Sonar request fee + tokens

# How often a worker re-reads Stop, and the pause before the one retry of a
# Google page that failed in a way a second request usually fixes.
CANCEL_POLL_SECONDS = 2.0
RETRY_SECONDS = 3.0
_RETRY_CODES = ("network", "http_429", "empty_results")

MAX_TOP_RESULTS = 10
MAX_BRANDS = 10
MAX_CITATIONS = 10
SENTIMENTS = ("positive", "neutral", "negative")

GOOGLE = CountryCheckResult.GOOGLE
QUEUED = CountryCheckResult.STATUS_QUEUED
RUNNING = CountryCheckResult.STATUS_RUNNING
DONE = CountryCheckResult.STATUS_DONE
FAILED = CountryCheckResult.STATUS_FAILED
SKIPPED = CountryCheckResult.STATUS_SKIPPED
CANCELLED = CountryCheckResult.STATUS_CANCELLED
ACTIVE = CountryCheckRun.ACTIVE_STATUSES
TERMINAL = CountryCheckRun.TERMINAL_STATUSES

# Public cell error messages (contract section 2h).
CELL_MESSAGES = {
    "serp_failed": "Google search failed.",
    "daily_limit": "This project used its Google pages for today.",
    "not_configured": "The engine is not configured.",
    "rate_limited": "The engine was busy or rate limited.",
    "timeout": "The engine did not respond in time.",
    "allowance_exceeded": "The monthly AI allowance is used up.",
    "refused": "The engine declined to answer this prompt.",
    "cut_off": "The engine's answer was cut off before it finished.",
    "engine_error": "The engine returned an error.",
    "cancelled": "Stopped before it ran.",
    "interrupted": "The check stopped before this finished.",
    "internal_error": "Something went wrong with this check.",
}
GOOGLE_NOT_CONFIGURED = "Google results are not configured."
# serp_google error codes in words (the agent's research.SERP_ERRORS).
SERP_ERRORS = {
    "network": "Google search could not be reached.",
    "decode": "Google search returned an unreadable response.",
    "empty_query": "The search query was empty.",
    "empty_results": "Google's results page came back empty, so this search could not be read.",
}
RUN_INTERRUPTED = "The check stopped before it finished."
RUN_IN_PROGRESS = "A country check is running for this project."

# The order create_run reports blockers in when several apply.
_ERROR_ORDER = (
    "nothing_to_check", "too_many_google_pages", "too_many_answers",
    "run_in_progress", "daily_limit", "allowance_exceeded", "insufficient_allowance",
)
_ERROR_STATUS = {
    "nothing_to_check": 400, "too_many_google_pages": 400, "too_many_answers": 400,
    "run_in_progress": 409, "daily_limit": 409,
    "allowance_exceeded": 403, "insufficient_allowance": 403,
}


# -- money, limits, quota -------------------------------------------------


def usd4(value) -> str:
    return str(Decimal(value).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))


def usd2(value, rounding=ROUND_HALF_UP) -> str:
    return str(Decimal(value).quantize(Decimal("0.01"), rounding=rounding))


def _decimal(value) -> Decimal:
    # The allowance helpers return floats; str() keeps their short form.
    return Decimal(str(value or 0))


def limits() -> dict:
    return {
        "max_countries": settings.COUNTRY_CHECK_MAX_COUNTRIES,
        "max_prompts": settings.COUNTRY_CHECK_MAX_PROMPTS,
        # Never past the pages the provider reads reliably (ScraperAPI: the top 10).
        "max_pages": min(settings.COUNTRY_CHECK_MAX_PAGES, serp_google.max_reliable_pages()),
        "max_google_pages": settings.COUNTRY_CHECK_MAX_GOOGLE_PAGES,
        "max_engine_answers": settings.COUNTRY_CHECK_MAX_ENGINE_ANSWERS,
    }


def google_quota() -> DailyQuota:
    """Google results pages per website per UTC day, keyed by website id."""
    return DailyQuota(
        namespace="country_serp",
        setting_name="COUNTRY_CHECK_DAILY_GOOGLE_PAGES",
        default_limit=100,
    )


def credits_per_search() -> int:
    return int(settings.SCRAPERAPI_CREDITS_PER_SEARCH)


def answer_usd_max(engine: str) -> Decimal:
    """Upper bound for one answer from ``engine`` (a public key)."""
    key = country_geo.provider_key(engine)
    if key == "perplexity":
        return PERPLEXITY_ANSWER_USD
    if regions.planned_country_method(key) == regions.METHOD_SEARCHED:
        return ENGINE_ANSWER_SEARCH_USD
    if key == "grok":
        return GROK_ANSWER_USD
    return ENGINE_ANSWER_USD


def _allowance(user) -> dict:
    """The user's monthly AI allowance as Decimals (``cap`` 0 = unlimited)."""
    from core import ai_tracking

    if user is None or not getattr(user, "is_authenticated", False):
        return {"unlimited": True, "cap": Decimal("0"), "spent": Decimal("0"), "left": None}
    cap = _decimal(ai_tracking.effective_ai_cap(user))
    spent = _decimal(ai_tracking.month_to_date_cost(user))
    unlimited = cap <= 0
    left = None if unlimited else max(Decimal("0"), cap - spent)
    return {"unlimited": unlimited, "cap": cap, "spent": spent, "left": left}


def allowance_payload(user) -> dict:
    """``{unlimited, cap_usd, spent_usd, left_usd}`` (options)."""
    a = _allowance(user)
    return {
        "unlimited": a["unlimited"],
        "cap_usd": None if a["unlimited"] else usd2(a["cap"]),
        "spent_usd": usd2(a["spent"]),
        "left_usd": None if a["unlimited"] else usd2(a["left"], ROUND_DOWN),
    }


def active_run(website):
    return (
        CountryCheckRun.objects
        .filter(website=website, status__in=ACTIVE)
        .order_by("-created_at")
        .first()
    )


def ledger_cost(run) -> Decimal:
    """What the usage ledger recorded for this run's answers and extractions."""
    from apps.accounts.models import AITokenUsage

    total = (
        AITokenUsage.objects
        .filter(created_at__gte=run.created_at, metadata__audit_id=run.audit_id)
        .aggregate(total=Sum("estimated_cost_usd"))["total"]
    )
    return total or Decimal("0")


# -- validation ----------------------------------------------------------


@dataclass
class CheckPlan:
    """A validated check request."""

    countries: list[str]
    prompts: list            # BrandPrompt, with ``prompt`` loaded
    engines: list[str]       # public keys, display order
    include_google: bool
    google_pages: int


def _invalid(field: str, message: str) -> CanseeException:
    return CanseeException(
        message, code="invalid", status_code=400, details={"field": field, "message": message},
    )


def _shown(value) -> str:
    return str(value).strip()[:64] or "This"


def validate_request(website, data) -> CheckPlan:
    """The request body as a CheckPlan. Raises 400 ``invalid`` (with
    ``details.field``) for anything malformed; the per-check caps and an
    empty check are blockers of the estimate, not validation errors."""
    from apps.prompt_library.models import BrandPrompt

    if not isinstance(data, dict):
        raise _invalid("countries", "Send the check as a JSON object.")
    lim = limits()

    raw = data.get("countries")
    if not isinstance(raw, list) or not raw:
        raise _invalid("countries", "Choose at least one country.")
    if len(raw) > lim["max_countries"]:
        raise _invalid("countries", f"Choose up to {lim['max_countries']} countries.")
    names = country_geo.country_names()
    countries: list[str] = []
    for item in raw:
        iso = serp_google.iso_country(item) if isinstance(item, str) else ""
        if iso not in names:
            raise _invalid("countries", f"{_shown(item)} is not a supported country.")
        if iso in countries:
            raise _invalid("countries", "Each country can be chosen once.")
        countries.append(iso)

    raw = data.get("prompt_ids")
    if not isinstance(raw, list) or not raw:
        raise _invalid("prompt_ids", "Choose at least one prompt.")
    if len(raw) > lim["max_prompts"]:
        raise _invalid("prompt_ids", f"Choose up to {lim['max_prompts']} prompts.")
    ids: list[str] = []
    for item in raw:
        try:
            pid = str(uuid.UUID(str(item)))
        except (TypeError, ValueError, AttributeError):
            message = f"No tracked prompt with id {_shown(item)} in this project."
            raise _invalid("prompt_ids", message) from None
        if pid in ids:
            raise _invalid("prompt_ids", "Each prompt can be chosen once.")
        ids.append(pid)
    tracked = {
        str(bp.prompt_id): bp
        for bp in BrandPrompt.objects
        .filter(website=website, is_archived=False, prompt_id__in=ids)
        .select_related("prompt")
    }
    for pid in ids:
        if pid not in tracked:
            raise _invalid("prompt_ids", f"No tracked prompt with id {pid} in this project.")

    raw = data.get("engines")
    if raw is None:
        engines = [k for k in country_geo.ENGINE_KEYS if country_geo.engine_configured(k)]
    else:
        if not isinstance(raw, list):
            raise _invalid("engines", "Choose the AI engines as a list.")
        chosen = set()
        for item in raw:
            key = item.strip().lower() if isinstance(item, str) else ""
            if key not in country_geo.ENGINE_LABELS:
                raise _invalid("engines", f"{_shown(item)} is not an AI engine.")
            if not country_geo.engine_configured(key):
                raise _invalid("engines", f"{country_geo.ENGINE_LABELS[key]} is not configured.")
            chosen.add(key)
        engines = [k for k in country_geo.ENGINE_KEYS if k in chosen]

    raw = data.get("include_google")
    google_ready = serp_google.is_configured()
    if raw is None:
        include_google = google_ready
    elif not isinstance(raw, bool):
        raise _invalid("include_google", "include_google must be true or false.")
    elif raw and not google_ready:
        raise _invalid("include_google", "Google results are not configured on this server.")
    else:
        include_google = raw

    raw = data.get("google_pages")
    google_pages = 1 if raw is None else raw
    if (
        isinstance(google_pages, bool) or not isinstance(google_pages, int)
        or not 1 <= google_pages <= lim["max_pages"]
    ):
        if lim["max_pages"] == 1:
            message = "Only the first Google results page (the top 10) can be checked."
        else:
            message = f"Choose 1 to {lim['max_pages']} Google results pages."
        raise _invalid("google_pages", message)

    return CheckPlan(
        countries=countries,
        prompts=[tracked[pid] for pid in ids],
        engines=engines,
        include_google=include_google,
        google_pages=google_pages,
    )


def parse_confirm(data) -> dict:
    """``{google_pages, engine_answers, ai_usd_max: Decimal}`` the user confirmed."""
    message = "Confirm the estimate before running the check."
    raw = data.get("confirm") if isinstance(data, dict) else None
    if not isinstance(raw, dict):
        raise _invalid("confirm", message)
    out = {}
    for name in ("google_pages", "engine_answers"):
        value = raw.get(name)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise _invalid("confirm", message)
        out[name] = value
    try:
        usd = Decimal(str(raw.get("ai_usd_max")))
        if not usd.is_finite() or usd < 0:
            raise ValueError
        # The run keeps it at 4 places; a number too large for that is not
        # an amount anyone confirmed.
        usd4(usd)
    except (InvalidOperation, ValueError):
        raise _invalid("confirm", message) from None
    out["ai_usd_max"] = usd
    return out


# -- estimate ------------------------------------------------------------


def _join(items: list[str]) -> str:
    if len(items) < 2:
        return "".join(items)
    return f"{', '.join(items[:-1])} and {items[-1]}"


def _asked_message(labels: list[str]) -> str:
    many = len(labels) > 1
    who = _join(labels)
    if getattr(settings, "LLM_WEBSEARCH_ENABLED", False):
        # Their search has no location parameter.
        reason = f"{who} {'have' if many else 'has'} no location setting here"
    else:
        reason = f"{who} {'answer' if many else 'answers'} without web search here"
    return (
        f"{reason}, so {'they are' if many else 'it is'} asked to answer for each country "
        "instead of searching from it."
    )


def _blocker(code: str, message: str) -> dict:
    return {"code": code, "message": message}


def estimate(website, user, plan: CheckPlan) -> dict:
    """What the check costs at most, and whether it may run (contract 2e).
    No side effects."""
    lim = limits()
    n_p, n_c, n_e = len(plan.prompts), len(plan.countries), len(plan.engines)
    pages = n_p * n_c * plan.google_pages if plan.include_google else 0
    per_engine = n_p * n_c
    answers = per_engine * n_e

    which = serp_google.provider()
    scraper = which == serp_google.PROVIDER_SCRAPERAPI
    quota = google_quota()
    key = str(website.id)
    daily_limit, daily_left = quota.limit(key), quota.remaining(key)
    methods = {c: country_geo.google_method(c) for c in plan.countries} if plan.include_google else {}
    google = {
        "included": plan.include_google,
        "pages_max": pages,
        "credits_max": pages * credits_per_search() if scraper else 0,
        "usd_max": usd4(serp_google.cost_per_search_usd() * pages),
        "provider": which or None,
        "provider_label": country_geo.PROVIDER_LABELS.get(which),
        "daily_limit": daily_limit,
        "daily_left": daily_left,
        "enough_daily": pages <= daily_left,
        "countries": [
            {"country": c, "method": m["method"], "method_label": m["label"]}
            for c, m in methods.items() if m
        ],
    }

    usd_max = Decimal("0")
    by_engine = []
    for engine in plan.engines:
        engine_usd = answer_usd_max(engine) * per_engine
        usd_max += engine_usd
        method = regions.planned_country_method(country_geo.provider_key(engine))
        by_engine.append({
            "key": engine,
            "label": country_geo.ENGINE_LABELS[engine],
            "answers": per_engine,
            "usd_max": usd4(engine_usd),
            "method": method,
            "method_label": regions.engine_method_label(method, None),
        })

    a = _allowance(user)
    exceeded = answers > 0 and not a["unlimited"] and a["spent"] >= a["cap"]
    enough = a["unlimited"] or answers == 0 or a["spent"] + usd_max <= a["cap"]

    blockers = []
    if plan.include_google and pages > daily_left:
        blockers.append(_blocker(
            "daily_limit",
            f"This project has {daily_left} Google page{'' if daily_left == 1 else 's'} left today; "
            f"this check needs up to {pages}.",
        ))
    if not enough and not exceeded:
        blockers.append(_blocker(
            "insufficient_allowance",
            f"This check needs up to ${usd2(usd_max)} of AI allowance and "
            f"${usd2(a['left'], ROUND_DOWN)} is left this month.",
        ))
    if exceeded:
        blockers.append(_blocker("allowance_exceeded", "You have used your monthly AI allowance."))
    if pages > lim["max_google_pages"]:
        blockers.append(_blocker(
            "too_many_google_pages",
            f"This check needs up to {pages} Google pages; the limit per check is "
            f"{lim['max_google_pages']}. Use fewer prompts, countries or pages.",
        ))
    if answers > lim["max_engine_answers"]:
        blockers.append(_blocker(
            "too_many_answers",
            f"This check needs {answers} engine answers; the limit per check is "
            f"{lim['max_engine_answers']}. Use fewer prompts, countries or engines.",
        ))
    if active_run(website) is not None:
        blockers.append(_blocker("run_in_progress", RUN_IN_PROGRESS))
    if not plan.include_google and not plan.engines:
        blockers.append(_blocker("nothing_to_check", "Choose Google or at least one AI engine."))

    warnings = []
    asked = [
        country_geo.ENGINE_LABELS[e] for e in plan.engines
        if regions.planned_country_method(country_geo.provider_key(e)) == regions.METHOD_ASKED
    ]
    if asked:
        warnings.append({"code": "asked_not_searched", "message": _asked_message(asked)})
    setting = [
        c.upper() for c, m in methods.items()
        if m and m["method"] == serp_google.METHOD_GOOGLE_SETTING
    ]
    if setting:
        connection_note = " through a US connection" if scraper else ""
        warnings.append({
            "code": "google_setting",
            "message": f"Google results for {_join(setting)} use Google's country setting{connection_note}.",
        })
    warnings.append({
        "code": "upper_bound",
        "message": "These are upper bounds; checks stop early when your site is found and "
                   "failed requests are not billed.",
    })

    return {
        "cells": n_p * n_c * (n_e + (1 if plan.include_google else 0)),
        "google": google,
        "engines": {"answers": answers, "usd_max": usd4(usd_max), "by_engine": by_engine},
        "allowance": {
            "unlimited": a["unlimited"],
            "cap_usd": None if a["unlimited"] else usd2(a["cap"]),
            "left_usd": None if a["unlimited"] else usd2(a["left"], ROUND_DOWN),
            "enough": enough,
        },
        "confirm": {"google_pages": pages, "engine_answers": answers, "ai_usd_max": usd4(usd_max)},
        "warnings": warnings,
        "blockers": blockers,
        "can_run": not blockers,
    }


# -- create --------------------------------------------------------------


def _run_in_progress(run) -> CanseeException:
    return CanseeException(
        RUN_IN_PROGRESS, code="run_in_progress", status_code=409, details={"run_id": str(run.id)},
    )


def _blocker_error(website, est: dict, blocker: dict) -> CanseeException:
    code = blocker["code"]
    details = None
    if code == "run_in_progress":
        current = active_run(website)
        if current is not None:
            return _run_in_progress(current)
    elif code == "daily_limit":
        details = {"left": est["google"]["daily_left"], "need": est["google"]["pages_max"]}
    elif code == "insufficient_allowance":
        details = {
            "need_usd": est["engines"]["usd_max"],
            "left_usd": est["allowance"]["left_usd"],
            "cap_usd": est["allowance"]["cap_usd"],
        }
    return CanseeException(
        blocker["message"], code=code, status_code=_ERROR_STATUS[code], details=details,
    )


def create_run(website, user, plan: CheckPlan, confirm: dict) -> CountryCheckRun:
    """Create and start a confirmed check. Raises the contract 2h errors:
    a blocker of the recomputed estimate, or 409 ``estimate_changed`` when
    it now needs more than the user confirmed."""
    from apps.websites.models import Website

    est = estimate(website, user, plan)
    blockers = {b["code"]: b for b in est["blockers"]}
    for code in _ERROR_ORDER:
        if code in blockers:
            raise _blocker_error(website, est, blockers[code])

    server = est["confirm"]
    if (
        server["google_pages"] > confirm["google_pages"]
        or server["engine_answers"] > confirm["engine_answers"]
        or Decimal(server["ai_usd_max"]) > confirm["ai_usd_max"]
    ):
        raise CanseeException(
            "The cost changed. Check the new estimate and confirm again.",
            code="estimate_changed", status_code=409, details={"estimate": est},
        )

    which = serp_google.provider() if plan.include_google else ""
    inline = settings.COUNTRY_CHECK_RUN_MODE == "inline"
    with transaction.atomic():
        # One active check per website: the website row serializes creates.
        list(Website.all_objects.select_for_update().filter(id=website.id).values_list("id", flat=True))
        current = active_run(website)
        if current is not None:
            raise _run_in_progress(current)
        run = CountryCheckRun.objects.create(
            website=website,
            created_by=user if getattr(user, "is_authenticated", False) else None,
            status=CountryCheckRun.STATUS_QUEUED,
            countries=plan.countries,
            prompt_ids=[str(bp.prompt_id) for bp in plan.prompts],
            brand_prompt_ids=[str(bp.id) for bp in plan.prompts],
            engines=plan.engines,
            include_google=plan.include_google,
            google_pages=plan.google_pages,
            provider=which,
            confirmed={
                "google_pages": confirm["google_pages"],
                "engine_answers": confirm["engine_answers"],
                "ai_usd_max": usd4(confirm["ai_usd_max"]),
            },
            estimate=est,
            country_status={c: QUEUED for c in plan.countries},
        )
        cells = []
        for bp in plan.prompts:
            for country in plan.countries:
                if plan.include_google:
                    geo = serp_google.geo_plan(country, which)
                    cells.append(CountryCheckResult(
                        run=run, website=website, brand_prompt=bp, country=country, engine=GOOGLE,
                        method=geo["method"], provider=which, proxy_country=geo["proxy_country"],
                    ))
                cells.extend(
                    CountryCheckResult(
                        run=run, website=website, brand_prompt=bp, country=country, engine=engine,
                        provider=country_geo.provider_key(engine),
                    )
                    for engine in plan.engines
                )
        CountryCheckResult.objects.bulk_create(cells)
        if not inline:
            run_id = run.id
            transaction.on_commit(lambda: dispatch(run_id))
    if inline:
        # on_commit never fires inside a test transaction.
        execute_run(run.id)
    run.refresh_from_db()
    return run


# -- dispatch and execution ------------------------------------------------


def dispatch(run_id) -> None:
    """Hand a created run to its executor (COUNTRY_CHECK_RUN_MODE)."""
    mode = settings.COUNTRY_CHECK_RUN_MODE
    if mode == "inline":
        execute_run(run_id)
        return
    try:
        if mode == "thread" or (mode == "auto" and getattr(settings, "CELERY_TASK_ALWAYS_EAGER", False)):
            threading.Thread(
                target=execute_run_safely, args=(str(run_id),),
                name=f"country-check-{run_id}", daemon=True,
            ).start()
            return
        from apps.search_gap.tasks import run_country_check

        run = CountryCheckRun.objects.filter(id=run_id).only("countries").first()
        for country in run.countries if run else []:
            run_country_check.delay(str(run_id), country)
    except Exception:
        # A run nobody will execute would hold the website's one active slot.
        logger.exception("country check %s could not be started", run_id)
        _fail_unstarted(run_id)


def _fail_unstarted(run_id) -> None:
    CountryCheckResult.objects.filter(run_id=run_id, status=QUEUED).update(
        status=FAILED, error_code="internal_error",
        error_message=CELL_MESSAGES["internal_error"], finished_at=timezone.now(),
    )
    finalize_if_done(run_id)


def execute_run_safely(run_id) -> None:
    """execute_run on a thread of its own: fresh connections, never raises."""
    close_old_connections()
    try:
        execute_run(run_id)
    except Exception:
        logger.exception("country check %s crashed", run_id)
    finally:
        connection.close()


def execute_run(run_id) -> None:
    """Every country of the run in plan order, then finalize."""
    run = CountryCheckRun.objects.filter(id=run_id).only("id", "status", "countries").first()
    if run is None or run.status in TERMINAL:
        return
    for country in run.countries:
        execute_country(run_id, country)
    finalize_if_done(run_id)


def _set_country_status(run_id, country: str, value: str) -> None:
    with transaction.atomic():
        run = (
            CountryCheckRun.objects.select_for_update()
            .only("id", "country_status").filter(id=run_id).first()
        )
        if run is None:
            return
        status = dict(run.country_status or {})
        status[country] = value
        CountryCheckRun.objects.filter(id=run_id).update(
            country_status=status, heartbeat_at=timezone.now(),
        )


def _keywords(website) -> list[str]:
    """What identifies the brand to the extractor: its name, topics and the
    competitor names, de-duplicated (prompt_crawler._website_keywords)."""
    out: list[str] = []
    name = getattr(website, "business_name", None) or website.name or ""
    if name:
        out.append(name)
    for topic in getattr(website, "topics", None) or []:
        if isinstance(topic, str) and topic.strip():
            out.append(topic.strip())
    for c in getattr(website, "competitors", None) or []:
        cname = c.get("name") if isinstance(c, dict) else (c if isinstance(c, str) else None)
        if cname and cname.strip():
            out.append(cname.strip())
    seen, deduped = set(), []
    for k in out:
        if k.lower() not in seen:
            seen.add(k.lower())
            deduped.append(k)
    return deduped


def _competitor_apexes(website) -> set[str]:
    out = set()
    for c in website.competitors or []:
        domain = c.get("domain") if isinstance(c, dict) else ""
        apex = extract_apex_domain(str(domain or "").strip().lower())
        if apex:
            out.add(apex)
    return out


class _Job:
    """What every cell of one country needs, shared by the worker threads."""

    def __init__(self, run, country: str):
        website = run.website
        self.run = run
        self.website = website
        self.country = country
        self.quota_key = str(website.id)
        self.quota = google_quota()
        self.apex = google_rank.site_apex(website.url)
        self.brand = getattr(website, "business_name", None) or website.name
        self.keywords = _keywords(website)
        self.competitor_apexes = _competitor_apexes(website)
        self._lock = threading.Lock()
        self._checked_at: float | None = None
        self._cancelled = False

    def cancelled(self) -> bool:
        """Stop was pressed (re-read at most every CANCEL_POLL_SECONDS)."""
        if self._cancelled:
            return True
        with self._lock:
            now = time.monotonic()
            if self._checked_at is None or now - self._checked_at >= CANCEL_POLL_SECONDS:
                self._checked_at = now
                self._cancelled = CountryCheckRun.objects.filter(
                    id=self.run.id, cancel_requested=True,
                ).exists()
        return self._cancelled


def _order(run):
    prompts = {bp_id: i for i, bp_id in enumerate(run.brand_prompt_ids)}
    engines = {GOOGLE: 0, **{key: i + 1 for i, key in enumerate(country_geo.ENGINE_KEYS)}}

    def key(cell):
        return (prompts.get(str(cell.brand_prompt_id), 999), engines.get(cell.engine, 99))

    return key


def execute_country(run_id, country: str) -> None:
    """One country's cells (the Celery task body), then finalize."""
    run = (
        CountryCheckRun.objects.select_related("website", "created_by")
        .filter(id=run_id).first()
    )
    if run is None or run.status in TERMINAL:
        return
    now = timezone.now()
    CountryCheckRun.objects.filter(id=run_id, status=CountryCheckRun.STATUS_QUEUED).update(
        status=CountryCheckRun.STATUS_RUNNING, started_at=now, heartbeat_at=now,
    )
    _set_country_status(run_id, country, RUNNING)

    cells = sorted(
        run.results.filter(country=country, status=QUEUED).select_related("brand_prompt__prompt"),
        key=_order(run),
    )
    job = _Job(run, country)
    google = [c for c in cells if c.engine == GOOGLE]
    engines = [c for c in cells if c.engine != GOOGLE]
    if settings.COUNTRY_CHECK_RUN_MODE == "inline":
        # Other threads cannot see a test transaction's rows.
        for cell in google + engines:
            _run_cell(job, cell)
    else:
        serp_workers = max(1, int(settings.COUNTRY_CHECK_SERP_CONCURRENCY))
        engine_workers = max(1, int(settings.COUNTRY_CHECK_ENGINE_CONCURRENCY))
        serp_pool = ThreadPoolExecutor(max_workers=serp_workers)
        engine_pool = ThreadPoolExecutor(max_workers=engine_workers)
        try:
            futures = [serp_pool.submit(_run_cell_in_thread, job, c) for c in google]
            futures += [engine_pool.submit(_run_cell_in_thread, job, c) for c in engines]
            wait(futures)
        except BaseException:
            # Raised in this thread while it waited: Celery's soft time limit.
            # No further cell starts and the caller ends the unfinished ones
            # (time_out_country); waiting on every queued cell here would run
            # into the hard limit, which kills the worker mid-run.
            for pool in (serp_pool, engine_pool):
                pool.shutdown(wait=False, cancel_futures=True)
            raise
        for pool in (serp_pool, engine_pool):
            pool.shutdown()
        for future in futures:
            if future.exception() is not None:
                # A cell left running is ended by the stale sweep.
                logger.error("country check %s: a %s worker failed", run_id, country,
                             exc_info=future.exception())

    _set_country_status(run_id, country, DONE)
    finalize_if_done(run_id)


def _run_cell_in_thread(job: _Job, cell) -> None:
    close_old_connections()
    try:
        _run_cell(job, cell)
    finally:
        connection.close()


def _error_fields(status: str, code: str, message: str | None = None) -> dict:
    return {
        "status": status,
        "error_code": code,
        "error_message": (message or CELL_MESSAGES[code])[:300],
    }


def _run_cell(job: _Job, cell) -> None:
    if job.cancelled():
        CountryCheckResult.objects.filter(id=cell.id, status=QUEUED).update(
            **_error_fields(CANCELLED, "cancelled"), finished_at=timezone.now(),
        )
        return
    started = CountryCheckResult.objects.filter(id=cell.id, status=QUEUED).update(
        status=RUNNING, started_at=timezone.now(),
    )
    if not started:
        return
    try:
        fields = _google_cell(job, cell) if cell.engine == GOOGLE else _engine_cell(job, cell)
    except Exception:
        logger.exception("country check %s: cell %s failed", job.run.id, cell.id)
        fields = _error_fields(FAILED, "internal_error")
    fields["finished_at"] = timezone.now()
    # Only over our own "running": a cell the stale sweep or a timed-out
    # country already ended keeps that end, as the finished run reports it.
    CountryCheckResult.objects.filter(id=cell.id, status=RUNNING).update(**fields)
    CountryCheckRun.objects.filter(id=job.run.id).update(heartbeat_at=timezone.now())


# -- Google cells ----------------------------------------------------------


def _retryable(code: str) -> bool:
    return code in _RETRY_CODES or code.startswith("http_5")


def _serp_message(code: str) -> str:
    if code.startswith("http_"):
        return f"Google search failed (HTTP {code[5:]})."
    return SERP_ERRORS.get(code, CELL_MESSAGES["serp_failed"])


def _search(query: str, *, max_results: int, start: int, country: str) -> dict:
    try:
        return serp_google.search(query, max_results=max_results, start=start, country=country)
    except Exception:
        logger.warning("country check: serp_google.search raised", exc_info=True)
        return {"error": "network"}


def _google_cell(job: _Job, cell) -> dict:
    which = serp_google.provider()
    if not which:
        return _error_fields(SKIPPED, "not_configured", GOOGLE_NOT_CONFIGURED)
    geo = serp_google.geo_plan(job.country, which)

    def page_search(query, *, max_results, start, gl="", hl=""):
        # find_rank's injectable search: one billed results page, quota first.
        if job.cancelled():
            return {"error": "cancelled"}
        if not job.quota.consume(job.quota_key):
            return {"error": "daily_limit"}
        payload = _search(query, max_results=max_results, start=start, country=job.country)
        if _retryable(str(payload.get("error") or "")):
            time.sleep(RETRY_SECONDS)
            if not job.cancelled() and job.quota.consume(job.quota_key):
                payload = _search(query, max_results=max_results, start=start, country=job.country)
        return payload

    query = " ".join(cell.brand_prompt.prompt.text.split())
    found = google_rank.find_rank(
        query, job.apex, max_depth=job.run.google_pages * google_rank.PAGE_SIZE, search=page_search,
    )
    pages = found["pages_fetched"]
    if pages:
        CountryCheckRun.objects.filter(id=job.run.id).update(
            google_pages_fetched=F("google_pages_fetched") + pages,
        )
    fields = {
        "method": geo["method"], "provider": which, "proxy_country": geo["proxy_country"],
        "pages_fetched": pages,
    }
    error = found["error"]
    if error == "daily_limit":
        return {**fields, **_error_fields(SKIPPED, "daily_limit")}
    if error == "cancelled":
        return {**fields, **_error_fields(CANCELLED, "cancelled")}
    if error == "serpapi_not_configured":
        return {**fields, **_error_fields(SKIPPED, "not_configured", GOOGLE_NOT_CONFIGURED)}
    if error:
        return {**fields, **_error_fields(FAILED, "serp_failed", _serp_message(error))}

    top = [
        {
            **row,
            "is_yours": google_rank.is_same_site(row.get("domain", ""), job.apex),
            "is_competitor": extract_apex_domain(row.get("domain", "")) in job.competitor_apexes,
        }
        for row in found["top_results"][:MAX_TOP_RESULTS]
    ]
    return {
        **fields,
        "status": DONE, "error_code": "", "error_message": "",
        "position": found["position"],
        # The depth actually searched: a later page that failed was not.
        "depth": pages * google_rank.PAGE_SIZE,
        "url": (found["url"] or "")[:2000] if found["position"] else "",
        "top_results": top,
        "ai_overview": google_rank.summarize_ai_overview(
            found["ai_overview"], brand=job.website.name, apex=job.apex,
            deferred=bool(found["ai_overview_deferred"]),
        ),
    }


# -- engine cells ------------------------------------------------------------


def engine_error(raw: str) -> tuple[str, str]:
    """(cell status, public code) for a provider error string; the mapping
    of the agent's research.public_engine_error."""
    e = (raw or "").strip().lower()
    if e.startswith("refusal:"):
        return FAILED, "refused"
    if e.startswith(("max_tokens:", "pause_turn:")):
        return FAILED, "cut_off"
    if e.startswith("empty_answer:"):
        return FAILED, "engine_error"
    if "allowance_check_failed" in e:
        # The allowance lookup broke (fail-closed): nothing is known to be spent.
        return FAILED, "internal_error"
    if "allowance" in e:
        return FAILED, "allowance_exceeded"
    if "not configured" in e or "not enabled" in e:
        return SKIPPED, "not_configured"
    if any(s in e for s in ("rate limit", "429", "quota", "overloaded", "circuit open")):
        return FAILED, "rate_limited"
    if "timeout" in e or "timed out" in e:
        return FAILED, "timeout"
    return FAILED, "engine_error"


def query_with_retry(instance, prompt_text: str, **kwargs):
    """One provider call, retried once on a transient failure (the rules of
    prompt_crawler._query_with_retry, plus no retry past the allowance).
    Returns the last ProviderResult, or None when every attempt raised."""
    last = None
    for attempt in (1, 2):
        try:
            result = instance.query(prompt_text, **kwargs)
        except Exception as exc:  # noqa: BLE001 - providers raise widely
            logger.warning("country check: %s raised (attempt %d): %s",
                           getattr(instance, "name", "provider"), attempt, exc)
            continue
        last = result
        if result.succeeded:
            return result
        err = (result.error or "").lower()
        if (
            err.startswith("refusal:") or "allowance" in err or "service_unavailable" in err
            or "not configured" in err or "not enabled" in err
        ):
            return result
    return last


def _rank(value) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and 0 < value < 32768 else None


def _brands(analysis: dict, brand: str) -> list[dict]:
    fold = (brand or "").casefold()
    out = []
    for c in analysis.get("competitors_mentioned") or []:
        name = (c.get("name") or "").strip() if isinstance(c, dict) else ""
        if not name or name.casefold() == fold:
            continue
        out.append({"name": name[:200], "position": _rank(c.get("position"))})
        if len(out) >= MAX_BRANDS:
            break
    return out


def _citations(result, analysis: dict) -> list[str]:
    out, seen = [], set()
    for url in [*(result.citations or []), *(analysis.get("citations") or [])]:
        if isinstance(url, str) and url.startswith(("http://", "https://")) and url not in seen:
            seen.add(url)
            out.append(url[:2000])
            if len(out) >= MAX_CITATIONS:
                break
    return out


def _engine_cell(job: _Job, cell) -> dict:
    from apps.llm_ranking.providers import PROVIDERS

    key = country_geo.provider_key(cell.engine)
    fields = {"provider": key}
    cls = PROVIDERS.get(key)
    instance = cls() if cls is not None else None
    if instance is None or not instance.is_configured():
        return {**fields, **_error_fields(SKIPPED, "not_configured")}
    fields["model_id"] = str(getattr(instance, "model", "") or "")[:64]

    run = job.run
    result = query_with_retry(
        instance, cell.brand_prompt.prompt.text.strip(),
        system_prompt=regions.country_system_prompt(key, job.country),
        user=run.created_by, website=job.website, audit_id=run.audit_id,
        role="country_check", region=regions.region_for_country(job.country),
        module="llm_ranking",
        extra_metadata={"actor": "user", "feature": "country_checks", "country": job.country},
    )
    if result is None:
        return {**fields, **_error_fields(FAILED, "engine_error")}
    text = (result.text or "").strip() if result.succeeded else ""
    if not text:
        # An empty answer is not a measurement: it never reads as "not named".
        status, code = engine_error(result.error if not result.succeeded else "empty_answer:")
        return {**fields, **_error_fields(status, code)}

    analysis = HaikuExtractionService.extract(
        response_text=text, brand_name=job.brand, keywords=job.keywords,
        user=run.created_by, website=job.website, audit_id=run.audit_id,
    )
    mentioned = bool(analysis.get("is_mentioned"))
    sentiment = analysis.get("sentiment")
    return {
        **fields,
        "status": DONE, "error_code": "", "error_message": "",
        "method": regions.answer_country_method(key, result),
        "mentioned": mentioned,
        "rank": _rank(analysis.get("mention_rank")) if mentioned else None,
        "sentiment": sentiment if mentioned and sentiment in SENTIMENTS else "",
        "brands": _brands(analysis, job.brand),
        "citations": _citations(result, analysis),
        "answer_text": text,
        "extraction_model": str(analysis.get("extraction_model") or "")[:100],
    }


# -- finish, stop, sweep -----------------------------------------------------


def _status_counts(run) -> dict:
    return Counter(run.results.values_list("status", flat=True))


def _most_common_error(run) -> tuple[str, str]:
    pairs = Counter(run.results.exclude(error_code="").values_list("error_code", "error_message"))
    if not pairs:
        return "internal_error", CELL_MESSAGES["internal_error"]
    (code, message), _ = pairs.most_common(1)[0]
    return code, message


def finalize_if_done(run_id):
    """End the run once no cell is queued or running (contract 2f)."""
    with transaction.atomic():
        run = CountryCheckRun.objects.select_for_update().filter(id=run_id).first()
        if run is None or run.status in TERMINAL:
            return run
        counts = _status_counts(run)
        if counts.get(QUEUED) or counts.get(RUNNING):
            return run
        done, total = counts.get(DONE, 0), sum(counts.values())
        if run.cancel_requested and counts.get(CANCELLED):
            status = CountryCheckRun.STATUS_CANCELLED
        elif total and done == total:
            status = CountryCheckRun.STATUS_DONE
        elif done:
            status = CountryCheckRun.STATUS_PARTIAL
        else:
            status = CountryCheckRun.STATUS_FAILED
            run.error_code, run.error_message = _most_common_error(run)
        now = timezone.now()
        run.status = status
        run.finished_at = now
        run.heartbeat_at = now
        run.ai_cost_usd = ledger_cost(run)
        run.country_status = {c: DONE for c in run.countries}
        run.save(update_fields=[
            "status", "finished_at", "heartbeat_at", "ai_cost_usd", "country_status",
            "error_code", "error_message", "updated_at",
        ])
        return run


def cancel_run(run) -> CountryCheckRun:
    """Stop a check: workers end their remaining cells ``cancelled``. A run
    no worker holds yet (queued) ends at once. Idempotent; a finished run is
    returned unchanged."""
    if run.status in TERMINAL:
        return run
    CountryCheckRun.objects.filter(id=run.id).update(cancel_requested=True)
    run.refresh_from_db()
    if run.status == CountryCheckRun.STATUS_QUEUED:
        run.results.filter(status=QUEUED).update(
            **_error_fields(CANCELLED, "cancelled"), finished_at=timezone.now(),
        )
        finalize_if_done(run.id)
        run.refresh_from_db()
    return run


def time_out_country(run_id, country: str) -> None:
    """A country job ran out of time (Celery soft limit): its unfinished
    cells fail, then the run may finish."""
    now = timezone.now()
    unfinished = CountryCheckResult.objects.filter(
        run_id=run_id, country=country, status__in=(QUEUED, RUNNING),
    )
    unfinished.exclude(engine=GOOGLE).update(**_error_fields(FAILED, "timeout"), finished_at=now)
    unfinished.filter(engine=GOOGLE).update(**_error_fields(FAILED, "interrupted"), finished_at=now)
    _set_country_status(run_id, country, DONE)
    finalize_if_done(run_id)


def sweep_stale(website) -> int:
    """Interrupt the website's checks that stopped reporting (no heartbeat
    for COUNTRY_CHECK_STALE_MINUTES). Called on every read of the runs."""
    cutoff = timezone.now() - timedelta(minutes=settings.COUNTRY_CHECK_STALE_MINUTES)
    stale = list(
        CountryCheckRun.objects
        .filter(website=website, status__in=ACTIVE)
        .annotate(last_seen=Coalesce("heartbeat_at", "started_at", "created_at"))
        .filter(last_seen__lt=cutoff)
        .values_list("id", flat=True)
    )
    for run_id in stale:
        with transaction.atomic():
            run = CountryCheckRun.objects.select_for_update().filter(id=run_id).first()
            if run is None or run.status in TERMINAL:
                continue
            if (run.heartbeat_at or run.started_at or run.created_at) >= cutoff:
                continue
            now = timezone.now()
            run.results.filter(status__in=(QUEUED, RUNNING)).update(
                **_error_fields(FAILED, "interrupted"), finished_at=now,
            )
            run.status = CountryCheckRun.STATUS_INTERRUPTED
            run.finished_at = now
            run.error_code, run.error_message = "interrupted", RUN_INTERRUPTED
            run.ai_cost_usd = ledger_cost(run)
            run.country_status = {c: DONE for c in run.countries}
            run.save(update_fields=[
                "status", "finished_at", "error_code", "error_message", "ai_cost_usd",
                "country_status", "updated_at",
            ])
    return len(stale)
