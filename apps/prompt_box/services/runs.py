"""Ask AI runs (wave 4 contract 4).

The card posts ``estimate`` (no side effects) for the question on screen,
the user confirms it, and ``create_run`` recomputes the estimate, refuses a
run that a blocker stops or that now needs more than was confirmed (409
``estimate_changed``), and dispatches it. Answers are asked
PROMPT_BOX_CONCURRENCY at a time, interleaved by engine. Each answer takes
one unit of the project's daily Ask AI answers first, and every provider
call checks the caller's AI allowance again (LLMProvider.query with
``user``), so spend stays bounded by the confirmed estimate, the daily cap
and the allowance.

The question is asked bare (no system prompt), as Autopilot asks it, and
each answer is read with the extraction every scan uses. Answers stay in
this app's tables: tracked numbers never include them.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass
from datetime import timedelta
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal, InvalidOperation

from django.conf import settings
from django.db import close_old_connections, connection, transaction
from django.db.models.functions import Coalesce
from django.utils import timezone

from apps.insights.services import runs as insights_runs
from apps.llm_ranking.services.extraction_service import HaikuExtractionService
from apps.llm_ranking.services.ranking_service import search_query_fields, wilson_ci
from apps.prompt_box.models import PromptBoxAnswer, PromptBoxRun
from apps.search_gap.services import country_checks, country_geo
from core.exceptions import CanseeException
from core.quota import DailyQuota
from core.utils.retention import grounded_links_may_be_stored

logger = logging.getLogger("apps")

QUEUED = PromptBoxAnswer.STATUS_QUEUED
RUNNING = PromptBoxAnswer.STATUS_RUNNING
DONE = PromptBoxAnswer.STATUS_DONE
FAILED = PromptBoxAnswer.STATUS_FAILED
SKIPPED = PromptBoxAnswer.STATUS_SKIPPED
CANCELLED = PromptBoxAnswer.STATUS_CANCELLED
ACTIVE = PromptBoxRun.ACTIVE_STATUSES
TERMINAL = PromptBoxRun.TERMINAL_STATUSES
ZERO = Decimal("0")

# How often a worker re-reads Stop.
CANCEL_POLL_SECONDS = 2.0
MIN_CHARS = 3
MAX_CHARS = 300
DEFAULT_SAMPLES = 3
EXCERPT_CHARS = 200
RUNS_LISTED = 20

TEXT_MESSAGE = f"Ask one question, up to {MAX_CHARS} characters."
ENGINES_MESSAGE = "Choose at least one configured engine."
NOT_CONFIGURED = "AI engines aren't available yet."
RUN_IN_PROGRESS = "A question is already being asked for this project."
ALLOWANCE_EXCEEDED = "You have used your monthly AI allowance."
ESTIMATE_CHANGED = insights_runs.ESTIMATE_CHANGED
CONFIRM_MESSAGE = insights_runs.CONFIRM_MESSAGE
RUN_INTERRUPTED = insights_runs.RUN_INTERRUPTED
RUN_NOT_STARTED = insights_runs.RUN_NOT_STARTED

# Public answer error messages: the engine codes of country_checks.engine_error,
# plus this app's own daily cap and interruption.
ANSWER_MESSAGES = {
    **{
        code: country_checks.CELL_MESSAGES[code]
        for code in (
            "not_configured", "rate_limited", "timeout", "allowance_exceeded",
            "prompt_runs_exhausted", "refused", "cut_off", "engine_error", "cancelled",
            "internal_error",
        )
    },
    "daily_limit": "This project used its Ask AI answers for today.",
    "interrupted": "The run stopped before this answer finished.",
}

# Estimate blockers in the order create_run raises them, with their status.
BLOCKER_STATUS = {
    "not_configured": 400,
    "run_in_progress": 409,
    "prompt_runs_exhausted": 403,
    "insufficient_prompt_runs": 403,
    "allowance_exceeded": 403,
    "insufficient_allowance": 403,
    "daily_limit": 409,
}


def usd4(value) -> str:
    return insights_runs.usd4(value)


def usd2(value, rounding=ROUND_HALF_UP) -> str:
    return insights_runs.usd2(value, rounding)


def daily_quota() -> DailyQuota:
    """Ask AI answers per project per UTC day, keyed by website id."""
    return DailyQuota(
        namespace="prompt_box",
        setting_name="PROMPT_BOX_DAILY_ANSWERS",
        default_limit=100,
    )


def max_samples() -> int:
    return max(1, int(settings.PROMPT_BOX_MAX_SAMPLES))


def configured_engines() -> list[dict]:
    """``[{key, label}]`` of the engines with a key on this server, display order."""
    from apps.agent.tools.research import engine_catalog

    return [{"key": e["key"], "label": e["label"]} for e in engine_catalog() if e["configured"]]


def active_run(website):
    return (
        PromptBoxRun.objects.filter(website=website, status__in=ACTIVE)
        .order_by("-created_at").first()
    )


# -- validation ------------------------------------------------------------------


@dataclass
class Ask:
    """A validated question."""

    text: str            # whitespace collapsed to one line
    samples: int         # answers per engine
    engines: list[dict]  # [{key, label}], display order


def _invalid(field: str, message: str) -> CanseeException:
    return CanseeException(
        message, code="invalid", status_code=400, details={"field": field, "message": message},
    )


def validate(data) -> Ask:
    """The request as an Ask. Raises 400 ``invalid`` (with ``details.field``)
    for anything malformed. With no engine configured on the server the
    engines are empty and the estimate says so (``not_configured``)."""
    if not isinstance(data, dict):
        raise _invalid("text", TEXT_MESSAGE)
    raw = data.get("text")
    text = " ".join(raw.split()) if isinstance(raw, str) else ""
    if not MIN_CHARS <= len(text) <= MAX_CHARS:
        raise _invalid("text", TEXT_MESSAGE)

    raw = data.get("samples")
    samples = DEFAULT_SAMPLES if raw is None else raw
    limit = max_samples()
    if isinstance(samples, bool) or not isinstance(samples, int) or not 1 <= samples <= limit:
        raise _invalid("samples", f"Choose 1 to {limit} answers per engine.")

    configured = configured_engines()
    raw = data.get("engines")
    if raw is None or not configured:
        engines = configured
    else:
        keys = {e["key"] for e in configured}
        if not isinstance(raw, list) or not raw:
            raise _invalid("engines", ENGINES_MESSAGE)
        chosen = set()
        for item in raw:
            key = item.strip().lower() if isinstance(item, str) else ""
            if key not in keys:
                raise _invalid("engines", ENGINES_MESSAGE)
            chosen.add(key)
        engines = [e for e in configured if e["key"] in chosen]
    return Ask(text=text, samples=samples, engines=engines)


def parse_confirm(raw) -> dict:
    """``{answers, ai_usd_max: Decimal}`` the user confirmed."""
    if not isinstance(raw, dict):
        raise _invalid("confirm", CONFIRM_MESSAGE)
    answers = raw.get("answers")
    if isinstance(answers, bool) or not isinstance(answers, int) or answers < 0:
        raise _invalid("confirm", CONFIRM_MESSAGE)
    try:
        usd = Decimal(str(raw.get("ai_usd_max")))
        if not usd.is_finite() or usd < 0:
            raise ValueError
        # Kept at 4 places; a number too large for that was never confirmed.
        usd4(usd)
    except (InvalidOperation, ValueError):
        raise _invalid("confirm", CONFIRM_MESSAGE) from None
    return {"answers": answers, "ai_usd_max": usd}


# -- estimate ---------------------------------------------------------------------


def tracked_map(website_id, texts) -> dict:
    """``{text hash: (prompt id, brand prompt id)}`` of the project's active
    tracked prompts whose text matches one of ``texts`` (the hash Track uses)."""
    from apps.prompt_library.models import BrandPrompt
    from apps.prompt_library.services._hash import text_hash

    hashes = {text_hash(text) for text in texts if text}
    if not hashes:
        return {}
    out = {}
    rows = (
        BrandPrompt.objects
        .filter(website_id=website_id, is_archived=False, prompt__text_hash__in=hashes)
        .order_by("created_at")
        .values_list("prompt__text_hash", "prompt_id", "id")
    )
    for digest, prompt_id, brand_prompt_id in rows:
        out.setdefault(digest, (str(prompt_id), str(brand_prompt_id)))
    return out


def tracked_block(website_id, text: str, found: dict | None = None) -> dict:
    """``{prompt_id, brand_prompt_id}`` when the question is a tracked prompt
    of the project, else both null."""
    from apps.prompt_library.services._hash import text_hash

    found = tracked_map(website_id, [text]) if found is None else found
    prompt_id, brand_prompt_id = found.get(text_hash(text), (None, None))
    return {"prompt_id": prompt_id, "brand_prompt_id": brand_prompt_id}


def _blocker(code: str, message: str, details: dict | None = None) -> dict:
    return {"code": code, "message": message, "status": BLOCKER_STATUS[code], "details": details}


def _plural(n: int, noun: str) -> str:
    return f"{n} {noun}{'' if n == 1 else 's'}"


def _prepare(website, user, ask: Ask) -> tuple[dict, list[dict]]:
    """(the estimate payload, its blockers with status and details)."""
    keys = [e["key"] for e in ask.engines]
    answers = ask.samples * len(keys)
    usd_max = sum((country_checks.answer_usd_max(key) for key in keys), ZERO) * ask.samples

    from apps.metering.services import prompt_runs

    a = insights_runs.allowance(user)
    quota = daily_quota()
    qkey = str(website.id)
    daily_limit, daily_left = quota.limit(qkey), quota.remaining(qkey)
    runs = prompt_runs.estimate_block(user, website, answers)
    # Free accounts count prompt runs and never see dollars.
    hide_usd = not runs["unlimited"]

    blockers = []
    if not keys:
        blockers.append(_blocker("not_configured", NOT_CONFIGURED))
    current = active_run(website)
    if current is not None:
        blockers.append(_blocker("run_in_progress", RUN_IN_PROGRESS, {"run_id": str(current.id)}))
    short = prompt_runs.shortfall(user, website, answers) if answers else None
    if short is not None:
        blockers.append(_blocker(short["code"], short["message"], short["details"]))
    if usd_max > 0 and not a["unlimited"]:
        if a["spent"] >= a["cap"]:
            blockers.append(_blocker(
                "allowance_exceeded",
                prompt_runs.ALLOWANCE_USED_FREE if hide_usd else ALLOWANCE_EXCEEDED,
            ))
        elif a["spent"] + usd_max > a["cap"]:
            left = usd2(a["left"], ROUND_DOWN)
            blockers.append(_blocker(
                "insufficient_allowance",
                prompt_runs.ALLOWANCE_SHORT_FREE if hide_usd else (
                    f"This run needs up to ${usd2(usd_max)} of AI allowance and ${left} is left "
                    "this month."
                ),
                {"need_usd": usd4(usd_max), "left_usd": left, "cap_usd": usd2(a["cap"])},
            ))
    if answers > daily_left:
        if daily_left <= 0:
            message = (
                f"This project used its {_plural(daily_limit, 'Ask AI answer')} for today. "
                "Try again tomorrow."
            )
        else:
            message = (
                f"This project has {_plural(daily_left, 'Ask AI answer')} left today; "
                f"this question needs {answers}."
            )
        blockers.append(_blocker("daily_limit", message, {"left": daily_left, "need": answers}))

    estimate = {
        "answers": answers,
        "usd_max": usd4(usd_max),
        "engines": [dict(e) for e in ask.engines],
        "allowance": {
            "unlimited": a["unlimited"],
            "left_usd": None if a["unlimited"] else usd2(a["left"], ROUND_DOWN),
        },
        "daily": {"limit": daily_limit, "left": daily_left},
        # Each answer is one prompt run: {needed, remaining, limit, unlimited, enough}.
        "prompt_runs": runs,
        "tracked": tracked_block(website.id, ask.text),
        "blockers": [{"code": b["code"], "message": b["message"]} for b in blockers],
        "can_run": not blockers,
        "confirm": {"answers": answers, "ai_usd_max": usd4(usd_max)},
    }
    return estimate, blockers


def estimate(website, user, data) -> dict:
    """What asking ``data`` costs at most, and whether it may run. No side effects."""
    est, _blockers = _prepare(website, user, validate(data))
    return est


# -- create -----------------------------------------------------------------------


def create_run(website, user, data, confirm) -> PromptBoxRun:
    """Create and start a confirmed run. Raises the first blocker of the
    recomputed estimate as its error, or 409 ``estimate_changed`` (with
    ``details.estimate``) when it now needs more than the user confirmed.
    Spend is the caller's, as insights runs."""
    from apps.websites.models import Website

    ask = validate(data)
    confirmed = parse_confirm(confirm)
    est, blockers = _prepare(website, user, ask)
    if blockers:
        first = blockers[0]
        raise CanseeException(
            first["message"], code=first["code"], status_code=first["status"],
            details=first["details"],
        )
    if (
        est["answers"] > confirmed["answers"]
        or Decimal(est["usd_max"]) > confirmed["ai_usd_max"]
    ):
        raise CanseeException(
            ESTIMATE_CHANGED, code="estimate_changed", status_code=409, details={"estimate": est},
        )

    inline = settings.PROMPT_BOX_RUN_MODE == "inline"
    keys = [e["key"] for e in ask.engines]
    with transaction.atomic():
        # One active run per project: the website row serializes creates.
        list(Website.all_objects.select_for_update().filter(id=website.id).values_list("id", flat=True))
        current = active_run(website)
        if current is not None:
            raise CanseeException(
                RUN_IN_PROGRESS, code="run_in_progress", status_code=409,
                details={"run_id": str(current.id)},
            )
        run = PromptBoxRun.objects.create(
            website=website,
            created_by=user if getattr(user, "is_authenticated", False) else None,
            text=ask.text,
            samples=ask.samples,
            engines=keys,
            status=PromptBoxRun.STATUS_QUEUED,
            estimate=est,
            confirmed={
                "answers": confirmed["answers"],
                "ai_usd_max": usd4(confirmed["ai_usd_max"]),
            },
            answers_total=est["answers"],
        )
        PromptBoxAnswer.objects.bulk_create([
            PromptBoxAnswer(
                run=run, engine=key, provider=country_geo.provider_key(key), sample=sample,
            )
            for sample in range(1, ask.samples + 1)
            for key in keys
        ])
        if not inline:
            run_id = run.id
            transaction.on_commit(lambda: dispatch(run_id))
    if inline:
        # on_commit never fires inside a test transaction.
        execute_run(run.id)
    run.refresh_from_db()
    return run


# -- dispatch and execution ---------------------------------------------------------


def dispatch(run_id) -> None:
    """Hand a created run to its executor (PROMPT_BOX_RUN_MODE)."""
    mode = settings.PROMPT_BOX_RUN_MODE
    if mode == "inline":
        execute_run(run_id)
        return
    try:
        if mode == "thread" or (mode == "auto" and getattr(settings, "CELERY_TASK_ALWAYS_EAGER", False)):
            threading.Thread(
                target=execute_run_safely, args=(str(run_id),),
                name=f"prompt-box-{run_id}", daemon=True,
            ).start()
            return
        from apps.prompt_box.tasks import run_prompt_box

        run_prompt_box.apply_async(args=[str(run_id)], queue="ai")
    except Exception:
        # A run nobody will execute would hold the project's one active slot.
        logger.exception("prompt box run %s could not be started", run_id)
        PromptBoxAnswer.objects.filter(run_id=run_id, status=QUEUED).update(
            **_error_fields(FAILED, "internal_error"), finished_at=timezone.now(),
        )
        PromptBoxRun.objects.filter(id=run_id, status=PromptBoxRun.STATUS_QUEUED).update(
            error_code="internal_error", error_message=RUN_NOT_STARTED,
        )
        finalize(run_id)


def execute_run_safely(run_id) -> None:
    """execute_run on a thread of its own: fresh connections, never raises."""
    close_old_connections()
    try:
        execute_run(run_id)
    except Exception:
        logger.exception("prompt box run %s crashed", run_id)
        try:
            finalize(run_id)
        except Exception:
            logger.exception("prompt box run %s could not be finalized", run_id)
    finally:
        connection.close()


class _Job:
    """What every answer of one run needs, shared by the worker threads."""

    def __init__(self, run):
        website = run.website
        self.run = run
        self.website = website
        # Spend is attributed to the user who confirmed the run.
        self.user = run.created_by
        self.brand = getattr(website, "business_name", None) or website.name
        self.keywords = country_checks._keywords(website)
        self.quota = daily_quota()
        self.quota_key = str(website.id)
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
                    PromptBoxRun.objects.filter(id=self.run.id)
                    .values_list("cancel_requested", "status").first()
                )
                self._cancelled = row is None or bool(row[0]) or row[1] in TERMINAL
        return self._cancelled


def _order(answer) -> tuple:
    """Interleaved by engine: every engine's first answer, then the seconds..."""
    engines = {key: i for i, key in enumerate(country_geo.ENGINE_KEYS)}
    return (answer.sample, engines.get(answer.engine, 99))


def execute_run(run_id) -> None:
    """Ask a queued run's answers, then finalize. A run another worker holds,
    or one that was stopped before it started, is left alone."""
    run = PromptBoxRun.objects.select_related("website", "created_by").filter(id=run_id).first()
    if run is None or run.status in TERMINAL:
        return
    now = timezone.now()
    started = PromptBoxRun.objects.filter(id=run_id, status=PromptBoxRun.STATUS_QUEUED).update(
        status=PromptBoxRun.STATUS_RUNNING, started_at=now, heartbeat_at=now,
    )
    if not started:
        return
    job = _Job(run)
    answers = sorted(run.answers.filter(status=QUEUED), key=_order)
    if settings.PROMPT_BOX_RUN_MODE == "inline":
        # Other threads cannot see a test transaction's rows.
        for answer in answers:
            _run_answer(job, answer)
    else:
        workers = max(1, int(settings.PROMPT_BOX_CONCURRENCY))
        pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="prompt-box")
        try:
            futures = [pool.submit(_run_answer_in_thread, job, a) for a in answers]
            wait(futures)
        except BaseException:
            # Raised in this thread while it waited: Celery's soft time limit.
            # No further answer starts; the task finalizes the run.
            pool.shutdown(wait=False, cancel_futures=True)
            raise
        pool.shutdown()
        for future in futures:
            if future.exception() is not None:
                logger.error("prompt box run %s: a worker failed", run_id,
                             exc_info=future.exception())
    finalize(run_id)


def _run_answer_in_thread(job: _Job, answer) -> None:
    close_old_connections()
    try:
        _run_answer(job, answer)
    finally:
        connection.close()


def _error_fields(status: str, code: str) -> dict:
    return {"status": status, "error_code": code, "error_message": ANSWER_MESSAGES[code]}


def _run_answer(job: _Job, answer) -> None:
    if job.cancelled():
        PromptBoxAnswer.objects.filter(id=answer.id, status=QUEUED).update(
            **_error_fields(CANCELLED, "cancelled"), finished_at=timezone.now(),
        )
        return
    started = PromptBoxAnswer.objects.filter(id=answer.id, status=QUEUED).update(
        status=RUNNING, started_at=timezone.now(),
    )
    if not started:
        return
    try:
        fields = _ask(job, answer)
    except Exception:
        logger.exception("prompt box run %s: answer %s failed", job.run.id, answer.id)
        fields = _error_fields(FAILED, "internal_error")
    now = timezone.now()
    fields["finished_at"] = now
    # Only over our own "running": an answer the stale sweep already ended
    # keeps that end, as the finished run reports it.
    PromptBoxAnswer.objects.filter(id=answer.id, status=RUNNING).update(**fields)
    PromptBoxRun.objects.filter(id=job.run.id).exclude(status__in=TERMINAL).update(heartbeat_at=now)


def _ask(job: _Job, answer) -> dict:
    from apps.llm_ranking.providers import PROVIDERS

    key = country_geo.provider_key(answer.engine)
    fields = {"provider": key}
    cls = PROVIDERS.get(key)
    instance = cls() if cls is not None else None
    if instance is None or not instance.is_configured():
        return {**fields, **_error_fields(SKIPPED, "not_configured")}
    fields["model_id"] = str(getattr(instance, "model", "") or "")[:64]
    if not job.quota.consume(job.quota_key):
        return {**fields, **_error_fields(SKIPPED, "daily_limit")}

    run = job.run
    result = country_checks.query_with_retry(
        instance, run.text,
        user=job.user, website=job.website, audit_id=run.audit_id,
        role="prompt_box", module="llm_ranking",
        extra_metadata={"actor": "user", "feature": "prompt_box"},
        run_feature="prompt_box", run_key=f"promptbox:{answer.id}",
    )
    if result is None:
        return {**fields, **_error_fields(FAILED, "engine_error")}
    text = (result.text or "").strip() if result.succeeded else ""
    if not text:
        # An empty answer is not a measurement: it never reads as "not named".
        status, code = country_checks.engine_error(
            result.error if not result.succeeded else "empty_answer:",
        )
        return {**fields, **_error_fields(status, code)}

    analysis = HaikuExtractionService.extract(
        response_text=text, brand_name=job.brand, keywords=job.keywords,
        user=job.user, website=job.website, audit_id=run.audit_id,
    )
    mentioned = bool(analysis.get("is_mentioned"))
    sentiment = analysis.get("sentiment")
    context = str(analysis.get("mention_context") or "").strip() if mentioned else ""
    return {
        **fields,
        "status": DONE, "error_code": "", "error_message": "",
        "mentioned": mentioned,
        "rank": country_checks._rank(analysis.get("mention_rank")) if mentioned else None,
        "sentiment": sentiment if mentioned and sentiment in country_checks.SENTIMENTS else "",
        "brands": country_checks._brands(analysis, job.brand),
        # Gemini's grounding terms exclude its links from what may be kept.
        "citations": (
            country_checks._citations(result, analysis) if grounded_links_may_be_stored(key) else []
        ),
        "excerpt": context[:300] if context else text[:EXCERPT_CHARS],
        "answer_text": text,
        **search_query_fields(result),
        "extraction_model": str(analysis.get("extraction_model") or "")[:100],
    }


# -- finish, stop, sweep --------------------------------------------------------------


def _most_common_error(run) -> tuple[str, str]:
    pairs = Counter(
        run.answers.exclude(error_code="").exclude(error_code="cancelled")
        .values_list("error_code", "error_message")
    )
    if not pairs:
        return "internal_error", ANSWER_MESSAGES["internal_error"]
    (code, message), _ = pairs.most_common(1)[0]
    return code, message


def _counts(run) -> dict:
    """``answers_total``, ``answers_done`` and ``answers_failed`` (failed or
    skipped) of the run's rows."""
    counts = Counter(run.answers.values_list("status", flat=True))
    return {
        "answers_total": sum(counts.values()),
        "answers_done": counts.get(DONE, 0),
        "answers_failed": counts.get(FAILED, 0) + counts.get(SKIPPED, 0),
    }


def finalize(run_id, *, queued_only: bool = False):
    """End a run whose answers were all asked (or that will never run):
    answers left unfinished end ``cancelled`` after Stop, else ``failed``;
    then the status, the error, the cost and the finish time.
    ``queued_only`` (Stop of a queued run) leaves alone a run a worker started
    in the meantime: that worker sees Stop and finalizes it once its requests
    have returned, so their cost lands on the run."""
    with transaction.atomic():
        run = PromptBoxRun.objects.select_for_update().filter(id=run_id).first()
        if run is None or run.status in TERMINAL:
            return run
        if queued_only and run.status != PromptBoxRun.STATUS_QUEUED:
            return run
        now = timezone.now()
        stopped = run.cancel_requested
        run.answers.filter(status__in=(QUEUED, RUNNING)).update(
            **_error_fields(CANCELLED if stopped else FAILED, "cancelled" if stopped else "interrupted"),
            finished_at=now,
        )
        counts = _counts(run)
        total, done = counts["answers_total"], counts["answers_done"]
        error_code, error_message = run.error_code, run.error_message
        if stopped and done < total:
            status = PromptBoxRun.STATUS_CANCELLED
        elif total and done == total:
            status = PromptBoxRun.STATUS_DONE
        elif done:
            status = PromptBoxRun.STATUS_PARTIAL
        else:
            status = PromptBoxRun.STATUS_FAILED
            if not error_code:
                error_code, error_message = _most_common_error(run)
        PromptBoxRun.objects.filter(id=run.id).update(
            status=status,
            finished_at=now,
            heartbeat_at=now,
            ai_cost_usd=insights_runs.ledger_cost(run),
            error_code=error_code,
            error_message=error_message,
            updated_at=now,
            **counts,
        )
    run.refresh_from_db()
    return run


def cancel_run(run) -> PromptBoxRun:
    """Stop a run: its workers end their remaining answers ``cancelled``. A
    run no worker holds yet (queued) ends at once. Idempotent; a finished run
    is returned unchanged."""
    if run.status in TERMINAL:
        return run
    PromptBoxRun.objects.filter(id=run.id).update(cancel_requested=True)
    run.refresh_from_db()
    if run.status == PromptBoxRun.STATUS_QUEUED:
        # Checked again under the row lock: a worker may take it meanwhile.
        finalize(run.id, queued_only=True)
        run.refresh_from_db()
    return run


def sweep_stale(website) -> int:
    """Interrupt the project's runs that stopped reporting (no heartbeat for
    PROMPT_BOX_STALE_MINUTES). Called by every view that shows runs."""
    cutoff = timezone.now() - timedelta(minutes=settings.PROMPT_BOX_STALE_MINUTES)
    stale = list(
        PromptBoxRun.objects
        .filter(website=website, status__in=ACTIVE)
        .annotate(last_seen=Coalesce("heartbeat_at", "started_at", "created_at"))
        .filter(last_seen__lt=cutoff)
        .values_list("id", flat=True)
    )
    for run_id in stale:
        with transaction.atomic():
            run = PromptBoxRun.objects.select_for_update().filter(id=run_id).first()
            if run is None or run.status in TERMINAL:
                continue
            if (run.heartbeat_at or run.started_at or run.created_at) >= cutoff:
                continue
            now = timezone.now()
            run.answers.filter(status__in=(QUEUED, RUNNING)).update(
                **_error_fields(FAILED, "interrupted"), finished_at=now,
            )
            PromptBoxRun.objects.filter(id=run.id).update(
                status=PromptBoxRun.STATUS_INTERRUPTED,
                finished_at=now,
                error_code="interrupted",
                error_message=RUN_INTERRUPTED,
                ai_cost_usd=insights_runs.ledger_cost(run),
                updated_at=now,
                **_counts(run),
            )
    return len(stale)


# -- payloads ---------------------------------------------------------------------------


def _pct(value: float) -> float:
    return round(value * 100, 1)


def _answer_payload(row, full: bool) -> dict:
    done = row.status == DONE
    out = {
        "id": str(row.id),
        "sample": row.sample,
        "status": row.status,
        "mentioned": row.mentioned if done else None,
        "rank": row.rank if done and row.mentioned else None,
        "sentiment": row.sentiment or None,
        "excerpt": row.excerpt or None,
        "brands": list(row.brands or []),
        "citations": len(row.citations or []),
        "error": {"code": row.error_code, "message": row.error_message} if row.error_code else None,
    }
    if full:
        out["answer_text"] = row.answer_text
        out["citation_urls"] = list(row.citations or [])
        out["search_queries"] = row.search_queries
    return out


def _engine_payload(key: str, rows: list, *, answers: bool, full: bool) -> dict:
    """One engine's tally: shares are of the answers that came back (a failed
    answer is never "not named"), with a 95% Wilson range; all null with no
    answer."""
    answered = [r for r in rows if r.status == DONE]
    named = [r for r in answered if r.mentioned]
    n, m = len(answered), len(named)
    low, high = wilson_ci(m, n)
    ranks = [r.rank for r in named if r.rank]
    out = {
        "key": key,
        "label": country_geo.ENGINE_LABELS.get(key, key),
        "asked": len(rows),
        "answered": n,
        "named": m,
        "share_pct": _pct(m / n) if n else None,
        "share_low_pct": _pct(low) if n else None,
        "share_high_pct": _pct(high) if n else None,
        "avg_rank": round(sum(ranks) / len(ranks), 1) if ranks else None,
    }
    if answers:
        out["answers"] = [_answer_payload(r, full) for r in rows]
    return out


def run_payload(run, *, full: bool = False, summary: bool = False,
                tracked: dict | None = None) -> dict:
    """``Run`` (contract 4). ``full`` adds each answer's text, citation URLs
    and search queries; ``summary`` (the runs list) leaves the answers out."""
    rows = sorted(run.answers.all(), key=lambda r: r.sample)
    by_engine: dict[str, list] = {key: [] for key in run.engines or []}
    for row in rows:
        by_engine.setdefault(row.engine, []).append(row)
    statuses = Counter(r.status for r in rows)
    finished = run.status in TERMINAL
    # Stored when the run ended; read live from the usage ledger while it runs.
    ai_usd = run.ai_cost_usd if finished else insights_runs.ledger_cost(run)
    return {
        "id": str(run.id),
        "text": run.text,
        "samples": run.samples,
        "status": run.status,
        "created_at": insights_runs.iso(run.created_at),
        "finished_at": insights_runs.iso(run.finished_at),
        "progress": {
            "total": len(rows),
            "done": statuses.get(DONE, 0),
            "failed": statuses.get(FAILED, 0) + statuses.get(SKIPPED, 0),
            "running": statuses.get(RUNNING, 0),
        },
        "engines": [
            _engine_payload(key, items, answers=not summary, full=full)
            for key, items in by_engine.items()
        ],
        "cost": {"ai_usd": usd4(ai_usd)},
        "tracked": tracked_block(run.website_id, run.text, tracked),
        "error": {"code": run.error_code, "message": run.error_message} if run.error_code else None,
    }


def runs_list(website, limit: int = RUNS_LISTED) -> list[dict]:
    """The project's newest runs as summaries (no answers)."""
    runs = list(
        PromptBoxRun.objects.filter(website=website)
        .prefetch_related("answers")
        .order_by("-created_at")[:limit]
    )
    found = tracked_map(website.id, [r.text for r in runs])
    return [run_payload(r, summary=True, tracked=found) for r in runs]
