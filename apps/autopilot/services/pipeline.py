"""The Autopilot run, stage by stage (backend 4c and 4e).

plan -> engines -> google -> analysis -> finalize -> finished. Every stage
change is a conditional update (``filter(id=..., stage=old)``), so a
duplicated task does nothing; the engines and Google steps claim their
prompt rows with ``select_for_update(skip_locked=True)``, heartbeat per
prompt and hand over to a continuation after AUTOPILOT_STEP_SECONDS.

Spend stays bounded three ways: the owner's plan (the tracked-prompt cap
picks the prompts, ``tracked_prompts.runnable_brand_prompts``), Autopilot's
line in the owner's AI allowance (the cap less AUTOPILOT_RESERVE_PCT,
checked when the run is planned, before each prompt and before the
analysis) and the daily Google page quotas (per project and per
deployment). Every answer goes through ``EngineScan``, so the provider's
own spend wall applies too.

Runs execute by AUTOPILOT_RUN_MODE: ``inline`` (tests, the management
command) drives every stage in the caller's thread, ``thread`` on a daemon
thread, ``celery`` as one task per stage, and ``auto`` picks Celery, or a
thread when Celery runs eagerly (dev).
"""

from __future__ import annotations

import logging
import threading
import time as clock
from concurrent.futures import ThreadPoolExecutor, wait
from contextlib import contextmanager
from datetime import timedelta
from decimal import ROUND_DOWN, Decimal, InvalidOperation

from django.core.cache import cache
from django.db import IntegrityError, close_old_connections, connection, transaction
from django.db.models import Count, F, Q, Sum
from django.utils import timezone

from apps.autopilot.models import (
    CADENCE_WEEKLY,
    Autopilot,
    AutopilotRun,
    AutopilotRunPrompt,
)
from apps.autopilot.services import settings as ap
from core.exceptions import CanseeException
from core.quota import DailyQuota

logger = logging.getLogger("apps")

Run = AutopilotRun
Row = AutopilotRunPrompt
ZERO = Decimal("0")

QUEUED, RUNNING = Run.STATUS_QUEUED, Run.STATUS_RUNNING
PLAN, ENGINES, GOOGLE = Run.STAGE_PLAN, Run.STAGE_ENGINES, Run.STAGE_GOOGLE
ANALYSIS, FINALIZE, FINISHED = Run.STAGE_ANALYSIS, Run.STAGE_FINALIZE, Run.STAGE_FINISHED

# What a step reports to its driver.
ADVANCED, HANDOVER, IDLE, WAITING = "advanced", "handover", "idle", "waiting"

CANCEL_POLL_SECONDS = 2.0
ANALYSIS_POLL_SECONDS = 5.0
# A prompt row still running after this was left by a crashed worker.
ROW_STALE_MINUTES = 10
# A stage with no heartbeat this long is handed to a new worker.
RESUME_MINUTES = 10
MAX_RESUMES = 3
# Claims of one row before a crashed worker's row ends failed.
MAX_ATTEMPTS = 2
DISPATCH_LOCK = "autopilot:dispatch"
DISPATCH_LOCK_SECONDS = 240
SLOT_LOCK = "autopilot:slots"
GOOGLE_DEPTH = 10
GOOGLE_RETRY_CODES = ("empty_results", "network", "http_429")
ALLOWANCE_CODES = ("allowance_exceeded", "reserve_reached")

RUN_TODAY_EXISTS = "Today's run already happened. The next one is tomorrow at {time}."
ALLOWANCE_EXCEEDED = "You have used your monthly AI allowance."
RESERVE_REACHED = (
    "Autopilot keeps the last {pct}% of your AI allowance for your own questions. "
    "Runs resume {date}."
)
NOTHING_TO_RUN = "Track a prompt first."
NOT_CONFIGURED = "AI engines aren't available yet."
ESTIMATE_CHANGED = "The cost changed. Check the new estimate and confirm again."
CONFIRM_MESSAGE = "Confirm the estimate before running."
RUN_MESSAGES = {
    "no_answers": "No engine answered.",
    "interrupted": "The run stopped before it finished.",
    "internal_error": "Something went wrong with this run.",
    "engines_off": "AI engines are off in Autopilot.",
    "nothing_to_run": NOTHING_TO_RUN,
    "not_configured": NOT_CONFIGURED,
    "allowance_exceeded": "The monthly AI allowance is used up.",
}
BLOCKER_STATUS = {
    "plan_required": 403, "run_today_exists": 409, "nothing_to_run": 400,
    "not_configured": 400, "allowance_exceeded": 403, "reserve_reached": 403,
}


def _blocker(code: str, message: str) -> dict:
    return {"code": code, "message": message}


def reserve_message(state: dict) -> str:
    return RESERVE_REACHED.format(pct=state["reserve_pct"], date=ap.month_day(state["resets_on"]))


def today_exists_message(autopilot) -> str:
    return RUN_TODAY_EXISTS.format(time=ap.hhmm(autopilot.run_time))


# -- engines and bounds ---------------------------------------------------------------


def configured_engines() -> list[dict]:
    """``[{key, label, provider}]`` of the engines with an API key, in
    display order."""
    from apps.agent.tools.research import engine_catalog

    return [
        {"key": e["key"], "label": e["label"], "provider": e["provider"]}
        for e in engine_catalog() if e["configured"]
    ]


def asks_for(brand_prompt, engines: list[dict]) -> list[tuple[str, str | None]]:
    """(provider, variant) for each answer a prompt asks: its selected
    model variants whose engine is configured, else every configured
    engine's default model (variant None)."""
    from apps.llm_ranking.providers import PROVIDERS, parse_variant

    configured = [e["provider"] for e in engines]
    selected = list(brand_prompt.model_variants or [])
    if not selected:
        return [(provider, None) for provider in configured]
    out, seen = [], set()
    for raw in selected:
        variant = str(raw or "").strip()
        parsed = parse_variant(variant) if variant else None
        if parsed is None or variant in seen:
            continue
        if parsed[0] not in PROVIDERS or parsed[0] not in configured:
            continue
        seen.add(variant)
        out.append((parsed[0], variant))
    return out


def ask_bound(provider: str) -> Decimal:
    """Upper bound of one answer and its extraction."""
    from apps.citations.services.url_analytics import model_key
    from apps.search_gap.services.country_checks import answer_usd_max

    return answer_usd_max(model_key(provider))


def measured_per_prompt(website) -> Decimal | None:
    """What one prompt cost in the previous finished run, when it asked at
    least 5 prompts; None until then."""
    prev = (
        Run.objects.filter(website=website, status__in=(Run.STATUS_DONE, Run.STATUS_PARTIAL))
        .order_by("-created_at").first()
    )
    if prev is None or prev.prompts_done < 5:
        return None
    return Decimal(str(prev.ai_cost_usd or 0)) / prev.prompts_done


def _rotated(items: list, offset: int) -> list:
    if not items:
        return []
    start = offset % len(items)
    return items[start:] + items[:start]


def _serp_provider() -> str:
    from apps.citations.services import serp_google

    return serp_google.provider()


def _credits_per_page() -> int:
    from django.conf import settings

    return int(settings.SCRAPERAPI_CREDITS_PER_SEARCH)


def credits_for(run) -> int | None:
    """ScraperAPI credits a run's billed pages cost; None with SerpAPI."""
    from apps.citations.services import serp_google

    if (run.plan or {}).get("google_provider") != serp_google.PROVIDER_SCRAPERAPI:
        return None
    return run.google_pages * _credits_per_page()


def make_plan(autopilot, website, runnable: list) -> dict:
    """What a run of ``website`` does now, with no side effects: the
    engines, the prompts that fit under Autopilot's line (starting at
    ``prompt_offset`` when only some fit), the bounds and the Google work.
    ``skip`` names why the whole run would be skipped."""
    engines = configured_engines() if autopilot.run_engines else []
    asks = {bp.id: asks_for(bp, engines) for bp in runnable}
    bounds = {bp.id: sum((ask_bound(p) for p, _v in asks[bp.id]), ZERO) for bp in runnable}
    askable = [bp for bp in runnable if asks[bp.id]]
    measured = measured_per_prompt(website)
    state = ap.allowance_state(website.user)
    provider = _serp_provider()
    google = bool(autopilot.run_google and provider and runnable)

    fit, skip, limited, offset = list(askable), None, False, autopilot.prompt_offset
    if not runnable:
        skip = "nothing_to_run"
    elif askable and not state["unlimited"]:
        budget, total, fit = state["usable"], ZERO, []
        for bp in _rotated(askable, autopilot.prompt_offset):
            cost = measured if measured is not None else bounds[bp.id]
            if total + cost > budget:
                break
            total += cost
            fit.append(bp)
        if not fit:
            skip = "allowance_exceeded" if state["spent"] >= state["cap"] else "reserve_reached"
        elif len(fit) < len(askable):
            limited = True
            offset = (autopilot.prompt_offset + len(fit)) % len(askable)
        else:
            fit = list(askable)
    elif not askable and not google:
        skip = "not_configured" if autopilot.run_engines else "engines_off"

    usd_max = sum((bounds[bp.id] for bp in fit), ZERO) if not skip else ZERO
    return {
        "runnable": runnable,
        "fit": fit if not skip else [],
        "asks": asks,
        "askable": askable,
        "engines": engines,
        "skip": skip,
        "limited": limited,
        "next_offset": offset,
        "google": google and not skip,
        "google_provider": provider,
        "answers_max": sum(len(asks[bp.id]) for bp in fit) if not skip else 0,
        "usd_max": usd_max,
        "usd_expected": (measured * len(fit)) if measured is not None and not skip else None,
        "bound_usd": sum((bounds[bp.id] for bp in askable), ZERO),
        "state": state,
    }


def run_bound(autopilot, website) -> dict:
    """At most what one run costs before the first measures it: every
    askable tracked prompt and one Google page each."""
    from apps.billing.services import tracked_prompts

    runnable, _over = tracked_prompts.runnable_brand_prompts(website)
    plan = make_plan(autopilot, website, runnable)
    pages = len(runnable) if autopilot.run_google and plan["google_provider"] else 0
    return {"ai_usd_max": plan["bound_usd"], "pages_max": pages}


# -- payloads -----------------------------------------------------------------------


def _share(named: int, answered: int) -> float | None:
    return round(100 * named / answered, 1) if answered else None


def _planned(run) -> bool:
    return not (run.status in Run.ACTIVE and run.stage == PLAN)


def _analysis_status(run) -> str:
    if run.analysis_status:
        return run.analysis_status
    if not _planned(run):
        return "pending"
    return "pending" if (run.plan or {}).get("analysis") else "off"


def _google_status(run) -> str:
    if run.google_status:
        return run.google_status
    if not _planned(run):
        return "pending"
    return "pending" if (run.plan or {}).get("google") else "off"


def ledger_cost(run) -> Decimal:
    """The usage ledger's cost of a run (contract 7): its answers (tagged
    ``autopilot_run``), every audit its prompts point to (the extractions)
    and its analysis run."""
    from apps.accounts.models import AITokenUsage

    audits = [
        str(a) for a in Row.objects.filter(run_id=run.id, audit__isnull=False)
        .values_list("audit_id", flat=True)
    ]
    match = Q(metadata__autopilot_run=str(run.id))
    if audits:
        match |= Q(metadata__audit_id__in=audits)
    if run.analysis_run_id:
        match |= Q(metadata__audit_id=f"insights-run:{run.analysis_run_id}")
    total = (
        AITokenUsage.objects.filter(created_at__gte=run.created_at).filter(match)
        .aggregate(total=Sum("estimated_cost_usd"))["total"]
    )
    return total or ZERO


def run_summary(run) -> dict:
    """RunSummary (contract 3c)."""
    finished = run.status in Run.TERMINAL
    analysis = run.analysis_run
    analysis_answers = analysis.units_total if analysis is not None else None
    return {
        "id": str(run.id),
        "local_date": run.local_date.isoformat(),
        "trigger": run.trigger,
        "status": run.status,
        "stage": run.stage,
        "started_at": ap.iso(run.started_at),
        "finished_at": ap.iso(run.finished_at),
        "prompts": {
            "planned": run.prompts_planned,
            "done": run.prompts_done,
            "failed": run.prompts_failed,
            "skipped": run.prompts_skipped,
            "over_limit": run.over_limit,
        },
        "answers": {
            "asked": run.answers_asked,
            "answered": run.answers_answered,
            "named": run.answers_named,
            "share_pct": _share(run.answers_named, run.answers_answered),
        },
        "change_pts": run.change_pts,
        "google": {
            "status": _google_status(run),
            "measured": run.google_measured,
            "checked": run.google_checked,
            "reused": run.google_reused,
            "page_one": run.google_page_one,
            "pages": run.google_pages,
            "credits": credits_for(run),
        },
        "analysis": {
            "status": _analysis_status(run),
            "answers": analysis_answers,
            "new_contradicted": run.new_contradicted,
        },
        # Stored when the run ended; read live from the ledger while it runs.
        "cost": {"ai_usd": ap.usd4(run.ai_cost_usd if finished else ledger_cost(run))},
        "text": {
            "status": run.text_status,
            "reason": run.text_reason,
            "due_at": ap.iso(run.text_due_at),
            "sent_at": ap.iso(run.text_sent_at),
        },
        "error": {"code": run.error_code, "message": run.error_message} if run.error_code else None,
    }


def row_payload(row) -> dict:
    check = row.google_check
    position = check.position if check is not None and not check.error else None
    measured = row.google_status in (Row.GOOGLE_CHECKED, Row.GOOGLE_REUSED) and check is not None
    return {
        "brand_prompt_id": str(row.brand_prompt_id),
        "prompt_id": str(row.brand_prompt.prompt_id),
        "text": row.brand_prompt.prompt.text,
        "position": row.position,
        "status": row.status,
        "error_code": row.error_code,
        "answers": {
            "asked": row.answers_asked,
            "answered": row.answers_answered,
            "named": row.answers_named,
        },
        "engines": row.engines or {},
        "google": {
            "status": row.google_status or "pending",
            "position": position,
            "page_one": (position is not None and position <= GOOGLE_DEPTH) if measured else None,
            "pages": row.google_pages,
        },
    }


def run_detail(run) -> dict:
    """RunSummary with the run's prompt rows (``prompt_rows``; RunSummary
    already uses ``prompts`` for its counts) and the text it built."""
    rows = (
        Row.objects.filter(run=run)
        .select_related("brand_prompt", "brand_prompt__prompt", "google_check")
        .order_by("position")
    )
    return {
        **run_summary(run),
        "prompt_rows": [row_payload(row) for row in rows],
        "text_body": run.text_body,
    }


def last_measured_run(website):
    """The newest run that did paid work."""
    return (
        Run.objects.filter(
            website=website,
            status__in=(Run.STATUS_DONE, Run.STATUS_PARTIAL, Run.STATUS_FAILED,
                        Run.STATUS_INTERRUPTED),
        )
        .filter(Q(answers_asked__gt=0) | Q(google_pages__gt=0))
        .order_by("-created_at").first()
    )


# -- Run now --------------------------------------------------------------------------


def run_estimate(website, user=None) -> dict:
    """The Run now estimate (contract 3a), no side effects. Spend goes to
    the project's owner, whoever asks."""
    from apps.billing.services import tracked_prompts

    autopilot = ap.get_or_build(website)
    runnable, over = tracked_prompts.runnable_brand_prompts(website)
    plan = make_plan(autopilot, website, runnable)
    state = plan["state"]
    blockers = []
    if not ap.plan_allows(website.user, "autopilot"):
        blockers.append(_blocker("plan_required", ap.PLAN_REQUIRED))
    if Run.objects.filter(website=website, local_date=ap.local_today(autopilot)).exists():
        blockers.append(_blocker("run_today_exists", today_exists_message(autopilot)))
    skip = plan["skip"]
    if skip == "nothing_to_run":
        blockers.append(_blocker("nothing_to_run", NOTHING_TO_RUN))
    elif skip in ("not_configured", "engines_off"):
        blockers.append(_blocker("not_configured", NOT_CONFIGURED))
    elif skip == "allowance_exceeded":
        blockers.append(_blocker("allowance_exceeded", ALLOWANCE_EXCEEDED))
    elif skip == "reserve_reached":
        blockers.append(_blocker("reserve_reached", reserve_message(state)))

    provider = plan["google_provider"]
    pages = len(runnable) if plan["google"] else 0
    from apps.citations.services import serp_google

    scraper = provider == serp_google.PROVIDER_SCRAPERAPI
    usd_max = plan["usd_max"]
    unlimited = state["unlimited"]
    ask = bool(plan["fit"])
    return {
        "prompts": len(plan["fit"]) if ask else (len(runnable) if plan["google"] else 0),
        "over_limit": over,
        "limited_by": "allowance" if plan["limited"] else None,
        "engines": [{"key": e["key"], "label": e["label"]} for e in plan["engines"]],
        "answers_max": plan["answers_max"],
        "usd_max": ap.usd4(usd_max),
        "usd_expected": (
            ap.usd4(plan["usd_expected"]) if plan["usd_expected"] is not None else None
        ),
        "pages_max": pages,
        "credits_max": pages * _credits_per_page() if scraper else None,
        "allowance": {
            "unlimited": unlimited,
            "cap_usd": None if unlimited else ap.usd2(state["cap"]),
            "left_usd": None if unlimited else ap.usd2(state["left"], ROUND_DOWN),
            "reserve_pct": state["reserve_pct"],
            "usable_usd": None if unlimited else ap.usd2(state["usable"], ROUND_DOWN),
        },
        "blockers": blockers,
        "can_run": not blockers,
        "confirm": {
            "answers": plan["answers_max"],
            "ai_usd_max": ap.usd4(usd_max),
            "google_pages": pages,
        },
    }


def _parse_confirm(raw) -> dict:
    if not isinstance(raw, dict):
        raise ap.invalid("confirm", CONFIRM_MESSAGE)
    out = {}
    for name in ("answers", "google_pages"):
        value = raw.get(name)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ap.invalid("confirm", CONFIRM_MESSAGE)
        out[name] = value
    try:
        usd = Decimal(str(raw.get("ai_usd_max")))
        if not usd.is_finite() or usd < 0:
            raise ValueError
        ap.usd4(usd)
    except (InvalidOperation, ValueError):
        raise ap.invalid("confirm", CONFIRM_MESSAGE) from None
    out["ai_usd_max"] = usd
    return out


def create_manual_run(website, user, confirm) -> Run:
    """Run now (contract 3): today's run, started at once when a slot is
    free, else queued for the next dispatcher tick."""
    est = run_estimate(website, user)
    if est["blockers"]:
        first = est["blockers"][0]
        raise CanseeException(
            first["message"], code=first["code"],
            status_code=BLOCKER_STATUS.get(first["code"], 400),
        )
    confirmed = _parse_confirm(confirm)
    server = est["confirm"]
    if (
        server["answers"] > confirmed["answers"]
        or Decimal(server["ai_usd_max"]) > confirmed["ai_usd_max"]
        or server["google_pages"] > confirmed["google_pages"]
    ):
        raise CanseeException(
            ESTIMATE_CHANGED, code="estimate_changed", status_code=409, details={"estimate": est},
        )
    autopilot = ap.get_or_build(website)
    try:
        with transaction.atomic():
            run = Run.objects.create(
                website=website,
                local_date=ap.local_today(autopilot),
                trigger=Run.TRIGGER_MANUAL,
                created_by=user if getattr(user, "is_authenticated", False) else None,
                status=QUEUED,
                plan={"confirmed": {
                    "answers": confirmed["answers"],
                    "ai_usd_max": ap.usd4(confirmed["ai_usd_max"]),
                    "google_pages": confirmed["google_pages"],
                }},
            )
    except IntegrityError:
        raise CanseeException(
            today_exists_message(autopilot), code="run_today_exists", status_code=409,
        ) from None
    if claim_slot(run.id):
        start_soon(run.id)
    run.refresh_from_db()
    return run


def cancel_run(run) -> Run:
    """Stop a run: no text is sent for it. A run no worker holds yet ends at
    once; a running one ends when its workers see Stop."""
    from apps.insights.services import runs as insights_runs

    if run.status in Run.TERMINAL:
        return run
    Run.objects.filter(id=run.id).update(cancel_requested=True)
    run.refresh_from_db()
    if run.status == QUEUED or run.stage in (ANALYSIS, FINALIZE):
        if run.analysis_run is not None:
            try:
                insights_runs.cancel_run(run.analysis_run)
            except Exception:
                logger.exception("autopilot: could not stop the analysis of run %s", run.id)
        finalize_run(run.id)
        deliver_due(run.id)
    run.refresh_from_db()
    return run


def sweep_stale(website) -> int:
    """End the project's runs that stopped reporting for AUTOPILOT_STALE_MINUTES
    (no beat to do it, as in dev). Called by the views that show runs."""
    cutoff = timezone.now() - timedelta(minutes=int(ap.conf("AUTOPILOT_STALE_MINUTES")))
    stale = [
        run.id for run in Run.objects.filter(website=website, status=RUNNING)
        if (run.heartbeat_at or run.started_at or run.created_at) < cutoff
    ]
    for run_id in stale:
        finalize_run(run_id, interrupted=True)
        deliver_due(run_id)
    return len(stale)


# -- slots and modes ------------------------------------------------------------------


_forced = threading.local()


@contextmanager
def forced_mode(mode: str):
    """Run in ``mode`` on this thread whatever AUTOPILOT_RUN_MODE says (the
    management command runs inline)."""
    previous = getattr(_forced, "mode", None)
    _forced.mode = mode
    try:
        yield
    finally:
        _forced.mode = previous


def run_mode() -> str:
    from django.conf import settings

    forced = getattr(_forced, "mode", None)
    if forced:
        return forced
    mode = ap.conf("AUTOPILOT_RUN_MODE")
    if mode == "auto":
        return "thread" if getattr(settings, "CELERY_TASK_ALWAYS_EAGER", False) else "celery"
    return mode if mode in ("inline", "thread", "celery") else "celery"


def _inline() -> bool:
    return run_mode() == "inline"


def _lock_slots() -> None:
    """Serialize slot claims until the transaction ends (Postgres)."""
    if connection.vendor != "postgresql":
        return
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", [SLOT_LOCK])


def active_count() -> int:
    """Runs holding a slot: running, except those only waiting for their
    analysis run (which runs as an insights run of its own)."""
    return Run.objects.filter(status=RUNNING).exclude(stage=ANALYSIS).count()


def _free_slot() -> bool:
    return active_count() < max(1, int(ap.conf("AUTOPILOT_MAX_ACTIVE_RUNS")))


def claim_slot(run_id) -> bool:
    """Start a queued run when one of the AUTOPILOT_MAX_ACTIVE_RUNS slots is free."""
    now = timezone.now()
    with transaction.atomic():
        _lock_slots()
        if not _free_slot():
            return False
        return bool(Run.objects.filter(id=run_id, status=QUEUED).update(
            status=RUNNING, started_at=now, heartbeat_at=now, updated_at=now,
        ))


def start_soon(run_id, *, mode: str | None = None) -> None:
    """Kick a run once the caller's transaction commits (inline: now)."""
    mode = mode or run_mode()
    if mode == "inline":
        kick(run_id, mode=mode)
        return
    transaction.on_commit(lambda: kick(run_id, mode=mode))


def kick(run_id, *, mode: str | None = None) -> None:
    """Hand a running run's current stage to its executor."""
    mode = mode or run_mode()
    if mode == "inline":
        drive(run_id)
        return
    if mode == "thread":
        threading.Thread(
            target=_drive_safely, args=(str(run_id),), name=f"autopilot-{run_id}", daemon=True,
        ).start()
        return
    from apps.autopilot import tasks

    run = Run.objects.filter(id=run_id).values("status", "stage", "analysis_status").first()
    if run is None or run["status"] != RUNNING:
        return
    try:
        stage = run["stage"]
        if stage == PLAN:
            tasks.start_run.delay(str(run_id))
        elif stage == ENGINES:
            tasks.run_engines.delay(str(run_id))
        elif stage == GOOGLE:
            tasks.run_google.delay(str(run_id))
        elif stage == ANALYSIS:
            if not run["analysis_status"] and start_analysis(run_id) == ADVANCED:
                tasks.finalize_run.delay(str(run_id))
        elif stage == FINALIZE:
            tasks.finalize_run.delay(str(run_id))
    except Exception:
        # The dispatcher resumes a stage that stops reporting.
        logger.exception("autopilot: run %s could not be handed on", run_id)


def _drive_safely(run_id) -> None:
    close_old_connections()
    try:
        drive(run_id)
    except Exception:
        logger.exception("autopilot: run %s crashed", run_id)
    finally:
        connection.close()


def drive(run_id, *, wait_for_analysis: bool = True) -> Run | None:
    """Run every stage of a running run in this thread, then send its text
    when it is due (inline and thread modes)."""
    for _ in range(10_000):
        run = Run.objects.filter(id=run_id).only(
            "status", "stage", "analysis_status", "cancel_requested",
        ).first()
        if run is None or run.status != RUNNING:
            break
        stage = run.stage
        if stage == PLAN:
            if not start_run(run_id):
                break
        elif stage == ENGINES:
            if run_engines(run_id) == IDLE:
                break
        elif stage == GOOGLE:
            if run_google(run_id) == IDLE:
                break
        elif stage == ANALYSIS:
            outcome = start_analysis(run_id) if not run.analysis_status else WAITING
            if outcome == WAITING:
                if not wait_for_analysis:
                    break
                _wait_for_analysis(run_id)
            elif outcome == IDLE:
                break
        elif stage == FINALIZE:
            finalize_run(run_id)
        else:
            break
    deliver_due(run_id)
    return Run.objects.filter(id=run_id).first()


def deliver_due(run_id, *, mode: str | None = None) -> None:
    """Send a run's text now when it is waiting and due."""
    from apps.autopilot.services import text

    due = Run.objects.filter(
        id=run_id, text_status=Run.TEXT_WAITING, text_due_at__lte=timezone.now(),
    ).exists()
    if not due:
        return
    mode = mode or run_mode()
    if mode == "celery":
        from apps.autopilot import tasks

        try:
            tasks.deliver_text.delay(str(run_id))
        except Exception:
            logger.exception("autopilot: the text of run %s could not be queued", run_id)
        return
    text.deliver_text(run_id)


# -- stages -----------------------------------------------------------------------------


def _advance(run_id, old: str, new: str) -> bool:
    now = timezone.now()
    return bool(Run.objects.filter(id=run_id, status=RUNNING, stage=old).update(
        stage=new, heartbeat_at=now, updated_at=now,
    ))


def _after_engines(run) -> str:
    if run.cancel_requested:
        return FINALIZE
    plan = run.plan or {}
    if plan.get("google"):
        return GOOGLE
    return ANALYSIS if plan.get("analysis") else FINALIZE


def _after_google(run) -> str:
    if run.cancel_requested:
        return FINALIZE
    return ANALYSIS if (run.plan or {}).get("analysis") else FINALIZE


def _heartbeat(run_id) -> None:
    Run.objects.filter(id=run_id, status=RUNNING).update(heartbeat_at=timezone.now())


def start_run(run_id) -> bool:
    """The plan stage: pick the prompts, create their rows, move on."""
    from apps.billing.services import tracked_prompts

    with transaction.atomic():
        run = (
            Run.objects.select_for_update().filter(id=run_id, status=RUNNING, stage=PLAN).first()
        )
        if run is None:
            return False
        website = run.website
        autopilot = ap.get_or_build(website)
        now = timezone.now()
        if run.cancel_requested:
            Run.objects.filter(id=run.id).update(stage=FINALIZE, heartbeat_at=now)
            return True
        runnable, over = tracked_prompts.runnable_brand_prompts(website)
        plan = make_plan(autopilot, website, runnable)
        skip = plan["skip"]
        fit_ids = {bp.id for bp in plan["fit"]}
        ask = bool(fit_ids)
        state = plan["state"]
        reserve_code = "reserve_reached" if state["reserve_pct"] else "allowance_exceeded"
        rows, skipped = [], 0
        for position, bp in enumerate(runnable):
            if bp.id in fit_ids:
                status, code = Row.STATUS_QUEUED, ""
            else:
                status = Row.STATUS_SKIPPED
                skipped += 1
                if skip in ALLOWANCE_CODES:
                    code = skip
                elif not autopilot.run_engines:
                    code = "engines_off"
                elif not plan["asks"][bp.id]:
                    code = "not_configured"
                else:
                    code = reserve_code
            rows.append(Row(
                run=run, brand_prompt=bp, position=position, status=status, error_code=code,
                google_status=(
                    Row.GOOGLE_PENDING if plan["google"] and not skip else Row.GOOGLE_OFF
                ),
            ))
        Row.objects.bulk_create(rows)

        stored = dict(run.plan or {})
        stored.update({
            "engines": [{k: e[k] for k in ("key", "label", "provider")} for e in plan["engines"]],
            "ask": ask,
            "google": plan["google"],
            "google_provider": plan["google_provider"],
            "cadence": autopilot.google_cadence,
            "analysis": bool(autopilot.run_analysis and ask),
            "limited_by": "allowance" if plan["limited"] else None,
            "skip": skip,
            "usd_max": ap.usd4(plan["usd_max"]),
            "reserve_pct": state["reserve_pct"],
            "run_engines": autopilot.run_engines,
            "run_google": autopilot.run_google,
        })
        fields = {
            "plan": stored,
            "prompts_planned": len(rows),
            "prompts_skipped": skipped,
            "over_limit": over,
            "heartbeat_at": now,
            "updated_at": now,
        }
        if skip:
            fields["error_code"] = skip
            fields["error_message"] = (
                reserve_message(state) if skip == "reserve_reached" else RUN_MESSAGES[skip]
            )
            nxt = FINALIZE
        elif ask:
            nxt = ENGINES
        elif plan["google"]:
            nxt = GOOGLE
        else:
            nxt = FINALIZE
        fields["stage"] = nxt
        Run.objects.filter(id=run.id).update(**fields)
        if plan["limited"] and ap.is_saved(autopilot):
            Autopilot.objects.filter(id=autopilot.id).update(prompt_offset=plan["next_offset"])
    return True


class Stop:
    """Stop was pressed, or the run already ended (re-read at most every
    CANCEL_POLL_SECONDS)."""

    def __init__(self, run_id):
        self.run_id = run_id
        self._lock = threading.Lock()
        self._checked_at: float | None = None
        self._stopped = False

    def __call__(self) -> bool:
        if self._stopped:
            return True
        with self._lock:
            now = clock.monotonic()
            if self._checked_at is None or now - self._checked_at >= CANCEL_POLL_SECONDS:
                self._checked_at = now
                row = Run.objects.filter(id=self.run_id).values_list(
                    "cancel_requested", "status",
                ).first()
                self._stopped = row is None or bool(row[0]) or row[1] in Run.TERMINAL
        return self._stopped


def _in_thread(fn, *args):
    close_old_connections()
    try:
        return fn(*args)
    except Exception:
        logger.exception("autopilot: a worker failed")
        return None
    finally:
        connection.close()


def _workers(count: int, fn, *, inline: bool, halt: threading.Event) -> None:
    """Run ``fn`` on ``count`` threads until each returns (once, in this
    thread, inline: other threads cannot see a test transaction's rows)."""
    if inline or count <= 1:
        try:
            fn()
        except Exception:
            logger.exception("autopilot: a worker failed")
        return
    pool = ThreadPoolExecutor(max_workers=count, thread_name_prefix="autopilot")
    futures = [pool.submit(_in_thread, fn) for _ in range(count)]
    try:
        wait(futures)
    except BaseException:
        # Celery's soft time limit: no new prompt starts; waiting would run
        # into the hard limit.
        halt.set()
        pool.shutdown(wait=False, cancel_futures=True)
        raise
    pool.shutdown()


def _claim(run_id, step: str):
    """The next row of a step, marked running; None when none is left."""
    now = timezone.now()
    with transaction.atomic():
        if step == ENGINES:
            qs = Row.objects.filter(run_id=run_id, status=Row.STATUS_QUEUED)
        else:
            qs = Row.objects.filter(run_id=run_id, google_status=Row.GOOGLE_PENDING)
        if connection.vendor == "postgresql":
            qs = qs.select_for_update(skip_locked=True)
        row = qs.order_by("position").first()
        if row is None:
            return None
        if step == ENGINES:
            Row.objects.filter(id=row.id).update(
                status=Row.STATUS_RUNNING, started_at=now, attempts=F("attempts") + 1,
            )
        else:
            Row.objects.filter(id=row.id).update(
                google_status=Row.GOOGLE_RUNNING, google_started_at=now,
                google_attempts=F("google_attempts") + 1,
            )
    return (
        Row.objects.select_related("brand_prompt", "brand_prompt__prompt", "run")
        .filter(id=row.id).first()
    )


def _end_row(row, status: str, *, code: str = "", tally: dict | None = None) -> None:
    """End a row of the engines step and count it on the run."""
    tally = tally or {}
    asked = sum(t["asked"] for t in tally.values())
    answered = sum(t["answered"] for t in tally.values())
    named = sum(t["named"] for t in tally.values())
    now = timezone.now()
    updated = Row.objects.filter(id=row.id, status=Row.STATUS_RUNNING).update(
        status=status, error_code=code, engines=tally, answers_asked=asked,
        answers_answered=answered, answers_named=named, finished_at=now,
    )
    if not updated:
        return
    counter = {
        Row.STATUS_DONE: "prompts_done",
        Row.STATUS_FAILED: "prompts_failed",
        Row.STATUS_SKIPPED: "prompts_skipped",
    }[status]
    Run.objects.filter(id=row.run_id).update(**{
        counter: F(counter) + 1,
        "answers_asked": F("answers_asked") + asked,
        "answers_answered": F("answers_answered") + answered,
        "answers_named": F("answers_named") + named,
        "heartbeat_at": now,
    })


def _skip_queued(run_id, code: str) -> None:
    """End the step's queued rows skipped (Stop, or the line was reached)."""
    count = Row.objects.filter(run_id=run_id, status=Row.STATUS_QUEUED).update(
        status=Row.STATUS_SKIPPED, error_code=code, finished_at=timezone.now(),
    )
    if count:
        Run.objects.filter(id=run_id).update(prompts_skipped=F("prompts_skipped") + count)


def _give_back(run_id, step: str) -> None:
    """Rows a crashed worker left running go back to the queue once; the
    second time they end failed (a crash can re-ask one prompt)."""
    cutoff = timezone.now() - timedelta(minutes=ROW_STALE_MINUTES)
    if step == ENGINES:
        stale = Row.objects.filter(run_id=run_id, status=Row.STATUS_RUNNING, started_at__lt=cutoff)
        stale.filter(attempts__lt=MAX_ATTEMPTS).update(status=Row.STATUS_QUEUED)
        for row in Row.objects.filter(run_id=run_id, status=Row.STATUS_RUNNING,
                                      started_at__lt=cutoff):
            _end_row(row, Row.STATUS_FAILED, code="interrupted", tally=row.engines or {})
        return
    stale = Row.objects.filter(
        run_id=run_id, google_status=Row.GOOGLE_RUNNING, google_started_at__lt=cutoff,
    )
    stale.filter(google_attempts__lt=MAX_ATTEMPTS).update(google_status=Row.GOOGLE_PENDING)
    Row.objects.filter(
        run_id=run_id, google_status=Row.GOOGLE_RUNNING, google_started_at__lt=cutoff,
    ).update(google_status=Row.GOOGLE_FAILED)


def _error_code(answers: list) -> str:
    text = " ".join((a.error or "").lower() for a in answers)
    if "allowance" in text:
        return "allowance_exceeded"
    return "engine_error"


def _ask_prompt(run, row, stop: Stop, *, inline: bool) -> None:
    """Ask one prompt to each of its engines (in parallel) through
    EngineScan, then count its answers."""
    from apps.citations.services.url_analytics import model_key
    from apps.prompt_library.services.prompt_crawler import EngineScan

    website = run.website
    bp = row.brand_prompt
    asks = asks_for(bp, (run.plan or {}).get("engines") or [])
    if not asks:
        _end_row(row, Row.STATUS_SKIPPED, code="not_configured")
        return
    scan = EngineScan(
        website, bp.prompt, acting_user=website.user, actor="system",
        usage_tags={"feature": "autopilot", "autopilot_run": str(run.id)}, feature="autopilot",
    )
    linked = threading.Lock()

    def ask(provider, variant):
        answer = scan.ask(provider, variant, should_stop=stop)
        audit_id = scan.audit_id
        if audit_id:
            with linked:
                Row.objects.filter(id=row.id).exclude(audit_id=audit_id).update(audit_id=audit_id)
        return provider, answer

    results = []
    if inline or len(asks) == 1:
        for provider, variant in asks:
            results.append(ask(provider, variant))
    else:
        with ThreadPoolExecutor(max_workers=len(asks), thread_name_prefix="autopilot-ask") as pool:
            futures = [pool.submit(_in_thread, ask, p, v) for p, v in asks]
        results = [out for out in (f.result() for f in futures) if out is not None]
    try:
        scan.finish(stopped=stop())
    except Exception:
        logger.exception("autopilot: the scan of run %s row %s could not finish", run.id, row.id)

    tally: dict = {}
    answers = []
    for provider, answer in results:
        if answer.stopped or answer.not_configured:
            continue
        answers.append(answer)
        key = model_key(provider)
        cell = tally.setdefault(key, {"asked": 0, "answered": 0, "named": 0, "rank": None})
        cell["asked"] += 1
        if not answer.succeeded:
            continue
        cell["answered"] += 1
        if (answer.analysis or {}).get("is_mentioned"):
            cell["named"] += 1
            rank = (answer.analysis or {}).get("mention_rank")
            if isinstance(rank, int) and rank > 0 and (cell["rank"] is None or rank < cell["rank"]):
                cell["rank"] = rank
    if any(cell["answered"] for cell in tally.values()):
        _end_row(row, Row.STATUS_DONE, tally=tally)
    elif not answers:
        stopped = any(answer.stopped for _p, answer in results)
        _end_row(row, Row.STATUS_SKIPPED, code="cancelled" if stopped else "not_configured",
                 tally=tally)
    else:
        _end_row(row, Row.STATUS_FAILED, code=_error_code(answers), tally=tally)


def run_engines(run_id) -> str:
    """The engines step: every queued prompt, AUTOPILOT_PROMPT_CONCURRENCY at
    a time, until none is left, Stop, Autopilot's line or the step's time."""
    from core.ai_tracking import month_to_date_cost

    run = (
        Run.objects.select_related("website", "website__user")
        .filter(id=run_id, status=RUNNING, stage=ENGINES).first()
    )
    if run is None:
        return IDLE
    _give_back(run.id, ENGINES)
    inline = _inline()
    stop = Stop(run.id)
    halt = threading.Event()
    reached = threading.Event()
    deadline = clock.monotonic() + int(ap.conf("AUTOPILOT_STEP_SECONDS"))
    owner = run.website.user
    state = ap.allowance_state(owner)

    def work():
        while not halt.is_set() and not reached.is_set():
            if stop() or clock.monotonic() >= deadline:
                return
            if not state["unlimited"] and Decimal(str(month_to_date_cost(owner))) >= state["line"]:
                reached.set()
                return
            row = _claim(run.id, ENGINES)
            if row is None:
                return
            try:
                _ask_prompt(run, row, stop, inline=inline)
            except Exception:
                logger.exception("autopilot: run %s prompt %s failed", run.id, row.id)
                _end_row(row, Row.STATUS_FAILED, code="internal_error")
            _heartbeat(run.id)

    _workers(int(ap.conf("AUTOPILOT_PROMPT_CONCURRENCY")), work, inline=inline, halt=halt)
    if stop():
        _skip_queued(run.id, "cancelled")
    elif reached.is_set():
        _skip_queued(run.id, "reserve_reached" if state["reserve_pct"] else "allowance_exceeded")
    rows = Row.objects.filter(run_id=run.id)
    if rows.filter(status=Row.STATUS_QUEUED).exists():
        return HANDOVER
    if rows.filter(status=Row.STATUS_RUNNING).exists():
        return IDLE
    run.refresh_from_db()
    return ADVANCED if _advance(run.id, ENGINES, _after_engines(run)) else IDLE


def _quotas() -> tuple[DailyQuota, DailyQuota]:
    project = DailyQuota(
        namespace="autopilot_serp", setting_name="AUTOPILOT_GOOGLE_PAGES_PER_PROJECT",
        default_limit=200,
    )
    deployment = DailyQuota(
        namespace="autopilot_serp_all", setting_name="AUTOPILOT_GOOGLE_PAGES_PER_DAY",
        default_limit=1000,
    )
    return project, deployment


def take_page(website_id) -> bool:
    """One Google page from the project's and the deployment's daily quotas."""
    project, deployment = _quotas()
    key = str(website_id)
    if not project.consume(key):
        return False
    if deployment.consume("all"):
        return True
    # Give the project's unit back: no page went out.
    try:
        cache.decr(project._key(key))
    except ValueError:
        pass
    return False


def _end_google(row, status: str, *, check=None, pages: int = 0) -> None:
    Row.objects.filter(id=row.id).update(
        google_status=status, google_check=check, google_pages=F("google_pages") + pages,
    )
    if pages:
        Run.objects.filter(id=row.run_id).update(google_pages=F("google_pages") + pages)


def _check_google(run, row, reuse_hours: int) -> None:
    """Google page one for one prompt: a recent check is reused, else one
    page within both quotas, retried once on a transient error."""
    from apps.search_gap.models import GoogleRankCheck
    from apps.search_gap.services import google_rank

    bp = row.brand_prompt
    recent = (
        GoogleRankCheck.objects.filter(
            brand_prompt=bp, error="",
            checked_at__gte=timezone.now() - timedelta(hours=reuse_hours),
        ).order_by("-checked_at").first()
    )
    if recent is not None:
        _end_google(row, Row.GOOGLE_REUSED, check=recent)
        return
    pages = 0
    for attempt in (1, 2):
        if not take_page(run.website_id):
            _end_google(row, Row.GOOGLE_SKIPPED, pages=pages)
            return
        try:
            check = google_rank.check(bp, force=True, depth=GOOGLE_DEPTH, enforce_daily_cap=False)
        except google_rank.NotConfigured:
            _end_google(row, Row.GOOGLE_FAILED, pages=pages)
            return
        pages += check.pages_fetched
        if not check.error:
            _end_google(row, Row.GOOGLE_CHECKED, check=check, pages=pages)
            return
        if attempt == 1 and check.error in GOOGLE_RETRY_CODES:
            continue
        _end_google(row, Row.GOOGLE_FAILED, check=check, pages=pages)
        return


def _google_counts(run_id) -> dict:
    measured = (Row.GOOGLE_CHECKED, Row.GOOGLE_REUSED)
    return Row.objects.filter(run_id=run_id).aggregate(
        total=Count("id", filter=~Q(google_status=Row.GOOGLE_OFF)),
        checked=Count("id", filter=Q(google_status=Row.GOOGLE_CHECKED)),
        reused=Count("id", filter=Q(google_status=Row.GOOGLE_REUSED)),
        failed=Count("id", filter=Q(google_status=Row.GOOGLE_FAILED)),
        page_one=Count("id", filter=Q(
            google_status__in=measured, google_check__position__isnull=False,
            google_check__position__lte=GOOGLE_DEPTH,
        )),
        pages=Sum("google_pages"),
    )


def _google_fields(run_id, planned: bool) -> dict:
    counts = _google_counts(run_id)
    measured = counts["checked"] + counts["reused"]
    if not planned or not counts["total"]:
        status = "off"
    elif measured == counts["total"]:
        status = "done"
    elif measured:
        status = "partial"
    elif counts["failed"]:
        status = "failed"
    else:
        status = "skipped"
    return {
        "google_status": status,
        "google_measured": measured,
        "google_checked": counts["checked"],
        "google_reused": counts["reused"],
        "google_page_one": counts["page_one"],
        "google_pages": counts["pages"] or 0,
    }


def run_google(run_id) -> str:
    """The Google step: page one of every planned prompt,
    AUTOPILOT_GOOGLE_CONCURRENCY at a time."""
    run = (
        Run.objects.select_related("website")
        .filter(id=run_id, status=RUNNING, stage=GOOGLE).first()
    )
    if run is None:
        return IDLE
    _give_back(run.id, GOOGLE)
    inline = _inline()
    stop = Stop(run.id)
    halt = threading.Event()
    deadline = clock.monotonic() + int(ap.conf("AUTOPILOT_STEP_SECONDS"))
    weekly = (run.plan or {}).get("cadence") == CADENCE_WEEKLY
    reuse_hours = int(ap.conf(
        "AUTOPILOT_GOOGLE_WEEKLY_REUSE_HOURS" if weekly else "AUTOPILOT_GOOGLE_REUSE_HOURS",
    ))

    def work():
        while not halt.is_set():
            if stop() or clock.monotonic() >= deadline:
                return
            row = _claim(run.id, GOOGLE)
            if row is None:
                return
            try:
                _check_google(run, row, reuse_hours)
            except Exception:
                logger.exception("autopilot: run %s Google check %s failed", run.id, row.id)
                _end_google(row, Row.GOOGLE_FAILED)
            _heartbeat(run.id)

    _workers(int(ap.conf("AUTOPILOT_GOOGLE_CONCURRENCY")), work, inline=inline, halt=halt)
    rows = Row.objects.filter(run_id=run.id)
    if stop():
        rows.filter(google_status=Row.GOOGLE_PENDING).update(google_status=Row.GOOGLE_SKIPPED)
    elif rows.filter(google_status=Row.GOOGLE_PENDING).exists():
        return HANDOVER
    if rows.filter(google_status=Row.GOOGLE_RUNNING).exists():
        return IDLE
    Run.objects.filter(id=run.id).update(**_google_fields(run.id, True))
    run.refresh_from_db()
    return ADVANCED if _advance(run.id, GOOGLE, _after_google(run)) else IDLE


def start_analysis(run_id) -> str:
    """The analysis step: one answer analysis run over the new answers that
    name the brand, when it fits under Autopilot's line (the insights
    estimate checks only the full cap). The owner agreed to it when they
    turned Autopilot on."""
    from django.conf import settings

    from apps.insights.models import InsightRun
    from apps.insights.services import runs as insights_runs

    claimed = Run.objects.filter(
        id=run_id, status=RUNNING, stage=ANALYSIS, analysis_status="",
    ).update(analysis_status="running", heartbeat_at=timezone.now())
    if not claimed:
        return IDLE
    run = Run.objects.select_related("website", "website__user").get(id=run_id)
    website, owner = run.website, run.website.user
    named = Row.objects.filter(run_id=run.id).aggregate(n=Sum("answers_named"))["n"] or 0
    skip = None
    insight = None
    if run.cancel_requested:
        skip = "cancelled"
    elif not named:
        skip = "nothing_to_run"
    elif not getattr(settings, "INSIGHTS_ENABLED", True):
        skip = "not_configured"
    else:
        params = {"period": "7d", "autopilot_run": str(run.id)}
        try:
            est = insights_runs.estimate(website, owner, "answer_analysis", params)
            state = ap.allowance_state(owner)
            need = Decimal(str(est["ai"]["usd_max"]))
            if not est["can_run"]:
                skip = est["blockers"][0]["code"] if est["blockers"] else "nothing_to_run"
            elif not state["unlimited"] and state["spent"] + need > state["line"]:
                skip = "reserve_reached"
            else:
                insight = insights_runs.create_run(
                    website, owner, "answer_analysis", params, est["confirm"],
                )
        except CanseeException as exc:
            skip = exc.code
        except Exception:
            logger.exception("autopilot: the analysis of run %s could not start", run.id)
            skip = "internal_error"
    plan = dict(run.plan or {})
    if skip:
        plan["analysis_skip"] = skip
        Run.objects.filter(id=run.id).update(analysis_status="skipped", plan=plan)
        return ADVANCED if _advance(run.id, ANALYSIS, FINALIZE) else IDLE
    Run.objects.filter(id=run.id).update(analysis_run=insight)
    insight.refresh_from_db()
    if insight.status in InsightRun.TERMINAL:
        return ADVANCED if _advance(run.id, ANALYSIS, FINALIZE) else IDLE
    return WAITING


def analysis_ready(run, now=None) -> bool:
    """The analysis run ended, or the text waited AUTOPILOT_ANALYSIS_WAIT_MINUTES."""
    from apps.insights.models import InsightRun

    insight = run.analysis_run
    if run.cancel_requested or insight is None or insight.status in InsightRun.TERMINAL:
        return True
    waited = (now or timezone.now()) - insight.created_at
    return waited >= timedelta(minutes=int(ap.conf("AUTOPILOT_ANALYSIS_WAIT_MINUTES")))


def _wait_for_analysis(run_id) -> None:
    while True:
        run = Run.objects.select_related("analysis_run").filter(id=run_id).first()
        if run is None or run.status != RUNNING or run.stage != ANALYSIS:
            return
        if analysis_ready(run):
            _advance(run_id, ANALYSIS, FINALIZE)
            return
        clock.sleep(ANALYSIS_POLL_SECONDS)


# -- finalize -------------------------------------------------------------------------


def _row_counts(run_id) -> dict:
    agg = Row.objects.filter(run_id=run_id).aggregate(
        planned=Count("id"),
        done=Count("id", filter=Q(status=Row.STATUS_DONE)),
        failed=Count("id", filter=Q(status=Row.STATUS_FAILED)),
        skipped=Count("id", filter=Q(status=Row.STATUS_SKIPPED)),
        asked=Sum("answers_asked"),
        answered=Sum("answers_answered"),
        named=Sum("answers_named"),
    )
    return {k: v or 0 for k, v in agg.items()}


def _pairs(run_id) -> dict:
    """{(brand prompt, engine): (answered, named)} of a run's answered pairs."""
    out = {}
    for bp_id, engines in Row.objects.filter(run_id=run_id).values_list(
        "brand_prompt_id", "engines",
    ):
        for engine, cell in (engines or {}).items():
            answered = int((cell or {}).get("answered") or 0)
            if answered:
                out[(bp_id, engine)] = (answered, int(cell.get("named") or 0))
    return out


def previous_run(run):
    """The newest earlier run of the project that finished with answers."""
    return (
        Run.objects.filter(
            website_id=run.website_id, status__in=Run.TERMINAL, answers_answered__gt=0,
            created_at__lt=run.created_at,
        )
        .exclude(id=run.id).order_by("-created_at").first()
    )


def change_pts(run, prev) -> float | None:
    """Share of answers that name the brand, this run against ``prev``, over
    the (prompt, engine) pairs answered in both (contract 3c)."""
    if prev is None:
        return None
    now, before = _pairs(run.id), _pairs(prev.id)
    common = set(now) & set(before)
    if not common:
        return None
    answered_now = sum(now[k][0] for k in common)
    named_now = sum(now[k][1] for k in common)
    answered_before = sum(before[k][0] for k in common)
    named_before = sum(before[k][1] for k in common)
    return round(100 * (named_now / answered_now - named_before / answered_before), 1)


def _new_contradicted(run) -> int | None:
    """Claims this run's analysis found contradicted that no earlier
    contradicted claim of the project has."""
    from apps.insights.models import AnswerClaim, InsightRun

    insight = run.analysis_run
    if insight is None or insight.status not in InsightRun.TERMINAL:
        return None
    contradicted = AnswerClaim.VERDICT_CONTRADICTED
    keys = set(
        AnswerClaim.objects.filter(analysis__run=insight, verdict=contradicted)
        .values_list("text_key", flat=True)
    )
    if not keys:
        return 0
    earlier = set(
        AnswerClaim.objects.filter(website_id=run.website_id, verdict=contradicted,
                                   text_key__in=keys)
        .exclude(analysis__run=insight)
        .filter(Q(analysis__run__isnull=True) | Q(analysis__run__created_at__lt=insight.created_at))
        .values_list("text_key", flat=True)
    )
    return len(keys - earlier)


def _analysis_final(run) -> str:
    from apps.insights.models import InsightRun

    insight = run.analysis_run
    if insight is None:
        if run.analysis_status in ("", "running"):
            # Never reached, or never started (stopped or interrupted first).
            return "skipped" if (run.plan or {}).get("analysis") else "off"
        return run.analysis_status
    if insight.status in (InsightRun.STATUS_DONE, InsightRun.STATUS_PARTIAL):
        return "done"
    if insight.status in InsightRun.TERMINAL:
        return "failed"
    return "pending"


def finalize_run(run_id, *, interrupted: bool = False) -> Run | None:
    """End a run: unfinished rows, counts from the rows, the ledger cost,
    the analysis outcome, the status, the failure count (3 failed runs in
    a row pause Autopilot), then the text and its state (contract 5c)."""
    from apps.autopilot.services import text

    with transaction.atomic():
        run = (
            Run.objects.select_for_update().filter(id=run_id).first()
        )
        if run is None or run.status in Run.TERMINAL:
            return run
        now = timezone.now()
        stopped = run.cancel_requested
        end_status = Row.STATUS_SKIPPED if stopped else Row.STATUS_FAILED
        end_code = "cancelled" if stopped else "interrupted"
        Row.objects.filter(
            run_id=run.id, status__in=(Row.STATUS_QUEUED, Row.STATUS_RUNNING),
        ).update(status=end_status, error_code=end_code, finished_at=now)
        Row.objects.filter(
            run_id=run.id, google_status__in=(Row.GOOGLE_PENDING, Row.GOOGLE_RUNNING),
        ).update(google_status=Row.GOOGLE_SKIPPED if stopped else Row.GOOGLE_FAILED)

        plan = dict(run.plan or {})
        counts = _row_counts(run.id)
        google = _google_fields(run.id, bool(plan.get("google")))
        if plan.get("google") and stopped and not google["google_measured"]:
            google["google_status"] = "skipped"
        error_code, error_message = run.error_code, run.error_message
        skip = plan.get("skip")
        if stopped:
            status = Run.STATUS_CANCELLED
        elif interrupted:
            status = Run.STATUS_INTERRUPTED
            error_code, error_message = "interrupted", RUN_MESSAGES["interrupted"]
        elif skip:
            status = Run.STATUS_SKIPPED
        elif plan.get("ask"):
            if not counts["answered"]:
                reserve = Row.objects.filter(
                    run_id=run.id, status=Row.STATUS_SKIPPED, error_code__in=ALLOWANCE_CODES,
                ).count()
                if reserve and not counts["done"] and not counts["failed"]:
                    status = Run.STATUS_SKIPPED
                    error_code = "reserve_reached" if plan.get("reserve_pct") else "allowance_exceeded"
                    state = ap.allowance_state(run.website.user)
                    error_message = (
                        reserve_message(state) if error_code == "reserve_reached"
                        else RUN_MESSAGES["allowance_exceeded"]
                    )
                    plan["skip"] = error_code
                else:
                    status = Run.STATUS_FAILED
                    error_code, error_message = "no_answers", RUN_MESSAGES["no_answers"]
            elif (counts["done"] == counts["planned"]
                  and google["google_status"] not in ("partial", "failed")):
                status = Run.STATUS_DONE
            else:
                status = Run.STATUS_PARTIAL
        else:
            status = {
                "done": Run.STATUS_DONE, "partial": Run.STATUS_PARTIAL,
                "failed": Run.STATUS_FAILED,
            }.get(google["google_status"], Run.STATUS_SKIPPED)
            reason = "engines_off" if not plan.get("run_engines", True) else "not_configured"
            if status in (Run.STATUS_SKIPPED, Run.STATUS_FAILED) and not error_code:
                error_code, error_message = reason, RUN_MESSAGES[reason]

        prev = previous_run(run)
        analysis_status = _analysis_final(run)
        fields = {
            "status": status,
            "stage": FINISHED,
            "plan": plan,
            "prompts_planned": counts["planned"],
            "prompts_done": counts["done"],
            "prompts_failed": counts["failed"],
            "prompts_skipped": counts["skipped"],
            "answers_asked": counts["asked"],
            "answers_answered": counts["answered"],
            "answers_named": counts["named"],
            "change_pts": change_pts(run, prev) if counts["answered"] else None,
            **google,
            "analysis_status": analysis_status,
            "new_contradicted": _new_contradicted(run) if analysis_status == "done" else None,
            "ai_cost_usd": ledger_cost(run),
            "error_code": error_code,
            "error_message": error_message,
            "finished_at": now,
            "heartbeat_at": now,
            "updated_at": now,
        }
        Run.objects.filter(id=run.id).update(**fields)

        autopilot = Autopilot.objects.select_for_update().filter(website_id=run.website_id).first()
        paused = False
        if autopilot is not None:
            if status == Run.STATUS_FAILED:
                failures = autopilot.consecutive_failures + 1
                update = {"consecutive_failures": failures}
                if failures >= 3 and autopilot.enabled:
                    update.update(enabled=False, paused_reason=Autopilot.PAUSED_FAILURES)
                    paused = True
                Autopilot.objects.filter(id=autopilot.id).update(**update)
            elif status in (Run.STATUS_DONE, Run.STATUS_PARTIAL) and autopilot.consecutive_failures:
                Autopilot.objects.filter(id=autopilot.id).update(consecutive_failures=0)
        if paused:
            plan["paused"] = True
            Run.objects.filter(id=run.id).update(plan=plan)

    run = Run.objects.select_related("website", "website__user", "analysis_run").get(id=run_id)
    try:
        text.prepare_text(run)
    except Exception:
        logger.exception("autopilot: the text of run %s could not be prepared", run_id)
    return Run.objects.filter(id=run_id).first()


# -- dispatcher -----------------------------------------------------------------------


def _create_scheduled(autopilot, day, now):
    """Today's scheduled run, when a slot is free (False: no slot)."""
    with transaction.atomic():
        _lock_slots()
        if not _free_slot():
            return False
        try:
            with transaction.atomic():
                run, created = Run.objects.get_or_create(
                    website_id=autopilot.website_id, local_date=day,
                    defaults={
                        "trigger": Run.TRIGGER_SCHEDULE, "status": RUNNING, "started_at": now,
                        "heartbeat_at": now,
                    },
                )
        except IntegrityError:
            return None
    return run if created else None


def _start_waiting(now, mode) -> int:
    """Queued runs (manual first, oldest first) take free slots."""
    started = 0
    waiting = Run.objects.filter(status=QUEUED).order_by("created_at")
    for run in sorted(waiting, key=lambda r: (r.trigger != Run.TRIGGER_MANUAL, r.created_at)):
        if not claim_slot(run.id):
            break
        started += 1
        start_soon(run.id, mode=mode)
    return started


def _start_due(now, mode) -> int:
    """Scheduled runs of every project whose local run time has passed today."""
    candidates = []
    for autopilot in (
        Autopilot.objects.filter(
            enabled=True, paused_reason="", website__is_active=True, website__is_deleted=False,
        ).select_related("website", "website__user")
    ):
        tz = ap.zone(autopilot.timezone)
        local = now.astimezone(tz)
        if local.time().replace(tzinfo=None) < autopilot.run_time:
            continue
        candidates.append((ap.at_local(local.date(), autopilot.run_time, tz), autopilot,
                           local.date()))
    if not candidates:
        return 0
    have = set(
        Run.objects.filter(
            website_id__in=[a.website_id for _d, a, _l in candidates],
            local_date__in={day for _d, _a, day in candidates},
        ).values_list("website_id", "local_date")
    )
    started = 0
    candidates.sort(key=lambda c: (c[0], c[1].created_at))
    for _due, autopilot, day in candidates:
        if (autopilot.website_id, day) in have:
            continue
        if not ap.plan_allows(autopilot.website.user, "autopilot"):
            continue
        run = _create_scheduled(autopilot, day, now)
        if run is False:
            break
        if run is not None:
            started += 1
            start_soon(run.id, mode=mode)
    return started


def _analysis_waits(now, mode) -> int:
    moved = 0
    for run in Run.objects.filter(
        status=RUNNING, stage=ANALYSIS, analysis_status="running",
    ).select_related("analysis_run"):
        if analysis_ready(run, now) and _advance(run.id, ANALYSIS, FINALIZE):
            moved += 1
            kick(run.id, mode=mode)
    return moved


def _resume_and_sweep(now, mode) -> tuple[int, int]:
    """Stages that stopped reporting go to a new worker; after MAX_RESUMES
    or AUTOPILOT_STALE_MINUTES the run ends interrupted."""
    quiet = now - timedelta(minutes=RESUME_MINUTES)
    dead = now - timedelta(minutes=int(ap.conf("AUTOPILOT_STALE_MINUTES")))
    resumed = interrupted = 0
    for run in Run.objects.filter(status=RUNNING):
        seen = run.heartbeat_at or run.started_at or run.created_at
        if seen >= quiet:
            continue
        waiting_analysis = run.stage == ANALYSIS and run.analysis_status == "running"
        if waiting_analysis:
            continue
        if run.resumes >= MAX_RESUMES or seen < dead:
            finalize_run(run.id, interrupted=True)
            deliver_due(run.id, mode=mode)
            interrupted += 1
            continue
        if Run.objects.filter(id=run.id, status=RUNNING, heartbeat_at=run.heartbeat_at).update(
            resumes=F("resumes") + 1, heartbeat_at=now,
        ):
            resumed += 1
            kick(run.id, mode=mode)
    return resumed, interrupted


def _texts_due(now, mode) -> int:
    due = list(
        Run.objects.filter(text_status=Run.TEXT_WAITING, text_due_at__lte=now)
        .values_list("id", flat=True)
    )
    for run_id in due:
        deliver_due(run_id, mode=mode)
    return len(due)


def dispatch_tick(now=None, *, mode: str | None = None) -> dict:
    """One dispatcher tick (beat every 5 minutes, backend 4e): queued and
    due runs take free slots, finished analyses release their runs, silent
    stages resume or end, waiting texts go out."""
    if not ap.enabled_on_server():
        return {"skipped": "disabled"}
    if not cache.add(DISPATCH_LOCK, 1, DISPATCH_LOCK_SECONDS):
        return {"skipped": "busy"}
    try:
        now = now or timezone.now()
        mode = mode or run_mode()
        started = _start_waiting(now, mode)
        started += _start_due(now, mode)
        finalized = _analysis_waits(now, mode)
        resumed, interrupted = _resume_and_sweep(now, mode)
        texts = _texts_due(timezone.now(), mode)
    finally:
        cache.delete(DISPATCH_LOCK)
    return {
        "started": started, "finalized": finalized, "resumed": resumed,
        "interrupted": interrupted, "texts": texts,
    }
