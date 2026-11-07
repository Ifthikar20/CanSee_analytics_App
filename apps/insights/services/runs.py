"""One lifecycle for every insights run kind (contract 1e).

A kind (answer analysis, crawler audit, prompt research, fan-out Google
check, shopping check, and the agent app's context build) registers a
``Kind`` here. Every run follows the wave 1 country-check pattern: the
dialog posts ``estimate`` (no side effects) on every change, the user
confirms it, and ``create_run`` recomputes the estimate, refuses a run
that now needs more than was confirmed (409 ``estimate_changed``) or that
a blocker stops, and dispatches it. Workers count units on the run with
F() expressions; ``finalize`` ends the run once its kind returns.

Spend stays bounded the same three ways as country checks: the kind's own
caps, the per-website daily Google page quota (``insights_serp``), and the
user's monthly AI allowance, which create checks up front and every AI
call checks again before it goes out.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import Counter
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from datetime import UTC, timedelta
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal, InvalidOperation

from django.conf import settings
from django.db import close_old_connections, connection, transaction
from django.db.models import F, Sum, Value
from django.db.models.functions import Coalesce, Greatest
from django.utils import timezone

from apps.insights.models import InsightRun
from core.exceptions import CanseeException
from core.quota import DailyQuota

logger = logging.getLogger("apps")

ACTIVE = InsightRun.ACTIVE
TERMINAL = InsightRun.TERMINAL
QUEUED, RUNNING = InsightRun.STATUS_QUEUED, InsightRun.STATUS_RUNNING
# What a unit can end as (the InsightRun.units_<status> counters).
UNIT_STATUSES = ("done", "failed", "skipped", "cancelled")
# How often a worker re-reads Stop.
CANCEL_POLL_SECONDS = 2.0
ZERO = Decimal("0")
HAIKU_LABEL = "Claude Haiku 5.5"

# Public item error messages (contract 1e rule 6).
ITEM_MESSAGES = {
    "invalid_output": "The analysis came back unreadable.",
    "refused": "The model declined this input.",
    "rate_limited": "The model was busy or rate limited.",
    "upstream_error": "The model returned an error.",
    "allowance_exceeded": "The monthly AI allowance is used up.",
    "fetch_failed": "The page could not be loaded.",
    "blocked_url": "Only public web pages can be read.",
    "timeout": "The page took too long to load.",
    "serp_failed": "Google search failed.",
    "daily_limit": "This project used its Google pages for today.",
    "engine_error": "The engine returned an error.",
    "cancelled": "Stopped before it ran.",
    "interrupted": "The run stopped before this finished.",
    "internal_error": "Something went wrong with this run.",
}
RUN_INTERRUPTED = "The run stopped before it finished."
RUN_NOT_STARTED = "The run could not be started."
UPPER_BOUND = "These are upper bounds; you are billed only for what runs."
ESTIMATE_CHANGED = "The cost changed. Check the new estimate and confirm again."
CONFIRM_MESSAGE = "Confirm the estimate before running."

# Codes a kind's validate may raise that the estimate shows as a blocker
# (contract 1e rule 4) instead of failing the request.
BLOCKER_CODES = frozenset({
    "run_in_progress", "allowance_exceeded", "insufficient_allowance", "daily_limit",
    "nothing_to_run", "too_many", "not_configured", "cooldown", "daily_runs",
})
BLOCKER_STATUS = {
    "run_in_progress": 409, "allowance_exceeded": 403, "insufficient_allowance": 403,
    "daily_limit": 409, "nothing_to_run": 400, "too_many": 400, "not_configured": 400,
    "cooldown": 409, "daily_runs": 409,
}
# RunOptions.limits.max_units of the known kinds: (setting, default).
_MAX_UNITS = {
    "answer_analysis": ("INSIGHTS_ANALYSIS_MAX_ANSWERS", 60),
    "site_audit": ("SITE_AUDIT_MAX_PAGES", 20),
    "prompt_research": ("INSIGHTS_RESEARCH_MAX_GOOGLE_PROMPTS", 5),
    "fanout_google": ("INSIGHTS_FANOUT_MAX_QUERIES", 10),
    "shopping_check": ("SHOPPING_MAX_PRODUCTS_PER_RUN", 5),
    "context_build": ("CONTEXT_BUILD_MAX_PAGES", 8),
}


@dataclass(frozen=True)
class Kind:
    """One kind of run (contract 1e).

    ``validate(website, params)`` returns the kind's plan or raises a
    CanseeException: 400 ``invalid`` (with ``details.field``) for malformed
    params, or a blocker code (``nothing_to_run``, ``too_many``,
    ``not_configured``...), which the estimate shows as a blocker.
    ``estimate(website, user, plan)`` returns ``{summary, units, ai:
    {calls_max, usd_max, model_label?}, google: {pages_max}, fetches:
    {pages_max}, warnings: [{code, message}], blockers?: [{code, status,
    message, details?}]}`` with no side effects. ``choices(website, user)``
    is RunOptions.choices. ``plan_items(run, plan)`` creates the kind's
    item rows and returns the run's unit count. ``execute(ctx)`` works the
    items through a ``RunContext``. ``limits()``, when given, is
    RunOptions.limits.
    """

    key: str
    label: str
    queue: str
    validate: Callable
    estimate: Callable
    choices: Callable
    plan_items: Callable
    execute: Callable
    limits: Callable | None = None


KINDS: dict[str, Kind] = {}


def register(kind: Kind) -> None:
    """Make ``kind`` available to the runs API. Registering the same Kind
    again is a no-op; another Kind under a known key is a programming error."""
    existing = KINDS.get(kind.key)
    if existing is not None and existing is not kind and existing != kind:
        raise ValueError(f"Run kind {kind.key!r} is already registered.")
    KINDS[kind.key] = kind


def _invalid(field: str, message: str) -> CanseeException:
    return CanseeException(
        message, code="invalid", status_code=400, details={"field": field, "message": message},
    )


def get_kind(key) -> Kind:
    kind = KINDS.get(key) if isinstance(key, str) else None
    if kind is None:
        raise _invalid("kind", "Choose a known run kind.")
    return kind


def kind_label(key: str) -> str:
    kind = KINDS.get(key)
    return kind.label if kind is not None else key


# -- money, allowance, quota ---------------------------------------------------


def usd4(value) -> str:
    return str(Decimal(str(value)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))


def usd2(value, rounding=ROUND_HALF_UP) -> str:
    return str(Decimal(str(value)).quantize(Decimal("0.01"), rounding=rounding))


def _decimal(value) -> Decimal:
    try:
        out = Decimal(str(value if value is not None else 0))
    except (InvalidOperation, ValueError):
        return ZERO
    return out if out.is_finite() else ZERO


def allowance(user) -> dict:
    """The user's monthly AI allowance as Decimals (``cap`` 0 = unlimited)."""
    from core import ai_tracking

    if user is None or not getattr(user, "is_authenticated", False):
        return {"unlimited": True, "cap": ZERO, "spent": ZERO, "left": None}
    cap = _decimal(ai_tracking.effective_ai_cap(user))
    spent = _decimal(ai_tracking.month_to_date_cost(user))
    unlimited = cap <= 0
    left = None if unlimited else max(ZERO, cap - spent)
    return {"unlimited": unlimited, "cap": cap, "spent": spent, "left": left}


def allowance_payload(user) -> dict:
    """``{unlimited, cap_usd, spent_usd, left_usd}`` (RunOptions)."""
    a = allowance(user)
    return {
        "unlimited": a["unlimited"],
        "cap_usd": None if a["unlimited"] else usd2(a["cap"]),
        "spent_usd": usd2(a["spent"]),
        "left_usd": None if a["unlimited"] else usd2(a["left"], ROUND_DOWN),
    }


def google_quota() -> DailyQuota:
    """Google results pages per website per UTC day, every insights kind
    together, keyed by website id."""
    return DailyQuota(
        namespace="insights_serp",
        setting_name="INSIGHTS_DAILY_GOOGLE_PAGES",
        default_limit=60,
    )


def credits_per_page(kind_key: str) -> int:
    """ScraperAPI credits one Google page of this kind costs."""
    if kind_key == "shopping_check":
        return int(getattr(settings, "SCRAPERAPI_SHOPPING_CREDITS", 25))
    return int(settings.SCRAPERAPI_CREDITS_PER_SEARCH)


def ledger_cost(run) -> Decimal:
    """What the usage ledger recorded for this run's AI requests."""
    from apps.accounts.models import AITokenUsage

    total = (
        AITokenUsage.objects
        .filter(created_at__gte=run.created_at, metadata__audit_id=run.audit_id)
        .aggregate(total=Sum("estimated_cost_usd"))["total"]
    )
    return total or ZERO


def iso(value) -> str | None:
    """ISO 8601 UTC with a Z, to the millisecond."""
    if value is None:
        return None
    text = value.astimezone(UTC).isoformat()
    if value.microsecond:
        text = text[:23] + text[26:]
    return text.replace("+00:00", "Z")


# -- run rows ----------------------------------------------------------------


def active_run(website, kind: str):
    return (
        InsightRun.objects.select_related("created_by")
        .filter(website=website, kind=kind, status__in=ACTIVE)
        .order_by("-created_at").first()
    )


def last_run(website, kind: str):
    return (
        InsightRun.objects.select_related("created_by")
        .filter(website=website, kind=kind, status__in=TERMINAL)
        .order_by("-created_at").first()
    )


def run_state(website, kind: str) -> dict:
    """``{active_run, last_run}`` of one kind for a page payload."""
    active, last = active_run(website, kind), last_run(website, kind)
    return {
        "active_run": run_payload(active) if active else None,
        "last_run": run_payload(last) if last else None,
    }


def _user_name(user) -> str | None:
    if user is None:
        return None
    return (getattr(user, "full_name", "") or "").strip() or user.email.split("@")[0]


def _duration_ms(run) -> int | None:
    if not (run.started_at and run.finished_at):
        return None
    return max(0, int((run.finished_at - run.started_at).total_seconds() * 1000))


def run_payload(run) -> dict:
    """``Run`` (contract 1e); ``RunSummary`` is the same object."""
    settled = run.units_done + run.units_failed + run.units_skipped + run.units_cancelled
    finished = run.status in TERMINAL
    # Stored when the run ended; read live from the usage ledger while it runs.
    ai_usd = run.ai_cost_usd if finished else ledger_cost(run)
    scraper = run.google_provider == "scraperapi"
    return {
        "id": str(run.id),
        "kind": run.kind,
        "kind_label": kind_label(run.kind),
        "status": run.status,
        "summary": run.summary,
        "created_at": iso(run.created_at),
        "started_at": iso(run.started_at),
        "finished_at": iso(run.finished_at),
        "duration_ms": _duration_ms(run),
        "created_by_name": _user_name(run.created_by),
        "progress": {
            "total": run.units_total,
            "queued": max(0, run.units_total - run.units_running - settled),
            "running": run.units_running,
            "done": run.units_done,
            "failed": run.units_failed,
            "skipped": run.units_skipped,
            "cancelled": run.units_cancelled,
        },
        "cost": {
            "ai_usd": usd4(ai_usd),
            "google_pages": run.google_pages,
            "scraperapi_credits": run.google_pages * credits_per_page(run.kind) if scraper else None,
        },
        "confirmed": run.confirmed,
        "cancel_requested": run.cancel_requested,
        "error": {"code": run.error_code, "message": run.error_message} if run.error_code else None,
        # The kind's own outcome (context_build's pages, suggestions and re-checks).
        "result": run.result or {},
    }


def runs_list(website, kind: str | None = None, limit: int = 20) -> list[dict]:
    qs = InsightRun.objects.filter(website=website).select_related("created_by")
    if kind:
        qs = qs.filter(kind=kind)
    return [run_payload(run) for run in qs.order_by("-created_at")[:limit]]


# -- options and estimate ------------------------------------------------------


def _limits(kind: Kind) -> dict:
    if kind.limits is not None:
        return kind.limits()
    setting = _MAX_UNITS.get(kind.key)
    if setting is None:
        return {}
    return {"max_units": int(getattr(settings, setting[0], setting[1]))}


def options(website, user, kind: str) -> dict:
    """``RunOptions`` (contract 1e)."""
    from apps.citations.services import serp_google

    k = get_kind(kind)
    which = serp_google.provider()
    quota = google_quota()
    key = str(website.id)
    limit, left = quota.limit(key), quota.remaining(key)
    return {
        "kind": k.key,
        "enabled": bool(settings.INSIGHTS_ENABLED),
        "configured": {
            "ai": bool(getattr(settings, "ANTHROPIC_API_KEY", "")),
            "google": bool(which),
            "google_provider": which or None,
            "google_provider_label": serp_google.PROVIDER_LABELS.get(which),
            "shopping": which == serp_google.PROVIDER_SCRAPERAPI,
        },
        "limits": _limits(k),
        "allowance": allowance_payload(user),
        "google_daily": {
            "limit": limit,
            "used": max(0, limit - left),
            "left": left,
            "credits_per_page": (
                credits_per_page(k.key) if which == serp_google.PROVIDER_SCRAPERAPI else None
            ),
        },
        "choices": k.choices(website, user),
        **run_state(website, k.key),
    }


def _blocker(code: str, message: str, status: int | None = None, details: dict | None = None) -> dict:
    return {
        "code": code,
        "message": message,
        "status": status or BLOCKER_STATUS.get(code, 400),
        "details": details,
    }


def _ints(value) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def _prepare(website, user, kind: Kind, params) -> tuple:
    """(plan or None, RunEstimate, blockers with status and details)."""
    from apps.citations.services import serp_google

    if params is None:
        params = {}
    if not isinstance(params, dict):
        raise _invalid("params", "Send the run's settings as a JSON object.")

    plan, own = None, {}
    blocked_by_plan = None
    try:
        plan = kind.validate(website, params)
    except CanseeException as exc:
        if exc.code not in BLOCKER_CODES:
            raise
        blocked_by_plan = _blocker(
            exc.code, exc.message, getattr(exc, "status_code", None),
            getattr(exc, "details", None),
        )
    if blocked_by_plan is None:
        own = kind.estimate(website, user, plan) or {}

    ai = own.get("ai") or {}
    calls = _ints(ai.get("calls_max"))
    usd_max = max(ZERO, _decimal(ai.get("usd_max")))
    pages = _ints((own.get("google") or {}).get("pages_max"))
    fetches = _ints((own.get("fetches") or {}).get("pages_max"))

    which = serp_google.provider()
    quota = google_quota()
    qkey = str(website.id)
    daily_limit, daily_left = quota.limit(qkey), quota.remaining(qkey)

    a = allowance(user)
    paid_ai = usd_max > 0
    exceeded = paid_ai and not a["unlimited"] and a["spent"] >= a["cap"]
    enough = a["unlimited"] or not paid_ai or a["spent"] + usd_max <= a["cap"]

    blockers = []
    current = active_run(website, kind.key)
    if current is not None:
        blockers.append(_blocker(
            "run_in_progress", f"{kind.label} is already running for this project.",
            details={"run_id": str(current.id)},
        ))
    if exceeded:
        blockers.append(_blocker("allowance_exceeded", "You have used your monthly AI allowance."))
    elif not enough:
        left_usd = usd2(a["left"], ROUND_DOWN)
        blockers.append(_blocker(
            "insufficient_allowance",
            f"This run needs up to ${usd2(usd_max)} of AI allowance and ${left_usd} is left "
            "this month.",
            details={"need_usd": usd4(usd_max), "left_usd": left_usd, "cap_usd": usd2(a["cap"])},
        ))
    if pages > daily_left:
        blockers.append(_blocker(
            "daily_limit",
            f"This project has {daily_left} Google page{'' if daily_left == 1 else 's'} left "
            f"today; this run needs up to {pages}.",
            details={"left": daily_left, "need": pages},
        ))
    if blocked_by_plan is not None:
        blockers.append(blocked_by_plan)
    for item in own.get("blockers") or []:
        blockers.append(_blocker(
            item["code"], item["message"], item.get("status"), item.get("details"),
        ))

    warnings = [dict(w) for w in own.get("warnings") or []]
    if paid_ai or pages:
        warnings.append({"code": "upper_bound", "message": UPPER_BOUND})

    scraper = which == serp_google.PROVIDER_SCRAPERAPI
    estimate = {
        "kind": kind.key,
        "summary": str(own.get("summary") or "")[:200],
        "units": own.get("units") or {},
        "ai": {
            "calls_max": calls,
            "usd_max": usd4(usd_max),
            "model_label": ai.get("model_label"),
        },
        "google": {
            "pages_max": pages,
            "credits_max": pages * credits_per_page(kind.key) if scraper else 0,
            "usd_max": usd4(serp_google.cost_per_search_usd() * pages),
            "daily_left": daily_left,
            "daily_limit": daily_limit,
        },
        "fetches": {"pages_max": fetches},
        "allowance": {
            "unlimited": a["unlimited"],
            "cap_usd": None if a["unlimited"] else usd2(a["cap"]),
            "left_usd": None if a["unlimited"] else usd2(a["left"], ROUND_DOWN),
            "enough": enough,
        },
        "confirm": {"ai_calls": calls, "ai_usd_max": usd4(usd_max), "google_pages": pages},
        "warnings": warnings,
        "blockers": [{"code": b["code"], "message": b["message"]} for b in blockers],
        "can_run": not blockers,
    }
    return plan, estimate, blockers


def estimate(website, user, kind: str, params) -> dict:
    """``RunEstimate`` (contract 1e). No side effects."""
    _plan, est, _blockers = _prepare(website, user, get_kind(kind), params)
    return est


def parse_confirm(raw) -> dict:
    """``{ai_calls, ai_usd_max: Decimal, google_pages}`` the user confirmed."""
    if not isinstance(raw, dict):
        raise _invalid("confirm", CONFIRM_MESSAGE)
    out = {}
    for name in ("ai_calls", "google_pages"):
        value = raw.get(name)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise _invalid("confirm", CONFIRM_MESSAGE)
        out[name] = value
    try:
        usd = Decimal(str(raw.get("ai_usd_max")))
        if not usd.is_finite() or usd < 0:
            raise ValueError
        # Kept at 4 places; a number too large for that was never confirmed.
        usd4(usd)
    except (InvalidOperation, ValueError):
        raise _invalid("confirm", CONFIRM_MESSAGE) from None
    out["ai_usd_max"] = usd
    return out


def _blocker_error(blocker: dict) -> CanseeException:
    return CanseeException(
        blocker["message"], code=blocker["code"], status_code=blocker["status"],
        details=blocker.get("details"),
    )


def create_run(website, user, kind: str, params, confirm) -> InsightRun:
    """Create and start a confirmed run. Raises the first blocker of the
    recomputed estimate as its contract error, or 409 ``estimate_changed``
    when it now needs more than the user confirmed."""
    from apps.citations.services import serp_google
    from apps.websites.models import Website

    k = get_kind(kind)
    confirmed = parse_confirm(confirm)
    plan, est, blockers = _prepare(website, user, k, params)
    if blockers:
        raise _blocker_error(blockers[0])
    server = est["confirm"]
    if (
        server["ai_calls"] > confirmed["ai_calls"]
        or Decimal(server["ai_usd_max"]) > confirmed["ai_usd_max"]
        or server["google_pages"] > confirmed["google_pages"]
    ):
        raise CanseeException(
            ESTIMATE_CHANGED, code="estimate_changed", status_code=409, details={"estimate": est},
        )

    inline = settings.INSIGHTS_RUN_MODE == "inline"
    with transaction.atomic():
        # One active run per (website, kind): the website row serializes creates.
        list(Website.all_objects.select_for_update().filter(id=website.id).values_list("id", flat=True))
        current = active_run(website, k.key)
        if current is not None:
            raise CanseeException(
                f"{k.label} is already running for this project.", code="run_in_progress",
                status_code=409, details={"run_id": str(current.id)},
            )
        run = InsightRun.objects.create(
            website=website,
            created_by=user if getattr(user, "is_authenticated", False) else None,
            kind=k.key,
            status=QUEUED,
            params=params or {},
            estimate=est,
            confirmed={
                "ai_calls": confirmed["ai_calls"],
                "ai_usd_max": usd4(confirmed["ai_usd_max"]),
                "google_pages": confirmed["google_pages"],
            },
            summary=est["summary"],
            google_provider=serp_google.provider() if est["google"]["pages_max"] else "",
        )
        total = int(k.plan_items(run, plan) or 0)
        InsightRun.objects.filter(id=run.id).update(units_total=total)
        if not inline:
            run_id = run.id
            transaction.on_commit(lambda: dispatch(run_id))
    if inline:
        # on_commit never fires inside a test transaction.
        execute_run(run.id)
    run.refresh_from_db()
    return run


# -- dispatch and execution ------------------------------------------------------


def dispatch(run_id) -> None:
    """Hand a created run to its executor (INSIGHTS_RUN_MODE)."""
    mode = settings.INSIGHTS_RUN_MODE
    if mode == "inline":
        execute_run(run_id)
        return
    try:
        if mode == "thread" or (mode == "auto" and getattr(settings, "CELERY_TASK_ALWAYS_EAGER", False)):
            threading.Thread(
                target=execute_run_safely, args=(str(run_id),),
                name=f"insight-run-{run_id}", daemon=True,
            ).start()
            return
        from apps.insights.tasks import run_insight

        run = InsightRun.objects.filter(id=run_id).only("kind").first()
        if run is None:
            return
        run_insight.apply_async(args=[str(run_id)], queue=get_kind(run.kind).queue)
    except Exception:
        # A run nobody will execute would hold its kind's one active slot.
        logger.exception("insights run %s could not be started", run_id)
        InsightRun.objects.filter(id=run_id, status=QUEUED).update(
            error_code="internal_error", error_message=RUN_NOT_STARTED,
        )
        finalize(run_id, errors=Counter({"internal_error": 1}))


def execute_run_safely(run_id) -> None:
    """execute_run on a thread of its own: fresh connections, never raises."""
    close_old_connections()
    try:
        execute_run(run_id)
    except Exception:
        logger.exception("insights run %s crashed", run_id)
    finally:
        connection.close()


def execute_run(run_id) -> None:
    """Run a queued run's kind, then finalize. A run another worker holds,
    or one that was stopped before it started, is left alone."""
    run = InsightRun.objects.select_related("website", "created_by").filter(id=run_id).first()
    if run is None or run.status in TERMINAL:
        return
    now = timezone.now()
    started = InsightRun.objects.filter(id=run_id, status=QUEUED).update(
        status=RUNNING, started_at=now, heartbeat_at=now,
    )
    if not started:
        return
    run.status, run.started_at, run.heartbeat_at = RUNNING, now, now
    kind = KINDS.get(run.kind)
    ctx = RunContext(run)
    if kind is None:
        logger.error("insights run %s has an unknown kind %r", run_id, run.kind)
        ctx.errors["internal_error"] += 1
    else:
        try:
            kind.execute(ctx)
        except Exception:
            # Celery's soft time limit lands here too; finalize ends the
            # units that did not finish.
            logger.exception("insights run %s (%s) failed", run_id, run.kind)
    finalize(run_id, errors=ctx.errors)


class Pool:
    """Runs a kind's work items: on up to ``workers`` threads in the thread
    and celery modes, one after another in ``inline`` (other threads cannot
    see a test transaction's rows). A callable that raises is logged and
    yields None; finalize ends the unit it left unfinished. Use it as a
    context manager when calling ``submit``.
    """

    def __init__(self, workers: int, *, inline: bool):
        self._inline = inline
        self._workers = max(1, int(workers))
        self._executor: ThreadPoolExecutor | None = None
        self._entered = False

    def __enter__(self) -> Pool:
        self._entered = True
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if exc_type is None:
            self.shutdown()
        else:
            # Raised while waiting (Celery's soft time limit): no further
            # item starts; waiting for the queue would run into the hard limit.
            self.shutdown(wait=False, cancel_futures=True)

    def submit(self, fn, *args, **kwargs) -> Future:
        if self._inline:
            future: Future = Future()
            future.set_result(_call(fn, args, kwargs))
            return future
        if self._executor is None:
            self._executor = ThreadPoolExecutor(
                max_workers=self._workers, thread_name_prefix="insight",
            )
        return self._executor.submit(_call_in_thread, fn, args, kwargs)

    def map(self, fn, *iterables) -> list:
        futures = [self.submit(fn, *args) for args in zip(*iterables, strict=False)]
        try:
            wait(futures)
        except BaseException:
            self.shutdown(wait=False, cancel_futures=True)
            raise
        if not self._entered:
            self.shutdown()
        return [future.result() for future in futures]

    def shutdown(self, wait: bool = True, cancel_futures: bool = False) -> None:
        if self._executor is not None:
            self._executor.shutdown(wait=wait, cancel_futures=cancel_futures)
            self._executor = None


def _call(fn, args, kwargs):
    try:
        return fn(*args, **kwargs)
    except Exception:
        logger.exception("insights: a run item failed")
        return None


def _call_in_thread(fn, args, kwargs):
    close_old_connections()
    try:
        return _call(fn, args, kwargs)
    finally:
        connection.close()


class RunContext:
    """What a kind's ``execute`` works with (contract 1e, backend 1d)."""

    def __init__(self, run):
        self.run = run
        self.website = run.website
        # Spend is attributed to the user who confirmed the run.
        self.user = run.created_by
        self.audit_id = run.audit_id
        # Error codes of failed units, for the run's error when none is done.
        self.errors: Counter = Counter()
        self._quota = google_quota()
        self._quota_key = str(run.website_id)
        self._lock = threading.Lock()
        self._checked_at: float | None = None
        self._cancelled = False

    def cancelled(self) -> bool:
        """Stop was pressed, or the run already ended (re-read at most every
        CANCEL_POLL_SECONDS)."""
        if self._cancelled:
            return True
        with self._lock:
            now = time.monotonic()
            if self._checked_at is None or now - self._checked_at >= CANCEL_POLL_SECONDS:
                self._checked_at = now
                row = (
                    InsightRun.objects.filter(id=self.run.id)
                    .values_list("cancel_requested", "status").first()
                )
                self._cancelled = row is None or bool(row[0]) or row[1] in TERMINAL
        return self._cancelled

    def _units(self):
        return InsightRun.objects.filter(id=self.run.id).exclude(status__in=TERMINAL)

    def heartbeat(self) -> None:
        self._units().update(heartbeat_at=timezone.now())

    def start_item(self) -> None:
        self._units().update(units_running=F("units_running") + 1, heartbeat_at=timezone.now())

    def end_item(self, status: str, *, error: str | None = None, started: bool = True) -> None:
        """Count one unit as ``status`` (done, failed, skipped, cancelled).
        ``started`` False for a unit that ended without start_item."""
        if status not in UNIT_STATUSES:
            raise ValueError(f"Unknown unit status {status!r}.")
        fields = {f"units_{status}": F(f"units_{status}") + 1, "heartbeat_at": timezone.now()}
        if started:
            fields["units_running"] = Greatest(F("units_running") - 1, Value(0))
        self._units().update(**fields)
        if error and status != "done":
            with self._lock:
                self.errors[error] += 1

    def google_page(self) -> bool:
        """Take one Google page of the project's daily quota (False when it
        is used up). Called once per request sent."""
        return self._quota.consume(self._quota_key)

    def billed_page(self) -> None:
        """Count one Google page the provider billed."""
        InsightRun.objects.filter(id=self.run.id).update(google_pages=F("google_pages") + 1)

    def pool(self, workers: int) -> Pool:
        return Pool(workers, inline=settings.INSIGHTS_RUN_MODE == "inline")


# -- finish, stop, sweep -------------------------------------------------------


def _item_models() -> tuple:
    """Kinds' item rows that carry a status per run."""
    from apps.insights.models import AnswerAnalysis, ShoppingResult

    return (AnswerAnalysis, ShoppingResult)


def _end_items(run_id, status: str, code: str) -> None:
    """End the run's queued and running item rows as ``status`` / ``code``."""
    for model in _item_models():
        fields = {"status": status, "error_code": code}
        if any(f.name == "error_message" for f in model._meta.fields):
            fields["error_message"] = ITEM_MESSAGES[code]
        model.objects.filter(run_id=run_id, status__in=(QUEUED, RUNNING)).update(**fields)


def _run_error(run_id, errors: Counter | None) -> tuple[str, str]:
    """The most common item error of a run in public words."""
    tally = Counter(errors or {})
    for model in _item_models():
        tally.update(
            model.objects.filter(run_id=run_id).exclude(error_code="")
            .values_list("error_code", flat=True)
        )
    tally.pop("", None)
    if not tally:
        return "internal_error", ITEM_MESSAGES["internal_error"]
    code = tally.most_common(1)[0][0]
    return code, ITEM_MESSAGES.get(code, ITEM_MESSAGES["internal_error"])


def finalize(run_id, *, errors: Counter | None = None, queued_only: bool = False):
    """End a run whose kind returned (or that will never run): units left
    unfinished end ``cancelled`` after Stop, else ``failed``; then the
    status, the error, the cost and the finish time (contract 1e).
    ``queued_only`` (Stop of a queued run) leaves alone a run a worker
    started in the meantime: that worker sees Stop and finalizes it once its
    requests have returned, so their cost lands on the run."""
    with transaction.atomic():
        run = InsightRun.objects.select_for_update().filter(id=run_id).first()
        if run is None or run.status in TERMINAL:
            return run
        if queued_only and run.status != QUEUED:
            return run
        settled = run.units_done + run.units_failed + run.units_skipped + run.units_cancelled
        left = max(0, run.units_total - settled)
        stopped = run.cancel_requested
        end_as = "cancelled" if stopped else "failed"
        _end_items(run.id, end_as, "cancelled" if stopped else "interrupted")

        done, total = run.units_done, run.units_total
        all_done = bool(total) and done >= total
        error_code, error_message = run.error_code, run.error_message
        if stopped and not all_done:
            status = InsightRun.STATUS_CANCELLED
        elif all_done:
            status = InsightRun.STATUS_DONE
        elif done:
            status = InsightRun.STATUS_PARTIAL
        else:
            status = InsightRun.STATUS_FAILED
            if not error_code:
                error_code, error_message = _run_error(run.id, errors)
        now = timezone.now()
        fields = {
            "status": status,
            "units_running": 0,
            "finished_at": now,
            "heartbeat_at": now,
            "ai_cost_usd": ledger_cost(run),
            "error_code": error_code,
            "error_message": error_message,
            "updated_at": now,
        }
        if left:
            fields[f"units_{end_as}"] = F(f"units_{end_as}") + left
        InsightRun.objects.filter(id=run.id).update(**fields)
    run.refresh_from_db()
    return run


def cancel_run(run) -> InsightRun:
    """Stop a run: its workers end their remaining units ``cancelled``. A
    run no worker holds yet (queued) ends at once. Idempotent; a finished
    run is returned unchanged."""
    if run.status in TERMINAL:
        return run
    InsightRun.objects.filter(id=run.id).update(cancel_requested=True)
    run.refresh_from_db()
    if run.status == QUEUED:
        # Checked again under the row lock: a worker may take it meanwhile.
        finalize(run.id, queued_only=True)
        run.refresh_from_db()
    return run


def sweep_stale(website) -> int:
    """Interrupt the website's runs that stopped reporting (no heartbeat for
    INSIGHTS_RUN_STALE_MINUTES). Called by every view that shows runs."""
    cutoff = timezone.now() - timedelta(minutes=settings.INSIGHTS_RUN_STALE_MINUTES)
    stale = list(
        InsightRun.objects
        .filter(website=website, status__in=ACTIVE)
        .annotate(last_seen=Coalesce("heartbeat_at", "started_at", "created_at"))
        .filter(last_seen__lt=cutoff)
        .values_list("id", flat=True)
    )
    for run_id in stale:
        with transaction.atomic():
            run = InsightRun.objects.select_for_update().filter(id=run_id).first()
            if run is None or run.status in TERMINAL:
                continue
            if (run.heartbeat_at or run.started_at or run.created_at) >= cutoff:
                continue
            settled = run.units_done + run.units_failed + run.units_skipped + run.units_cancelled
            left = max(0, run.units_total - settled)
            _end_items(run.id, "failed", "interrupted")
            now = timezone.now()
            InsightRun.objects.filter(id=run.id).update(
                status=InsightRun.STATUS_INTERRUPTED,
                units_running=0,
                units_failed=F("units_failed") + left,
                finished_at=now,
                error_code="interrupted",
                error_message=RUN_INTERRUPTED,
                ai_cost_usd=ledger_cost(run),
                updated_at=now,
            )
    return len(stale)
