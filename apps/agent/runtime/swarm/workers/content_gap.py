"""content_gap: the analysis model compares the brand's page with the
competitor pages read for one prompt (design section 8d).

It reads the pages its prompt's page tasks read in this execution (their
text and SEO facts) or, for page tasks carried from an earlier version,
the summaries that version saved. Every gap names its evidence; a gap's
target must be one of the compared pages, and a gap without an issue,
evidence or fix is dropped.
"""
from __future__ import annotations

from apps.agent.constants import WorkerType
from apps.agent.runtime.swarm.context import (
    ANALYSIS_NOT_STARTED,
    TaskJob,
    TaskStopped,
    WorkerOutput,
    failed,
    plural,
    skipped,
)
from apps.agent.tools.shaping import clip, host_of

PURPOSE = "content_gap"
MAX_COMPETITOR_PAGES = 2
MAX_GOOGLE_RESULTS = 10
MAX_REASONS = 9
HEADLINE_CHARS = 70
NO_OWN_PAGE = "Your page could not be read."
NO_COMPETITOR_PAGE = "No competitor page to compare with."
SAVED_NOTE = "Compared with the page summaries an earlier version saved."
_FACTS = ("title", "meta_description", "h1", "h2", "word_count", "structured_data",
          "canonical")


def _competitor_tasks(swarm_run, key: str) -> list[dict]:
    out = []
    for spec in swarm_run.prompt_specs(key, WorkerType.PAGE_INSPECT):
        if (spec.get("inputs") or {}).get("role") != "competitor":
            continue
        task = swarm_run.done(spec["id"])
        if task is not None:
            out.append(task)
    return out[:MAX_COMPETITOR_PAGES]


def first_step(spec: dict, swarm_run) -> str:
    key = (spec.get("inputs") or {}).get("prompt_key")
    for task in _competitor_tasks(swarm_run, key):
        domain = (task.get("detail") or {}).get("domain")
        if domain:
            return f"Comparing your page with {domain}"
    return "Comparing the pages"


def _page(swarm_run, task: dict, role: str) -> dict:
    detail = task.get("detail") or {}
    seo = detail.get("seo") or {}
    with swarm_run.lock:
        read_now = swarm_run.page_texts.get(task["id"])
    return {
        "role": role,
        "url": detail.get("final_url") or detail.get("url") or "",
        "title": detail.get("title") or "",
        "domain": detail.get("domain") or "",
        "facts": {name: seo.get(name) for name in _FACTS},
        "text": read_now["text"] if read_now else (detail.get("excerpt") or ""),
        "saved": read_now is None,
    }


def _google(swarm_run, key: str) -> dict:
    captures = swarm_run.serp_captures(key)
    positions = [p for c in captures for p in c.get("brand_positions") or []]
    others = [
        {"position": r.get("position"), "domain": r.get("domain") or "",
         "competitor": r.get("competitor_name")}
        for c in captures for r in c.get("results") or [] if not r.get("is_yours")
    ][:MAX_GOOGLE_RESULTS]
    return {
        "your_position": min(positions) if positions else None,
        "results_searched": 10 * len(captures),
        "others": others,
    }


def _engine_reasons(swarm_run, key: str) -> list[str]:
    reasons = []
    for spec in swarm_run.prompt_specs(key, WorkerType.ENGINE_ANSWER):
        task = swarm_run.done(spec["id"])
        detail = (task or {}).get("detail") or {}
        for reason in ((detail.get("analysis") or {}).get("reasons") or []):
            reasons.append(f"{detail.get('engine_label') or spec.get('label')}: {reason}")
    return reasons[:MAX_REASONS]


def run(job: TaskJob) -> WorkerOutput:
    from apps.agent.llm.worker import ask_haiku
    from apps.agent.runtime.swarm.prompts import GAP_SCHEMA, GAP_SYSTEM, GapAnalysis, gap_content

    swarm_run = job.run
    key = job.inputs["prompt_key"]
    own = swarm_run.done(f"site.{key}.page")
    if own is None:
        return skipped("nothing_to_analyze", NO_OWN_PAGE)
    competitors = _competitor_tasks(swarm_run, key)
    if not competitors:
        return skipped("nothing_to_analyze", NO_COMPETITOR_PAGE)
    pages = [_page(swarm_run, own, "own"), *(_page(swarm_run, t, "competitor")
                                             for t in competitors)]
    job.reporter.step(f"Comparing your page with {pages[1]['domain'] or host_of(pages[1]['url'])}")
    question = (swarm_run.prompts.get(key) or {}).get("text") or ""
    content = gap_content(swarm_run.website, swarm_run.competitors(), question=question,
                          google=_google(swarm_run, key),
                          engine_reasons=_engine_reasons(swarm_run, key), pages=pages)
    job.check()
    job.reporter.step("Analyzing the gaps")
    result = ask_haiku(run=swarm_run, task_id=job.task_id, worker=WorkerType.CONTENT_GAP,
                       purpose=PURPOSE, system=GAP_SYSTEM, content=content, schema=GAP_SCHEMA,
                       output_model=GapAnalysis, timeout=job.remaining())
    job.release_primary()
    if not result.ok:
        if result.code == "cancelled":
            if job.stopped():
                raise TaskStopped()
            return failed(ANALYSIS_NOT_STARTED["code"], ANALYSIS_NOT_STARTED["message"])
        if result.code == "allowance_exceeded":
            return skipped("allowance_exceeded", result.message)
        return failed(result.code, result.message)

    compared = {p["url"] for p in pages if p["url"]}
    gaps = []
    for gap in result.data.gaps:
        if not (gap.issue.strip() and gap.evidence.strip() and gap.fix.strip()):
            continue
        gaps.append({
            "issue": gap.issue,
            "evidence": gap.evidence,
            "fix": gap.fix,
            "target": gap.target if gap.target in compared else "",
            "severity": gap.severity,
        })
    headline = result.data.headline
    detail = {
        "compared": [{"role": p["role"], "url": p["url"], "title": clip(p["title"], 200)}
                     for p in pages],
        "headline": headline,
        "gaps": gaps,
        "analysis_error": None,
        "note": SAVED_NOTE if any(p["saved"] for p in pages) else None,
    }
    if gaps:
        line = plural(len(gaps), "gap")
        if headline:
            line += f" · {clip(headline, HEADLINE_CHARS)}"
    else:
        line = "No gaps found"
    return WorkerOutput(status="done", result=line, tone="warn" if gaps else None,
                        detail=detail)
