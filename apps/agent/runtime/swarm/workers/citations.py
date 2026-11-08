"""citations: the sources the AI engines (and Google's AI Overview) cited
for one prompt, counted per site, then an analysis of where the brand
could get cited (design section 8e).

The count is the measurement: when the analysis fails the task is still
done, with ``analysis_error``.
"""
from __future__ import annotations

from apps.agent.constants import AI_OVERVIEW_SOURCE, AI_OVERVIEW_SOURCE_LABEL, WorkerType
from apps.agent.runtime.swarm.context import (
    ANALYSIS_NOT_STARTED,
    TaskJob,
    TaskStopped,
    WorkerOutput,
    plural,
    skipped,
)
from apps.agent.tools.shaping import host_of

PURPOSE = "citations"
MAX_SOURCES = 15
# The engines list of a source cited by Google's AI Overview.
AI_OVERVIEW = AI_OVERVIEW_SOURCE
AI_OVERVIEW_LABEL = AI_OVERVIEW_SOURCE_LABEL
NOTHING_CITED = "No engine cited a source for this prompt."
NO_ANSWER = "No AI engine answered this prompt, so there were no citations to collect."


def _engine_order(key: str, catalog: list[str]) -> int:
    return catalog.index(key) if key in catalog else len(catalog)


def engines_answered(swarm_run, key: str) -> bool:
    """Whether any engine task of the prompt is done (its newest result):
    only then does an empty citation list measure "nothing cited"."""
    return any(swarm_run.done(spec["id"]) is not None
               for spec in swarm_run.prompt_specs(key, WorkerType.ENGINE_ANSWER))


def collect(swarm_run, key: str) -> tuple[list[tuple[str, str]], int]:
    """(url, engine key) of every citation of the prompt's done engine
    tasks, then of the AI Overview of its first Google page; and how many
    AI Overview references there were."""
    entries: list[tuple[str, str]] = []
    for spec in swarm_run.prompt_specs(key, WorkerType.ENGINE_ANSWER):
        task = swarm_run.done(spec["id"])
        detail = (task or {}).get("detail") or {}
        engine = detail.get("engine") or (spec.get("inputs") or {}).get("engine")
        for citation in detail.get("citations") or []:
            if citation.get("url"):
                entries.append((citation["url"], engine))
    page1 = swarm_run.done(f"google.{key}.page1")
    overview = ((page1 or {}).get("detail") or {}).get("ai_overview") or {}
    references = [r for r in overview.get("references") or [] if r.get("url")]
    entries.extend((r["url"], AI_OVERVIEW) for r in references)
    return entries, len(references)


def aggregate(swarm_run, entries: list[tuple[str, str]], catalog: list[str]) -> list[dict]:
    """One row per registrable domain, most cited first."""
    from apps.citations.services.url_normalizer import extract_apex_domain
    from apps.search_gap.services.google_rank import is_same_site, site_apex

    apex = site_apex(swarm_run.website.url)
    competitors = swarm_run.competitors()
    rows: dict[str, dict] = {}
    for url, engine in entries:
        host = host_of(url)
        domain = extract_apex_domain(host) or host
        if not domain:
            continue
        row = rows.get(domain)
        if row is None:
            row = rows[domain] = {
                "domain": domain,
                "citations": 0,
                "engines": [],
                "is_yours": is_same_site(domain, apex),
                "is_competitor": domain in competitors,
                "competitor_name": competitors.get(domain),
                "example_url": url,
            }
        row["citations"] += 1
        if engine and engine not in row["engines"]:
            row["engines"].append(engine)
    for row in rows.values():
        row["engines"].sort(key=lambda key: _engine_order(key, catalog))
    return sorted(rows.values(), key=lambda r: (-r["citations"], r["domain"]))


def run(job: TaskJob) -> WorkerOutput:
    from apps.agent.llm.worker import ask_haiku
    from apps.agent.runtime.swarm.prompts import (
        SOURCES_SCHEMA,
        SOURCES_SYSTEM,
        SourcesAnalysis,
        sources_content,
    )
    from apps.agent.runtime.swarm.workers.engine_answer import engine_label
    from apps.agent.tools.research import engine_catalog

    swarm_run = job.run
    key = job.inputs["prompt_key"]
    entries, overview_references = collect(swarm_run, key)
    if not entries:
        # Engines that failed or were stopped cited nothing because they did
        # not answer: that is not a measured "none cited".
        if not engines_answered(swarm_run, key):
            return skipped("dependency_failed", NO_ANSWER)
        return skipped("nothing_to_analyze", NOTHING_CITED)
    catalog = engine_catalog()
    labels = {e["key"]: e["label"] for e in catalog}
    labels[AI_OVERVIEW] = AI_OVERVIEW_LABEL
    rows = aggregate(swarm_run, entries, [e["key"] for e in catalog])
    sources = rows[:MAX_SOURCES]
    you_cited = any(r["is_yours"] for r in rows)

    job.check()
    job.reporter.step("Analyzing the sources")
    question = (swarm_run.prompts.get(key) or {}).get("text") or ""
    listed = [
        {"domain": r["domain"], "citations": r["citations"],
         "engines": [labels.get(e) or engine_label(e) for e in r["engines"]],
         "yours": r["is_yours"], "competitor": r["competitor_name"]}
        for r in sources
    ]
    result = ask_haiku(
        run=swarm_run, task_id=job.task_id, worker=WorkerType.CITATIONS, purpose=PURPOSE,
        system=SOURCES_SYSTEM,
        content=sources_content(swarm_run.website, swarm_run.competitors(), question=question,
                                sources=listed),
        schema=SOURCES_SCHEMA, output_model=SourcesAnalysis, timeout=job.remaining(),
    )
    job.release_primary()
    analysis = analysis_error = None
    if result.ok:
        domains = {r["domain"] for r in sources}
        analysis = {
            "headline": result.data.headline,
            "opportunities": [o.model_dump() for o in result.data.opportunities
                              if o.domain in domains],
        }
    elif result.code == "cancelled":
        if job.stopped():
            raise TaskStopped()
        analysis_error = dict(ANALYSIS_NOT_STARTED)
    else:
        analysis_error = result.error

    detail = {
        "total_citations": sum(r["citations"] for r in rows),
        "you_cited": you_cited,
        "overview_references": overview_references,
        "sources": sources,
        "analysis": analysis,
        "analysis_error": analysis_error,
    }
    top = sources[0]
    cited = "you're cited" if you_cited else "you're not cited"
    line = (f"{plural(len(rows), 'source')} · {cited} · "
            f"{top['domain']} cited {plural(top['citations'], 'time')}")
    return WorkerOutput(status="done", result=line, tone="good" if you_cited else "warn",
                        detail=detail)
