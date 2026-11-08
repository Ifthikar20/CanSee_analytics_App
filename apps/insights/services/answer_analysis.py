"""Run kind ``answer_analysis`` (contract 3a-3b, backend 3b).

For each answer that names the brand: one Claude Haiku 5.5 request reads
its claims and themes (extract), then, when claims remain, one request
checks them against the brand's own evidence (verify). The server keeps
only what it can find word for word: claims and themes quote the answer,
verdicts quote the evidence (services.answer_prompts). Every request is
tagged with the run's audit id and re-checks the user's allowance first.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from decimal import Decimal

from django.conf import settings
from django.db import transaction
from django.db.models import Count
from django.utils import timezone

from apps.citations.services.url_analytics import model_key
from apps.insights.models import AnswerAnalysis, AnswerClaim, AnswerTheme
from apps.insights.services import answer_prompts as prompts
from apps.insights.services import evidence as evidence_service
from apps.insights.services import runs
from apps.llm_ranking.services import answer_window
from apps.search_gap.services.google_rank import site_apex
from core.exceptions import CanseeException

logger = logging.getLogger("apps")

KEY = "answer_analysis"
LABEL = "Answer analysis"
ANALYSIS_VERSION = "1"
REQUEST_TIMEOUT = 90
ENGINE_LABELS = {"chatgpt": "ChatGPT", "claude": "Claude", "gemini": "Gemini", "grok": "Grok",
                 "perplexity": "Perplexity"}
NOTHING_TO_RUN = "Every answer that names you in this period is already analyzed."
NOT_CONFIGURED = "The analysis model isn't configured on this server."
# HaikuResult codes are the contract's item codes already.
_ITEM_CODES = ("allowance_exceeded", "refused", "invalid_output", "rate_limited", "upstream_error")


def _invalid(field_name: str, message: str) -> CanseeException:
    return CanseeException(
        message, code="invalid", status_code=400,
        details={"field": field_name, "message": message},
    )


def tracked_prompts(website) -> list:
    from apps.prompt_library.models import BrandPrompt

    return list(
        BrandPrompt.objects.filter(website=website, is_archived=False)
        .select_related("prompt").order_by("created_at")
    )


def window_answers(website, brand_prompts, *, period: str, interval: str = "day",
                   engine=None, prompt_bp=None, fields=answer_window.RESULT_FIELDS) -> tuple:
    """(window, [(bp, result, bucket)] in the window, [(bp, result)] of the
    previous window), filtered by engine and prompt."""
    window = answer_window.Window(period, interval, timezone.now().date())
    current, previous = [], []
    for bp, result in answer_window.tracked_answers(
            website, brand_prompts, since=window.prev_start_at, fields=fields):
        if prompt_bp is not None and bp.id != prompt_bp.id:
            continue
        if engine is not None and model_key(result.provider) != engine:
            continue
        if result.created_at >= window.start_at:
            index = window.bucket(result.created_at)
            if index is not None:
                current.append((bp, result, index))
        else:
            previous.append((bp, result))
    return window, current, previous


def analysis_status(result_ids) -> dict:
    """``{result_id: status}`` of the current-version analyses."""
    ids = list(result_ids)
    out = {}
    for offset in range(0, len(ids), 1000):
        out.update(
            AnswerAnalysis.objects.filter(
                result_id__in=ids[offset:offset + 1000], version=ANALYSIS_VERSION,
            ).values_list("result_id", "status")
        )
    return out


def ai_configured() -> bool:
    return bool(getattr(settings, "ANTHROPIC_API_KEY", ""))


# -- shared by the Sentiment and Fact check payloads ---------------------------------


def engine_configured() -> dict:
    """``{provider key: configured}`` for the five engines."""
    from apps.llm_ranking.providers import PROVIDERS
    from apps.prompt_library.services.prompt_stats import is_configured

    return {
        key: is_configured(PROVIDERS[key]) if key in PROVIDERS else False
        for key in answer_window.ENGINE_ORDER
    }


def engines_payload(configured: dict) -> list[dict]:
    return [
        {"key": model_key(p), "label": ENGINE_LABELS[model_key(p)], "configured": configured[p]}
        for p in answer_window.ENGINE_ORDER
    ]


def shown_engines(configured: dict, current: list, engine) -> list[str]:
    """Public keys drawn: the filtered engine, else every configured engine
    and every engine that answered in the window, in display order."""
    if engine is not None:
        return [engine]
    answered = {model_key(result.provider) for _bp, result, _i in current}
    return [
        model_key(p) for p in answer_window.ENGINE_ORDER
        if configured.get(p) or model_key(p) in answered
    ]


def window_payload(window) -> dict:
    return {
        "period": window.period,
        "interval": window.interval,
        "start": window.start.isoformat(),
        "end": window.end.isoformat(),
        "buckets": [day.isoformat() for day in window.buckets],
    }


def selected_prompt(brand_prompts: list, prompt_id):
    """The tracked prompt of ``prompt_id`` (404 when it is not one), or None."""
    from core.exceptions import ResourceNotFound

    if prompt_id is None:
        return None
    wanted = uuid.UUID(str(prompt_id))
    bp = next((bp for bp in brand_prompts if bp.prompt_id == wanted), None)
    if bp is None:
        raise ResourceNotFound("Prompt not found.")
    return bp


def coverage(named: list, status: dict) -> dict:
    """``{named_answers, analyzed, not_analyzed, failed}``: analyzed counts
    finished analyses; not analyzed is every other answer that names you
    (failed and stopped ones included, as the next run picks them again)."""
    done = sum(1 for r in named if status.get(r.id) == AnswerAnalysis.STATUS_DONE)
    failed = sum(1 for r in named if status.get(r.id) == AnswerAnalysis.STATUS_FAILED)
    return {
        "named_answers": len(named),
        "analyzed": done,
        "not_analyzed": len(named) - done,
        "failed": failed,
    }


# -- plan ------------------------------------------------------------------------


@dataclass
class Plan:
    period: str
    engines: list | None
    prompt_ids: list | None
    reanalyze: bool
    # (brand prompt, result), newest first, at most INSIGHTS_ANALYSIS_MAX_ANSWERS.
    answers: list = field(default_factory=list)
    # Answers that matched before the cap.
    matched: int = 0


def _engines(raw) -> list | None:
    if raw is None:
        return None
    if not isinstance(raw, list):
        raise _invalid("engines", "Choose the AI engines as a list.")
    chosen = set()
    for item in raw:
        key = item.strip().lower() if isinstance(item, str) else ""
        if key not in answer_window.ENGINE_KEYS:
            raise _invalid("engines", f"{str(item)[:64]} is not an AI engine.")
        chosen.add(key)
    return [k for k in answer_window.ENGINE_KEYS if k in chosen]


def _prompt_ids(website, raw) -> list | None:
    if raw is None:
        return None
    if not isinstance(raw, list):
        raise _invalid("prompt_ids", "Choose the prompts as a list.")
    tracked = {str(bp.prompt_id) for bp in tracked_prompts(website)}
    out = []
    for item in raw:
        try:
            pid = str(uuid.UUID(str(item)))
        except (TypeError, ValueError, AttributeError):
            raise _invalid("prompt_ids", "Choose tracked prompts of this project.") from None
        if pid not in tracked:
            raise _invalid("prompt_ids", f"No tracked prompt with id {pid} in this project.")
        if pid not in out:
            out.append(pid)
    return out


def select(website, *, period: str, engines=None, prompt_ids=None, reanalyze=False) -> tuple:
    """(answers to analyze newest first, capped; how many matched)."""
    brand_prompts = tracked_prompts(website)
    _window, current, _previous = window_answers(website, brand_prompts, period=period)
    named = [
        (bp, result) for bp, result, _i in current
        if result.is_mentioned
        and (engines is None or model_key(result.provider) in engines)
        and (prompt_ids is None or str(bp.prompt_id) in prompt_ids)
    ]
    if not reanalyze:
        status = analysis_status(r.id for _bp, r in named)
        named = [(bp, r) for bp, r in named if status.get(r.id) != AnswerAnalysis.STATUS_DONE]
    named.sort(key=lambda pair: pair[1].created_at, reverse=True)
    cap = int(settings.INSIGHTS_ANALYSIS_MAX_ANSWERS)
    return named[:cap], len(named)


def validate(website, params: dict) -> Plan:
    period = params.get("period") or "30d"
    if period not in answer_window.PERIOD_DAYS:
        raise _invalid("period", "Choose a period of 7d, 30d or 90d.")
    reanalyze = params.get("reanalyze", False)
    if not isinstance(reanalyze, bool):
        raise _invalid("reanalyze", "reanalyze must be true or false.")
    plan = Plan(
        period=period,
        engines=_engines(params.get("engines")),
        prompt_ids=_prompt_ids(website, params.get("prompt_ids")),
        reanalyze=reanalyze,
    )
    plan.answers, plan.matched = select(
        website, period=period, engines=plan.engines, prompt_ids=plan.prompt_ids,
        reanalyze=reanalyze,
    )
    if not plan.answers:
        raise CanseeException(NOTHING_TO_RUN, code="nothing_to_run", status_code=400)
    return plan


def _answers_text(n: int) -> str:
    return f"{n} answer that names you" if n == 1 else f"{n} answers that name you"


def estimate(website, user, plan: Plan) -> dict:
    n = len(plan.answers)
    cap = int(settings.INSIGHTS_ANALYSIS_MAX_ANSWERS)
    counts = evidence_service.fact_counts(website)
    warnings = []
    if plan.matched > n:
        warnings.append({
            "code": "capped",
            "message": f"{plan.matched} answers match; this run analyzes the newest {cap}. "
                       "Run it again for the rest.",
        })
    if counts["facts_pending"]:
        warnings.append({
            "code": "facts_pending",
            "message": f"{counts['facts_pending']} brand facts are waiting for review and are "
                       "not used as evidence.",
        })
    if not counts["facts"]:
        warnings.append({
            "code": "no_facts",
            "message": "You have no approved brand facts, so claims are checked against your "
                       "pages only.",
        })
    blockers = []
    if not ai_configured():
        blockers.append({"code": "not_configured", "status": 400, "message": NOT_CONFIGURED})
    days = answer_window.PERIOD_DAYS[plan.period]
    return {
        "summary": f"{_answers_text(n)}, last {days} days",
        "units": {"answers": n},
        "ai": {
            "calls_max": 2 * n,
            "usd_max": Decimal(str(settings.INSIGHTS_ANALYSIS_USD_PER_ANSWER)) * n,
            "model_label": runs.HAIKU_LABEL,
        },
        "google": {"pages_max": 0},
        "fetches": {"pages_max": int(settings.INSIGHTS_EVIDENCE_PAGES)},
        "warnings": warnings,
        "blockers": blockers,
    }


def choices(website, user) -> dict:
    """RunOptions.choices for the default period (30 days)."""
    brand_prompts = tracked_prompts(website)
    _window, current, _previous = window_answers(website, brand_prompts, period="30d")
    named = [(bp, r) for bp, r, _i in current if r.is_mentioned]
    status = analysis_status(r.id for _bp, r in named)
    open_ = [(bp, r) for bp, r in named if status.get(r.id) != AnswerAnalysis.STATUS_DONE]
    by_engine, by_prompt = {}, {}
    for bp, result in open_:
        key = model_key(result.provider)
        by_engine[key] = by_engine.get(key, 0) + 1
        by_prompt[bp.id] = by_prompt.get(bp.id, 0) + 1
    counts = evidence_service.fact_counts(website)
    configured = engine_configured()
    answered = {model_key(r.provider) for _bp, r in named}
    return {
        "named_answers": len(named),
        "unanalyzed": len(open_),
        # Engines that are configured or named you in the period.
        "engines": [
            {"key": model_key(p), "label": ENGINE_LABELS[model_key(p)],
             "unanalyzed": by_engine.get(model_key(p), 0)}
            for p in answer_window.ENGINE_ORDER
            if configured.get(p) or model_key(p) in answered
        ],
        "prompts": [
            {"prompt_id": str(bp.prompt_id), "text": bp.prompt.text,
             "unanalyzed": by_prompt.get(bp.id, 0)}
            for bp in brand_prompts
        ],
        "evidence": {
            "facts": counts["facts"],
            "facts_pending": counts["facts_pending"],
            "pages_planned": evidence_service.planned_pages(website, fetch=False),
        },
    }


def plan_items(run, plan: Plan) -> int:
    """One queued analysis per answer. An earlier same-version analysis of
    the answer (not finished, or any with ``reanalyze``) is replaced."""
    result_ids = [result.id for _bp, result in plan.answers]
    with transaction.atomic():
        earlier = AnswerAnalysis.objects.filter(result_id__in=result_ids, version=ANALYSIS_VERSION)
        if not plan.reanalyze:
            earlier = earlier.exclude(status=AnswerAnalysis.STATUS_DONE)
        earlier.delete()
        AnswerAnalysis.objects.bulk_create([
            AnswerAnalysis(
                website=run.website, result=result, run=run, version=ANALYSIS_VERSION,
                status=AnswerAnalysis.STATUS_QUEUED, engine=model_key(result.provider),
                prompt=bp.prompt, answered_at=result.created_at,
            )
            for bp, result in plan.answers
        ])
    return len(plan.answers)


# -- execute -----------------------------------------------------------------------


def known_themes(website) -> list[str]:
    """The project's 40 most used theme labels."""
    rows = (
        AnswerTheme.objects.filter(
            website=website, analysis__version=ANALYSIS_VERSION,
            analysis__status=AnswerAnalysis.STATUS_DONE,
        )
        .values("label_key").annotate(n=Count("id")).order_by("-n", "label_key")
    )
    return [row["label_key"] for row in rows[:prompts.KNOWN_THEMES]]


class _Work:
    """What every analysis of one run shares."""

    def __init__(self, ctx):
        from apps.agent.conf import agent_conf

        website = ctx.website
        self.ctx = ctx
        self.brand = {"name": website.name, "domain": site_apex(website.url)}
        self.known = known_themes(website)
        self.evidence = evidence_service.gather(ctx)
        self.model = agent_conf().worker_model[:64]


def execute(ctx) -> None:
    work = _Work(ctx)
    analyses = list(
        AnswerAnalysis.objects.filter(run=ctx.run, status=AnswerAnalysis.STATUS_QUEUED)
        .select_related("result", "prompt").order_by("-answered_at")
    )
    with ctx.pool(int(settings.INSIGHTS_ANALYSIS_CONCURRENCY)) as pool:
        pool.map(lambda analysis: analyze(work, analysis), analyses)


def _end(ctx, analysis, status: str, code: str = "", *, started: bool = True) -> None:
    AnswerAnalysis.objects.filter(
        id=analysis.id, status__in=(AnswerAnalysis.STATUS_QUEUED, AnswerAnalysis.STATUS_RUNNING),
    ).update(status=status, error_code=code)
    ctx.end_item(status, error=code or None, started=started)


def analyze(work: _Work, analysis) -> None:
    """Extract, then verify, one answer."""
    from apps.agent.llm.worker import ask_haiku_standalone

    ctx = work.ctx
    if ctx.cancelled():
        _end(ctx, analysis, AnswerAnalysis.STATUS_CANCELLED, "cancelled", started=False)
        return
    started = AnswerAnalysis.objects.filter(
        id=analysis.id, status=AnswerAnalysis.STATUS_QUEUED,
    ).update(status=AnswerAnalysis.STATUS_RUNNING)
    if not started:
        return
    ctx.start_item()
    result = analysis.result
    answer_text = result.response_text or ""
    question = analysis.prompt.text if analysis.prompt else result.prompt
    common = {
        "user": ctx.user, "website": ctx.website, "timeout": REQUEST_TIMEOUT,
        "audit_id": ctx.audit_id, "role": KEY,
        "max_tokens": int(settings.INSIGHTS_ANALYSIS_MAX_TOKENS), "should_stop": ctx.cancelled,
    }
    extracted = ask_haiku_standalone(
        system=prompts.EXTRACT_SYSTEM,
        content=prompts.extract_content(
            brand=work.brand, question=question, known_themes=work.known, answer=answer_text,
        ),
        schema=prompts.EXTRACT_SCHEMA, output_model=prompts.Extraction,
        step=f"{ctx.run.id}:{result.public_id}:extract", **common,
    )
    if not extracted.ok:
        if extracted.code == "cancelled":
            _end(ctx, analysis, AnswerAnalysis.STATUS_CANCELLED, "cancelled")
        else:
            code = extracted.code if extracted.code in _ITEM_CODES else "upstream_error"
            _end(ctx, analysis, AnswerAnalysis.STATUS_FAILED, code)
        return

    claims, themes = prompts.validate_extraction(extracted.data, answer_text)
    rows = _save(analysis, claims, themes)
    verified = False
    if rows:
        verified = _verify(work, analysis, rows, common)
    AnswerAnalysis.objects.filter(id=analysis.id, status=AnswerAnalysis.STATUS_RUNNING).update(
        status=AnswerAnalysis.STATUS_DONE, error_code="", claims_count=len(rows),
        themes_count=len(themes), verified=verified, model=work.model,
    )
    ctx.end_item("done")


def _save(analysis, claims: list[dict], themes: list[dict]) -> list:
    shared = {
        "website_id": analysis.website_id, "engine": analysis.engine,
        "prompt_id": analysis.prompt_id, "answered_at": analysis.answered_at,
    }
    with transaction.atomic():
        rows = AnswerClaim.objects.bulk_create([
            AnswerClaim(analysis=analysis, text=c["text"][:300], text_key=c["text_key"],
                        topic=c["topic"], quote=c["quote"], **shared)
            for c in claims
        ])
        AnswerTheme.objects.bulk_create([
            AnswerTheme(analysis=analysis, label=t["label"][:60], label_key=t["label_key"],
                        tone=t["tone"], quote=t["quote"], **shared)
            for t in themes
        ])
    return rows


def _verify(work: _Work, analysis, rows: list, common: dict) -> bool:
    """Check the saved claims; True when the check came back."""
    from apps.agent.llm.worker import ask_haiku_standalone

    ctx = work.ctx
    sent = [{"id": f"c{i}", "text": row.text, "topic": row.topic, "row": row}
            for i, row in enumerate(rows, 1)]
    if work.evidence.empty:
        # Nothing to check against: no claim is covered by the evidence.
        for row in rows:
            row.verdict = AnswerClaim.VERDICT_NOT_VERIFIABLE
        AnswerClaim.objects.bulk_update(rows, ["verdict"])
        return True
    facts, pages = evidence_service.for_answer(work.evidence, sent)
    checked = ask_haiku_standalone(
        system=prompts.VERIFY_SYSTEM,
        content=prompts.verify_content(brand=work.brand, claims=sent, facts=facts, pages=pages),
        schema=prompts.VERIFY_SCHEMA, output_model=prompts.Verification,
        step=f"{ctx.run.id}:{analysis.result.public_id}:verify", **common,
    )
    if not checked.ok:
        return False
    sources = {f["id"]: {"kind": "fact", "text": f["text"], "url": f["source_url"],
                         "fact_id": f["fact_id"]} for f in facts}
    sources.update({p["id"]: {"kind": "page", "text": p["text"], "url": p["final_url"],
                              "fact_id": None} for p in pages})
    verdicts = prompts.validate_verdicts(checked.data, {c["id"] for c in sent}, sources)
    changed = []
    for claim in sent:
        found = verdicts.get(claim["id"])
        if found is None:
            continue
        row = claim["row"]
        row.verdict = found["verdict"]
        row.evidence_kind = found["evidence_kind"]
        row.evidence_url = found["evidence_url"]
        row.evidence_fact_id = found["evidence_fact_id"]
        row.evidence_quote = found["evidence_quote"]
        row.note = found["note"]
        changed.append(row)
    if changed:
        AnswerClaim.objects.bulk_update(changed, [
            "verdict", "evidence_kind", "evidence_url", "evidence_fact", "evidence_quote", "note",
        ])
    return True


KIND = runs.Kind(
    key=KEY, label=LABEL, queue="ai", validate=validate, estimate=estimate, choices=choices,
    plan_items=plan_items, execute=execute,
)
runs.register(KIND)
