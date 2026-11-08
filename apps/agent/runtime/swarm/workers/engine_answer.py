"""engine_answer: one AI engine asked one prompt, then an analysis of its
answer (design section 8b).

The engine is asked through the prompt's EngineScan, which stores the
answer the way crawl_prompt does (result row, extraction, citations), so
the prompt page and Overview show it. The analysis (what the answer
recommends and why, and what the brand could change) is a second step:
when it fails the task is still done, with ``analysis_error``.
"""
from __future__ import annotations

from types import SimpleNamespace

from apps.agent.constants import WorkerType
from apps.agent.runtime.swarm.context import (
    ANALYSIS_NOT_STARTED,
    TaskJob,
    TaskStopped,
    WorkerOutput,
    failed,
    iso,
    skipped,
)
from apps.agent.tools.shaping import clip, host_of

MAX_BRANDS = 10
MAX_BRANDS_ABOVE = 6
MAX_CITATIONS = 8
SNIPPET_CHARS = 400
BRAND_CHARS = 80
PURPOSE = "engine_answer"
ALLOWANCE_MESSAGE = "The monthly AI allowance is used up."
PROMPT_MISSING = "This prompt is no longer tracked."
_BUSY = ("rate limit", "429", "quota", "overloaded", "circuit open")


def engine_label(provider: str | None) -> str:
    from apps.prompt_library.services.prompt_stats import MODEL_LABELS

    return MODEL_LABELS.get(provider or "", provider or "the engine")


def _key_setting(provider: str) -> str:
    from apps.llm_ranking.providers import PROVIDERS

    cls = PROVIDERS.get(provider)
    return getattr(cls, "api_key_setting", "") or f"The {engine_label(provider)} API key"


def _configured(provider: str) -> bool:
    from apps.llm_ranking.providers import PROVIDERS
    from apps.prompt_library.services.prompt_stats import is_configured

    cls = PROVIDERS.get(provider)
    return cls is not None and is_configured(cls)


def failure(raw: str) -> WorkerOutput:
    """An engine failure as the task reports it: the code from the kind
    of error, the message in public words (provider text stays in logs)."""
    from apps.agent.tools.research import public_engine_error

    text = (raw or "").strip().lower()
    message = public_engine_error(raw)
    if text.startswith("refusal:"):
        return failed("refused", message)
    if text.startswith(("max_tokens:", "pause_turn:", "empty_answer:")):
        return failed("upstream_error", message)
    if text.startswith("prompt_runs_exhausted"):
        return skipped("prompt_runs_exhausted", message)
    if "allowance" in text:
        return skipped("allowance_exceeded", message)
    if "not configured" in text or "not enabled" in text:
        return skipped("not_configured", message)
    if any(s in text for s in _BUSY):
        return failed("rate_limited", message)
    if "timeout" in text or "timed out" in text:
        return failed("timeout", message)
    return failed("upstream_error", message)


def _brands(analysis: dict, own_name: str) -> list[dict]:
    fold = (own_name or "").casefold()
    rows = []
    for c in analysis.get("competitors_mentioned") or []:
        name = str((c or {}).get("name") or "").strip() if isinstance(c, dict) else ""
        if not name or name.casefold() == fold:
            continue
        position = c.get("position")
        rows.append({"name": clip(name, BRAND_CHARS),
                     "position": position if isinstance(position, int) else None})
    rows.sort(key=lambda b: b["position"] or 999)
    return rows[:MAX_BRANDS]


def _brands_above(analysis: dict, mentioned: bool, rank, own_name: str) -> list[dict]:
    """The same rule as the prompt page (search_gap.overview._brands_above):
    brands listed ahead of you, or every brand when you are not named."""
    from apps.search_gap.services.overview import _brands_above as brands_above

    latest = SimpleNamespace(competitors_mentioned=analysis.get("competitors_mentioned") or [])
    rows = brands_above(latest, {"mentioned": mentioned, "rank": rank}, own_name or "")
    return [{"name": clip(r["name"], BRAND_CHARS), "position": r["position"]}
            for r in rows][:MAX_BRANDS_ABOVE]


def _citations(urls: list, website) -> list[dict]:
    from apps.search_gap.services.google_rank import is_same_site, site_apex

    apex = site_apex(website.url)
    out = []
    for url in urls or []:
        if not isinstance(url, str) or not url:
            continue
        domain = host_of(url)
        out.append({"url": url, "domain": domain, "is_yours": is_same_site(domain, apex)})
        if len(out) >= MAX_CITATIONS:
            break
    return out


def _analyze(job: TaskJob, *, label: str, question: str, answer_text: str,
             measurement: dict) -> tuple[dict | None, dict | None]:
    """(analysis, analysis_error). Stop during the analysis stops the task."""
    from apps.agent.llm.worker import ask_haiku
    from apps.agent.runtime.swarm.prompts import (
        ENGINE_SCHEMA,
        ENGINE_SYSTEM,
        EngineAnalysis,
        engine_content,
    )

    swarm_run = job.run
    job.reporter.step("Analyzing the answer")
    result = ask_haiku(
        run=swarm_run, task_id=job.task_id, worker=WorkerType.ENGINE_ANSWER, purpose=PURPOSE,
        system=ENGINE_SYSTEM,
        content=engine_content(swarm_run.website, swarm_run.competitors(), question=question,
                               measurement=measurement, answer=answer_text),
        schema=ENGINE_SCHEMA, output_model=EngineAnalysis, timeout=job.remaining(),
    )
    if result.ok:
        return result.data.model_dump(), None
    if result.code == "cancelled":
        if job.stopped():
            raise TaskStopped()
        return None, dict(ANALYSIS_NOT_STARTED)
    return None, result.error


def run(job: TaskJob) -> WorkerOutput:
    swarm_run = job.run
    inputs = job.inputs
    key = inputs["prompt_key"]
    provider = inputs.get("provider") or ""
    variant = inputs.get("variant") or None
    label = engine_label(provider)
    question = (swarm_run.prompts.get(key) or {}).get("text") or ""

    scan = swarm_run.engine_scan(key)
    if scan is None:
        return skipped("prompt_missing", PROMPT_MISSING)
    if not _configured(provider):
        return skipped("not_configured", f"{_key_setting(provider)} is not set.")
    job.check()
    answer = scan.ask(provider, variant, should_stop=job.stopped,
                      on_step=lambda _step: job.reporter.step(f"Reading {label}'s answer"))
    job.release_primary()
    if answer.stopped:
        raise TaskStopped()
    if answer.not_configured:
        return skipped("not_configured", f"{_key_setting(provider)} is not set.")
    if not answer.succeeded:
        return failure(answer.error)

    extraction = answer.analysis or {}
    mentioned = bool(extraction.get("is_mentioned"))
    rank = extraction.get("mention_rank") if mentioned else None
    rank = rank if isinstance(rank, int) and rank > 0 else None
    sentiment = extraction.get("sentiment") if mentioned else None
    own_name = swarm_run.website.name
    brands = _brands(extraction, own_name)

    analysis = analysis_error = None
    if (answer.response_text or "").strip():
        job.check()
        analysis, analysis_error = _analyze(
            job, label=label, question=question, answer_text=answer.response_text,
            measurement={"mentioned": mentioned, "rank": rank,
                         "brands": [{"name": b["name"], "position": b["position"]}
                                    for b in brands]},
        )

    if mentioned:
        line = f"Named #{rank}" if rank else "Named"
        if sentiment and sentiment != "not_mentioned":
            line += f" · {sentiment}"
        tone = "good" if rank and rank <= 3 else None
    else:
        line = "Not named"
        names = [b["name"] for b in brands[:2]]
        if names:
            line += f" · lists {', '.join(names)}"
        tone = "warn"
    detail = {
        "engine": inputs.get("engine"),
        "engine_label": label,
        "variant": variant,
        "model_id": answer.model_id,
        "prompt_text": question,
        "answered_at": iso(answer.created_at),
        "mentioned": mentioned,
        "rank": rank,
        "sentiment": sentiment,
        "brands": brands,
        "brands_above": _brands_above(extraction, mentioned, rank, own_name),
        "snippet": clip(extraction.get("mention_context") or "", SNIPPET_CHARS) if mentioned else "",
        "citations": _citations(answer.citations, swarm_run.website),
        "analysis": analysis,
        "analysis_error": analysis_error,
        "result_id": answer.result_id,
    }
    return WorkerOutput(status="done", result=line, tone=tone, detail=detail)
