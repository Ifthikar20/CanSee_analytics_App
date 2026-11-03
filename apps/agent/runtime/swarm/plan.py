"""A research plan expanded into categories and tasks (design section 6).

Pure functions: no database writes, and reads only through what the
caller passes in (the engine catalog, whether Google results are
configured, the resolved prompts). The expansion is deterministic, so a
re-run of the same swarm runs the same task ids.

Task order (the order of a part's tasks and the dispatch priority): by
prompt, then within a prompt the Google pages, the engines (catalog
order), the competitor pages, your page, the content gaps and the cited
sources. Every dependency of a task comes before it in that order.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from decimal import Decimal

from apps.agent.constants import SwarmCategory, SwarmScope, WorkerType
from apps.agent.tools.shaping import clip, host_of

CATEGORY_ORDER = ("google", "engines", "competitors", "site", "sources")
ENGINE_KEY_ORDER = ("chatgpt", "claude", "gemini", "grok", "perplexity")
WORKER_ORDER = {"serp_page": 0, "engine_answer": 1, "page_inspect_competitor": 2,
                "page_inspect_own": 3, "content_gap": 4, "citations": 5}
# Upper bounds used to check a plan against the user's AI allowance.
ENGINE_TASK_USD = Decimal("0.02")     # one engine answer plus its extraction
HAIKU_TASK_USD = Decimal("0.004")     # one worker analysis
REPORT_RESERVE_USD = Decimal("0.25")  # the orchestrator's report after the tool result

PAGE_TARGET_COMPETITOR = "Chosen from Google results"
PAGE_TARGET_OWN = "Your best-ranking page"
PAGE_TARGET_HOMEPAGE = "Your homepage"
SCOPE_LABEL_ALL = "All agents"
SCOPE_PROMPT_CHARS = 60
NOTE_PROMPT_CHARS = 60

SERP_NOT_CONFIGURED = (
    "Google results are not available (no SERPAPI_KEY or SCRAPERAPI_KEY), so no Google pages "
    "or competitor pages were planned."
)
SERP_NOT_CONFIGURED_HOMEPAGE = " Your homepage is read instead."

_SLUG_NOISE = re.compile(r"[^a-z0-9-]")
_DOMAIN = re.compile(
    r"^(?=.{3,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z][a-z0-9-]{0,62}$"
)


@dataclass(frozen=True)
class ResolvedPrompt:
    """One plan prompt, resolved to a tracked prompt by the tool handler."""

    key: str
    prompt_id: str
    brand_prompt_id: str
    text: str
    location: str
    model_variants: tuple[str, ...]
    tracked_now: bool


@dataclass(frozen=True)
class PlanOptions:
    title: str
    google_pages: int = 1
    # None: every configured engine.
    engines: tuple[str, ...] | None = None
    competitor_pages: int = 1
    # Competitor domains to prefer when choosing competitor pages. Entries
    # that are not domains are dropped with a ``competitor_dropped`` note.
    competitors: tuple[str, ...] = ()
    content_gaps: bool = True
    citations: bool = True


@dataclass
class Expansion:
    plan: dict              # AgentSwarm.plan (contract 3c)
    categories: list[dict]  # contract 5b: categories with tasks, with their notes
    tasks: list[dict]       # TaskSpec (contract 3d), in task order


# ── Helpers ──────────────────────────────────────────────────────────────


def _join(items: list[str]) -> str:
    """"A", "A and B", "A, B and C"."""
    items = [i for i in items if i]
    if len(items) <= 1:
        return "".join(items)
    return f"{', '.join(items[:-1])} and {items[-1]}"


def _slug(model_id: str) -> str:
    return _SLUG_NOISE.sub("-", (model_id or "").lower())


def _quoted(text: str, limit: int = NOTE_PROMPT_CHARS) -> str:
    return f"“{clip(' '.join((text or '').split()), limit)}”"


def competitor_domain(raw: str) -> str | None:
    """The registrable domain a competitor entry names ("atlassian.com",
    "https://www.atlassian.com/software"), or None when it names none."""
    from apps.citations.services.url_normalizer import extract_apex_domain

    text = (raw or "").strip().lower()
    if not text:
        return None
    host = host_of(text if "://" in text else f"https://{text}")
    apex = extract_apex_domain(host)
    return apex if apex and _DOMAIN.match(apex) else None


def _variant_catalog() -> dict[str, dict[str, tuple[str, bool]]]:
    """provider -> model_id -> (label, is_default)."""
    from apps.llm_ranking.providers import MODEL_VARIANTS

    return {
        provider: {model_id: (label, is_default) for label, model_id, is_default in rows}
        for provider, rows in MODEL_VARIANTS.items()
    }


@dataclass(frozen=True)
class _EngineAsk:
    key: str
    provider: str
    label: str
    variant: str | None
    task_id_suffix: str


def _selection(prompt: ResolvedPrompt, catalog: dict, variants: dict
               ) -> tuple[list[_EngineAsk], list[str], list[str]]:
    """What a prompt's model selection asks: (asks of compared engines,
    in selection order, labels of every selected model, labels of the
    selected models the research does not compare)."""
    from apps.llm_ranking.providers import PROVIDERS, parse_variant

    asks: list[_EngineAsk] = []
    labels: list[str] = []
    not_compared: list[str] = []
    seen: set[str] = set()
    by_provider = {e["provider"]: e for e in catalog.values()}
    for raw in prompt.model_variants:
        vid = str(raw or "").strip()
        parsed = parse_variant(vid) if vid else None
        if parsed is None or vid in seen:
            continue
        seen.add(vid)
        provider, model_id = parsed
        label, is_default = variants.get(provider, {}).get(model_id, (model_id, False))
        labels.append(label)
        engine = by_provider.get(provider)
        if provider not in PROVIDERS or engine is None:
            not_compared.append(label)
            continue
        if is_default:
            asks.append(_EngineAsk(engine["key"], provider, engine["label"], None, engine["key"]))
        else:
            asks.append(_EngineAsk(engine["key"], provider, f"{engine['label']} · {label}", vid,
                                   f"{engine['key']}-{_slug(model_id)}"))
    return asks, labels, not_compared


def _note(level: str, code: str, text: str) -> dict:
    return {"level": level, "code": code, "text": text}


def _spec(task_id: str, category: str, prompt_key: str, worker: str, label: str, target: str,
          depends_on: list[str], inputs: dict) -> dict:
    return {
        "id": task_id,
        "category": category,
        "subcategory": prompt_key,
        "worker": worker,
        "label": label,
        "target": target,
        "depends_on": depends_on,
        "inputs": inputs,
    }


# ── Expansion ────────────────────────────────────────────────────────────


def build_expansion(website, prompts: list[ResolvedPrompt], options: PlanOptions, *,
                    engines: list[dict], serp_configured: bool, now_iso: str) -> Expansion:
    """The plan, categories and task specs for ``prompts`` (algorithm in
    design section 6a). ``engines`` is research.engine_catalog()."""
    catalog = {e["key"]: e for e in engines}
    order = [e["key"] for e in engines]
    wanted = [k for k in order if options.engines is None or k in set(options.engines)]
    configured = {k for k in order if catalog[k]["configured"]}
    variants = _variant_catalog()

    prefer: list[str] = []
    dropped: list[str] = []
    for raw in options.competitors:
        domain = competitor_domain(raw)
        if domain is None:
            dropped.append(_quoted(raw, 40))
        elif domain not in prefer:
            prefer.append(domain)

    tasks: list[dict] = []
    engine_notes: list[dict] = []
    asked: set[str] = set()
    for prompt in prompts:
        key = prompt.key
        google_ids: list[str] = []
        if serp_configured:
            for page in range(1, options.google_pages + 1):
                task_id = f"google.{key}.page{page}"
                google_ids.append(task_id)
                tasks.append(_spec(task_id, SwarmCategory.GOOGLE.value, key, WorkerType.SERP_PAGE,
                                   f"Google page {page}", prompt.text, [],
                                   {"prompt_key": key, "page": page}))

        if prompt.model_variants:
            selected, labels, not_compared = _selection(prompt, catalog, variants)
            asks = [a for a in selected if a.key in wanted and a.key in configured]
            excluded = [catalog[k]["label"] for k in wanted
                        if k in configured and all(a.key != k for a in selected)]
            if excluded:
                engine_notes.append(_note(
                    "info", "engines_not_selected",
                    f"The prompt {_quoted(prompt.text)} selects only "
                    f"{_join(labels) or 'models that are not available'}, so {_join(excluded)} "
                    f"{'was' if len(excluded) == 1 else 'were'} not asked for it.",
                ))
            if not_compared:
                engine_notes.append(_note(
                    "info", "variant_not_compared",
                    f"{_join(not_compared)} {'is' if len(not_compared) == 1 else 'are'} selected "
                    f"for {_quoted(prompt.text)} but the research agents compare only "
                    f"{_join([catalog[k]['label'] for k in order])}, so "
                    f"{'it was' if len(not_compared) == 1 else 'they were'} not asked.",
                ))
            # Selection order would scatter an engine's variants; the
            # catalog order keeps the rows of one engine together.
            asks.sort(key=lambda a: order.index(a.key))
        else:
            asks = [_EngineAsk(k, catalog[k]["provider"], catalog[k]["label"], None, k)
                    for k in wanted if k in configured]
        engine_ids: list[str] = []
        for ask in asks:
            task_id = f"engines.{key}.{ask.task_id_suffix}"
            if task_id in engine_ids:
                continue
            engine_ids.append(task_id)
            asked.add(ask.key)
            tasks.append(_spec(task_id, SwarmCategory.ENGINES.value, key, WorkerType.ENGINE_ANSWER,
                               ask.label, prompt.text, [],
                               {"prompt_key": key, "engine": ask.key, "provider": ask.provider,
                                "variant": ask.variant}))

        competitor_ids: list[str] = []
        if serp_configured and options.competitor_pages > 0:
            for slot in range(1, options.competitor_pages + 1):
                task_id = f"competitors.{key}.c{slot}"
                competitor_ids.append(task_id)
                tasks.append(_spec(task_id, SwarmCategory.COMPETITORS.value, key,
                                   WorkerType.PAGE_INSPECT, f"Competitor page {slot}",
                                   PAGE_TARGET_COMPETITOR, list(google_ids),
                                   {"prompt_key": key, "role": "competitor", "slot": slot,
                                    "prefer": list(prefer)}))

        if options.content_gaps:
            page_id = f"site.{key}.page"
            tasks.append(_spec(page_id, SwarmCategory.SITE.value, key, WorkerType.PAGE_INSPECT,
                               "Your page",
                               PAGE_TARGET_OWN if serp_configured else PAGE_TARGET_HOMEPAGE,
                               list(google_ids), {"prompt_key": key, "role": "own"}))
            tasks.append(_spec(f"site.{key}.gaps", SwarmCategory.SITE.value, key,
                               WorkerType.CONTENT_GAP, "Content gaps", prompt.text,
                               [page_id, *competitor_ids, *engine_ids], {"prompt_key": key}))

        if options.citations and engine_ids:
            depends = list(engine_ids)
            if f"google.{key}.page1" in google_ids:
                depends.append(f"google.{key}.page1")
            tasks.append(_spec(f"sources.{key}.cited", SwarmCategory.SOURCES.value, key,
                               WorkerType.CITATIONS, "Cited sources", prompt.text, depends,
                               {"prompt_key": key}))

    not_configured = [k for k in wanted if k not in configured]
    if not_configured:
        labels = [catalog[k]["label"] for k in not_configured]
        engine_notes.insert(0, _note(
            "info", "engines_not_configured",
            f"{_join(labels)} {'is' if len(labels) == 1 else 'are'} not configured, so "
            f"{'it was' if len(labels) == 1 else 'they were'} not asked.",
        ))
    notes: dict[str, list[dict]] = {"engines": engine_notes}
    if dropped:
        notes["competitors"] = [_note(
            "info", "competitor_dropped",
            f"{_join(dropped)} {'is not a domain' if len(dropped) == 1 else 'are not domains'}, "
            f"so {'it was' if len(dropped) == 1 else 'they were'} not used to choose "
            "competitor pages.",
        )]
    categories = _categories(prompts, tasks, notes)
    if not serp_configured and categories:
        text = SERP_NOT_CONFIGURED + (SERP_NOT_CONFIGURED_HOMEPAGE if options.content_gaps else "")
        categories[0]["notes"].insert(0, _note("warning", "serp_not_configured", text))

    plan = {
        "title": options.title,
        "prompts": [
            {
                "key": p.key,
                "prompt_id": p.prompt_id,
                "brand_prompt_id": p.brand_prompt_id,
                "text": p.text,
                "location": p.location,
                "model_variants": list(p.model_variants),
                "tracked_now": p.tracked_now,
            }
            for p in prompts
        ],
        "google_pages": options.google_pages,
        "engines": list(options.engines) if options.engines else None,
        "competitor_pages": options.competitor_pages,
        "competitors": prefer,
        "content_gaps": options.content_gaps,
        "citations": options.citations,
        "serp_configured": serp_configured,
        "engines_asked": [k for k in order if k in asked],
        "engines_not_configured": not_configured,
        "planned_at": now_iso,
    }
    return Expansion(plan=plan, categories=categories, tasks=tasks)


def _categories(prompts: list[ResolvedPrompt], tasks: list[dict],
                notes: dict[str, list[dict]]) -> list[dict]:
    """Categories in CATEGORY_ORDER that have tasks, with the prompts that
    have tasks in them; ``engines`` stays (with no subcategories) when it
    has notes but no task, so the notes show."""
    out = []
    for key in CATEGORY_ORDER:
        with_tasks = {t["subcategory"] for t in tasks if t["category"] == key}
        category_notes = list(notes.get(key) or [])
        if not with_tasks and not (key == SwarmCategory.ENGINES and category_notes):
            continue
        out.append({
            "key": key,
            "label": SwarmCategory(key).label,
            "notes": category_notes,
            "subcategories": [{"key": p.key, "label": p.text} for p in prompts
                              if p.key in with_tasks],
        })
    return out


# ── Cost, size and scopes ────────────────────────────────────────────────


def estimate_cost(specs: list[dict], *, with_report: bool) -> Decimal:
    """The most these tasks can cost in AI spend (Google pages and page
    reads cost no AI allowance), plus the report when ``with_report``."""
    engine = sum(1 for s in specs if s["worker"] == WorkerType.ENGINE_ANSWER)
    analyses = engine + sum(1 for s in specs
                            if s["worker"] in (WorkerType.CONTENT_GAP, WorkerType.CITATIONS))
    total = ENGINE_TASK_USD * engine + HAIKU_TASK_USD * analyses
    return total + (REPORT_RESERVE_USD if with_report else Decimal("0"))


def max_prompts_for(*, engines_per_prompt: int, google_pages: int, competitor_pages: int,
                    content_gaps: bool, citations: bool, serp_configured: bool,
                    max_tasks: int) -> int:
    """How many prompts fit in ``max_tasks`` agents with these options."""
    per_prompt = ((google_pages + competitor_pages) if serp_configured else 0)
    per_prompt += engines_per_prompt + 2 * int(bool(content_gaps))
    per_prompt += int(bool(citations) and engines_per_prompt > 0)
    if per_prompt <= 0:
        return 0
    return max(0, math.floor(max_tasks / per_prompt))


def scope_task_ids(tasks: list[dict], scope: str, scope_key: str) -> list[str]:
    """The task ids a re-run of ``scope`` runs, in task order. A category
    with no tasks gives an empty list; an unknown scope, category or task
    id raises ValueError."""
    if scope == SwarmScope.ALL:
        return [t["id"] for t in tasks]
    if scope == SwarmScope.CATEGORY:
        if scope_key not in CATEGORY_ORDER:
            raise ValueError(f"Unknown category: {scope_key}")
        return [t["id"] for t in tasks if t["category"] == scope_key]
    if scope == SwarmScope.TASK:
        if not any(t["id"] == scope_key for t in tasks):
            raise ValueError(f"Unknown task: {scope_key}")
        return [scope_key]
    raise ValueError(f"Unknown scope: {scope}")


def scope_label(swarm_categories: list[dict], tasks: list[dict], prompts: list[dict],
                scope: str, scope_key: str) -> str:
    """``All agents``, the category label, or ``{task label} · “{prompt}”``."""
    if scope == SwarmScope.CATEGORY:
        for category in swarm_categories or []:
            if category.get("key") == scope_key:
                return category.get("label") or scope_key
        try:
            return SwarmCategory(scope_key).label
        except ValueError:
            return scope_key
    if scope == SwarmScope.TASK:
        spec = next((t for t in tasks or [] if t.get("id") == scope_key), None)
        if spec is None:
            return scope_key
        prompt = next((p for p in prompts or [] if p.get("key") == spec.get("subcategory")), None)
        if prompt is None:
            return spec.get("label") or scope_key
        return f"{spec.get('label')} · {_quoted(prompt.get('text'), SCOPE_PROMPT_CHARS)}"
    return SCOPE_LABEL_ALL


def initial_task(spec: dict) -> dict:
    """The contract 5c Task of a spec before it runs (status queued, rev 1)."""
    return {
        "id": spec["id"],
        "category": spec["category"],
        "subcategory": spec["subcategory"],
        "worker": spec["worker"],
        "label": spec["label"],
        "target": spec["target"],
        "status": "queued",
        "step": "",
        "result": "",
        "tone": None,
        "depends_on": list(spec.get("depends_on") or []),
        "started_at": None,
        "ended_at": None,
        "duration_ms": None,
        "attempt": 0,
        "error": None,
        "pane": None,
        "detail": None,
        "rev": 1,
    }
