"""Build from my site (design/p2_contract.md 11c): the ``context_build``
kind of the insights runs lifecycle, so it shares estimate, confirm,
cancel, polling and caps with every other run.

A build reads the home page and the pages its menu links to (about,
pricing, product, customers, integrations, security), the pages Offerings
entries cite and up to five pages the user names; it re-checks the other
pages active entries cite. Reading pages costs nothing. With ``synthesize``
two Claude Haiku 5.5 requests (company, audience and offerings; then
competitors, strategy and execution) draft entries from those pages, the
chosen documents, the approved brand facts and the tracked competitors.
Each draft keeps only the references whose quote is found word for word
in the source it names; a draft left without one is dropped. Drafts are
saved as suggestions, which the agents never read until a person accepts
them.

Imported from AgentConfig.ready() when apps.insights is installed; it
registers the kind with that app's runs lifecycle on import.
"""
from __future__ import annotations

import json
import logging
import re
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, time
from urllib.parse import urlsplit

from django.db.models import F
from django.utils import timezone

from apps.agent.conf import agent_conf, api_key_configured
from apps.agent.prompts import context_build as prompt
from apps.agent.services import context
from apps.agent.subagents import sourcing
from apps.insights.models import InsightRun
from apps.insights.services import runs
from core.exceptions import CanseeException

logger = logging.getLogger("apps")

KEY = "context_build"
LABEL = "Context build"
MODEL_LABEL = "Claude Haiku 5.5"
MAX_EXTRA_URLS = 5
MAX_DOCUMENTS = 10
MAX_FACTS = 40
FACT_CHARS = 300
PER_SECTION = 4
# Characters of page and document text one request carries (contract 11c).
PAGE_CHARS = 30_000
DOCUMENT_CHARS = 20_000
# Each page's text kept for checking quotes against it.
READ_CHARS = 200_000
REQUEST_TIMEOUT = 90
READ_WORKERS = 2

NOT_CONFIGURED = "The analysis model isn't configured on this server."
NOTHING_TO_CHECK = "Nothing to check: no entry cites a page yet."
NO_DOCUMENTS = "Strategy and Execution come mostly from documents you add; sites rarely state them."
# The menu pages a build reads, in this order (contract 11c).
LINK_PATTERNS = tuple(
    re.compile(rf"(?:^|/)(?:{words})(?:[-_./]|$)", re.IGNORECASE)
    for words in (
        "about|company|who-we-are|our-story",
        "pricing|plans",
        "product|products|features|platform|solutions",
        "customers|case-studies|stories",
        "integrations|apps|partners",
        "security|trust",
    )
)


@dataclass(frozen=True)
class Plan:
    synthesize: bool
    extra_urls: tuple[str, ...]
    # (id, title, chars) of the documents the requests carry.
    documents: tuple[tuple[str, str, int], ...]
    # Cited pages re-checked on top of the pages the build reads.
    recheck: tuple[str, ...]
    max_pages: int


def _invalid(field: str, message: str) -> CanseeException:
    return CanseeException(message, code="invalid", status_code=400,
                           details={"field": field, "message": message})


def _domain(website) -> str:
    return sourcing.apex_of(website.url or "")


def runs_today(website) -> int:
    midnight = datetime.combine(timezone.now().astimezone(UTC).date(), time.min, tzinfo=UTC)
    return InsightRun.objects.filter(website=website, kind=KEY, created_at__gte=midnight).count()


def _documents(website, ids) -> tuple[tuple[str, str, int], ...]:
    from apps.agent.models import AgentContextDocument

    qs = AgentContextDocument.objects.filter(website=website).order_by("-updated_at", "-id")
    if ids is None:
        rows = qs.values_list("id", "title", "chars")[:MAX_DOCUMENTS]
    else:
        rows = qs.filter(pk__in=ids).values_list("id", "title", "chars")
    return tuple((str(i), title, chars) for i, title, chars in rows)


def _known_pages(website, extra_urls) -> list[str]:
    """The pages a build reads before it sees the home page's menu."""
    return [website.url, *context.offering_urls(website), *extra_urls]


def _recheck(website, *, synthesize: bool, extra_urls) -> tuple[str, ...]:
    limit = agent_conf().context_check_max_pages
    if not synthesize:
        return tuple(context.cited_pages(website, limit=limit))
    known = {context.url_key(u) for u in _known_pages(website, extra_urls)}
    cited = context.cited_pages(website, limit=limit + len(known))
    return tuple([u for u in cited if context.url_key(u) not in known][:limit])


def _parse(website, params) -> Plan:
    """The plan of ``params``; raises 400 invalid for a malformed one."""
    params = params or {}
    synthesize = params.get("synthesize", True)
    if not isinstance(synthesize, bool):
        raise _invalid("synthesize", "Choose whether to suggest entries.")
    raw = params.get("extra_urls") or []
    if not isinstance(raw, list) or any(not isinstance(u, str) for u in raw):
        raise _invalid("extra_urls", "Send the extra pages as a list of addresses.")
    extra: list[str] = []
    seen: set[str] = set()
    for url in (u.strip() for u in raw):
        key = context.url_key(url) if url else ""
        if not url or key in seen:
            continue
        if len(extra) >= MAX_EXTRA_URLS:
            # Refused at the first page over the limit, without reading the rest.
            raise CanseeException(f"This run is over the limit of {MAX_EXTRA_URLS} pages.",
                                  code="too_many", status_code=400)
        seen.add(key)
        extra.append(url)
    for url in extra:
        if len(url) > 2000 or not sourcing.same_site(url, website):
            raise _invalid("extra_urls", f"Use pages of {_domain(website)} only.")
    ids = params.get("document_ids")
    if ids is not None:
        if not isinstance(ids, list) or any(not isinstance(i, str) for i in ids):
            raise _invalid("document_ids", "Send the documents as a list of ids.")
        ids = list(dict.fromkeys(i.strip() for i in ids if i.strip()))
        if len(ids) > MAX_DOCUMENTS:
            raise CanseeException(f"This run is over the limit of {MAX_DOCUMENTS} documents.",
                                  code="too_many", status_code=400)
        if any(not context.is_uuid(i) for i in ids):
            raise _invalid("document_ids", "Choose documents of this project.")
    documents = _documents(website, ids)
    if ids is not None and len(documents) != len(ids):
        raise _invalid("document_ids", "Choose documents of this project.")
    return Plan(
        synthesize=synthesize,
        extra_urls=tuple(extra),
        documents=documents,
        recheck=_recheck(website, synthesize=synthesize, extra_urls=extra),
        max_pages=agent_conf().context_build_max_pages if synthesize else 0,
    )


# ── The kind's interface (apps.insights.services.runs.Kind) ─────────────


def validate(website, params) -> Plan:
    plan = _parse(website, params)
    if plan.synthesize and not api_key_configured():
        raise CanseeException(NOT_CONFIGURED, code="not_configured", status_code=400)
    if not plan.synthesize and not plan.recheck:
        raise CanseeException(NOTHING_TO_CHECK, code="nothing_to_run", status_code=400)
    return plan


def estimate(website, user, plan: Plan) -> dict:
    conf = agent_conf()
    synthesize = plan.synthesize
    facts = context.fact_counts(website)
    warnings = []
    if synthesize and not plan.documents:
        warnings.append({"code": "no_documents", "message": NO_DOCUMENTS})
    if facts["pending"]:
        warnings.append({"code": "facts_pending",
                         "message": f"{facts['pending']} brand facts are waiting for review and "
                                    "are not used as evidence."})
    blockers = []
    today, limit = runs_today(website), conf.context_build_daily_runs
    if today >= limit:
        blockers.append({"code": "daily_runs", "status": 409,
                         "message": f"This project ran {today} context builds today; the limit "
                                    f"is {limit}."})
    if synthesize:
        summary = f"Up to {plan.max_pages} pages of {_domain(website)}"
        summary += f" and {len(plan.documents)} documents" if plan.documents else ""
        if plan.recheck:
            summary += f"; re-checks {len(plan.recheck)} cited pages"
    else:
        summary = f"Re-check {len(plan.recheck)} pages your entries cite"
    calls = 2 if synthesize else 0
    return {
        "summary": summary,
        "units": {"pages": plan.max_pages, "rechecks": len(plan.recheck),
                  "documents": len(plan.documents), "requests": calls},
        "ai": {"calls_max": calls, "usd_max": conf.context_build_usd_per_request * calls,
               "model_label": MODEL_LABEL if synthesize else None},
        "google": {"pages_max": 0},
        "fetches": {"pages_max": plan.max_pages + len(plan.recheck)},
        "warnings": warnings,
        "blockers": blockers,
    }


def choices(website, user) -> dict:
    from apps.agent.models import AgentContextDocument

    documents = (AgentContextDocument.objects.filter(website=website)
                 .order_by("-updated_at", "-id").values_list("id", "title", "chars"))
    return {
        "domain": _domain(website),
        "documents": [{"id": str(i), "title": title, "chars": chars}
                      for i, title, chars in documents],
        "facts": context.fact_counts(website),
        "competitors": len(context.competitor_list(website)),
        "recheck": len(_recheck(website, synthesize=True, extra_urls=[])),
        "runs_today": runs_today(website),
        "runs_limit": agent_conf().context_build_daily_runs,
    }


def plan_items(run, plan: Plan) -> int:
    """No item rows: one unit per page slot, re-check and request."""
    return plan.max_pages + len(plan.recheck) + (2 if plan.synthesize else 0)


def build_status(website) -> dict:
    """Contract 11b ``build``."""
    from django.conf import settings

    if not getattr(settings, "INSIGHTS_ENABLED", False):
        return context.build_block(available=False, reason="insights_disabled")
    runs.sweep_stale(website)
    state = runs.run_state(website, KEY)
    reason = None if api_key_configured() else "ai_not_configured"
    return context.build_block(available=True, reason=reason, **state)


# ── Execution ─────────────────────────────────────────────────────────────


def choose_pages(website, home, plan: Plan) -> list[str]:
    """After the home page: the first menu link matching each pattern, the
    pages Offerings entries cite, then the user's pages; at most
    ``plan.max_pages`` with the home page."""
    chosen = [home.url]
    keys = {context.url_key(home.url), context.url_key(home.final_url)}

    def add(url) -> None:
        key = context.url_key(url)
        if key not in keys and len(chosen) < plan.max_pages:
            keys.add(key)
            chosen.append(url)

    links = [link.get("url") or "" for link in home.links if isinstance(link, dict)]
    internal = [u for u in links if sourcing.same_site(u, website)]
    for pattern in LINK_PATTERNS:
        for url in internal:
            if context.url_key(url) in keys:
                continue
            try:
                path = urlsplit(url).path or "/"
            except ValueError:
                continue
            if pattern.search(path):
                add(url)
                break
    for url in [*context.offering_urls(website), *plan.extra_urls]:
        add(url)
    return chosen[1:]


def _error_code(page) -> str:
    code = (page.error or {}).get("code")
    return code if code in ("blocked_url", "timeout") else "fetch_failed"


def _read(ctx, url: str, page_id: str, *, with_links: bool = False):
    """One page as one unit of the run; None when the run was stopped."""
    if ctx.cancelled():
        return None
    ctx.start_item()
    page = sourcing.read_page(url, id=page_id, per_page_chars=READ_CHARS, with_links=with_links)
    ctx.end_item("done" if page.ok else "failed", error=None if page.ok else _error_code(page))
    return page


@dataclass(frozen=True)
class _Source:
    kind: str
    text: str
    values: dict


def _sources(website, pages, plan: Plan) -> tuple[dict[str, _Source], dict]:
    """The sources one request carries, by id, and their blocks' inputs."""
    from apps.agent.models import AgentContextDocument
    from apps.brand_vault.models import BrandFact

    sources: dict[str, _Source] = {}
    ok_pages = [p for p in pages if p.ok]
    for page in ok_pages:
        url = page.final_url if sourcing.same_site(page.final_url, website) else page.url
        sources[page.id] = _Source("page", page.text, {"kind": "page", "url": url[:2000]})
    docs = list(AgentContextDocument.objects.filter(
        website=website, pk__in=[d[0] for d in plan.documents]).order_by("-updated_at", "-id"))
    for n, document in enumerate(docs, start=1):
        sources[f"d{n}"] = _Source("document", document.text,
                                   {"kind": "document", "document": document,
                                    "title": document.title[:300]})
    facts = list(BrandFact.objects.filter(website=website, version_to__isnull=True,
                                          status__in=context.USABLE_FACT_STATUSES)
                 .order_by("-updated_at")[:MAX_FACTS])
    fact_rows = []
    for n, fact in enumerate(facts, start=1):
        text = f"{fact.subject} {fact.predicate} {fact.object}"[:FACT_CHARS]
        sources[f"f{n}"] = _Source("fact", text, {"kind": "fact", "fact": fact,
                                                  "title": context.fact_label(fact)[:300]})
        fact_rows.append({"id": f"f{n}", "text": text})
    competitor_rows = []
    for n, competitor in enumerate(context.competitor_list(website), start=1):
        domain = context.fold_domain(competitor["domain"])
        competitor_rows.append({"id": f"c{n}", "name": competitor["name"],
                                "domain": competitor["domain"]})
        if domain:
            sources[f"c{n}"] = _Source("competitor", "", {
                "kind": "competitor", "competitor_domain": domain,
                "competitor_name": competitor["name"][:200]})
    return sources, {"pages": ok_pages, "documents": docs, "facts": fact_rows,
                     "competitors": competitor_rows}


def build_content(website, sections, inputs: dict, existing: dict) -> list[dict]:
    """The user turn of one request: data blocks, third-party text escaped."""
    labels = {key: (label, help_text) for key, label, help_text in context.SECTIONS}
    brand = {"name": website.name, "domain": _domain(website),
             "industry": website.industry or "", "description": website.description or ""}
    blocks = [
        sourcing.tag("brand", json.dumps(brand, ensure_ascii=False)),
        sourcing.tag("sections", json.dumps(
            [{"key": key, "label": labels[key][0], "help": labels[key][1]} for key in sections],
            ensure_ascii=False)),
        sourcing.tag("existing", json.dumps({key: existing.get(key, []) for key in sections},
                                            ensure_ascii=False)),
    ]
    pages = inputs["pages"]
    for page, text in zip(pages, sourcing.water_fill([p.text for p in pages], PAGE_CHARS),
                          strict=True):
        blocks.append(sourcing.tag("page", text, id=page.id, url=page.final_url))
    documents = inputs["documents"]
    texts = sourcing.water_fill([d.text for d in documents], DOCUMENT_CHARS)
    for n, (document, text) in enumerate(zip(documents, texts, strict=True), start=1):
        blocks.append(sourcing.tag("document", text, id=f"d{n}", title=document.title))
    blocks.append(sourcing.tag("facts", json.dumps(inputs["facts"], ensure_ascii=False)))
    blocks.append(sourcing.tag("competitors", json.dumps(inputs["competitors"],
                                                         ensure_ascii=False)))
    return blocks


def validate_drafts(draft, sections, sources: dict[str, _Source],
                    existing: dict[str, list[str]]) -> list[dict]:
    """The model's entries that hold up: a known section, a text, and at
    least one reference whose quote (pages and documents) is found word
    for word in that source; no repeat of an existing or earlier title;
    at most PER_SECTION per section."""
    now = timezone.now()
    seen = {key: {sourcing.fold(t) for t in existing.get(key, [])} for key in sections}
    kept: Counter = Counter()
    out = []
    for entry in draft.entries:
        section = entry.section.strip()
        if section not in sections or kept[section] >= PER_SECTION:
            continue
        title, body = entry.title.strip(), entry.body.strip()
        if not title and not body:
            continue
        folded = sourcing.fold(title or body)
        if folded in seen[section]:
            continue
        references, targets = [], set()
        for ref in entry.references:
            source = sources.get(ref.source_id.strip())
            if source is None:
                continue
            values = dict(source.values)
            if source.kind in ("page", "document"):
                quote = sourcing.verbatim(source.text, ref.quote)
                if quote is None:
                    continue
                values["quote"] = quote[:context.QUOTE_MAX_LENGTH]
            target = (source.kind, ref.source_id.strip())
            if target in targets:
                continue
            targets.add(target)
            values["state"] = "ok"
            if source.kind == "page":
                values["checked_at"] = now
            references.append(values)
        if not references:
            continue
        seen[section].add(folded)
        kept[section] += 1
        out.append({"section": section, "title": title, "body": body,
                    "references": references[:context.MAX_REFERENCES_PER_ENTRY]})
    return out


def _page_row(page) -> dict:
    return {"url": page.url, "final_url": page.final_url, "title": page.title,
            "state": "ok" if page.ok else "failed", "words": page.words,
            "reason": page.reason or None}


def execute(ctx) -> None:
    from apps.agent.llm.worker import ask_haiku_standalone

    run, website = ctx.run, ctx.website
    conf = agent_conf()
    try:
        plan = _parse(website, run.params)
    except CanseeException:
        logger.warning("context build %s: its settings no longer apply", run.id)
        return
    pages = []
    unused = 0
    if plan.synthesize:
        home = _read(ctx, website.url, "p1", with_links=True)
        if home is None:
            return
        pages.append(home)
        urls = choose_pages(website, home, plan)
        unused += plan.max_pages - 1 - len(urls)
    else:
        urls = []
    read_keys = {context.url_key(u) for u in [website.url, *urls]} if plan.synthesize else set()
    recheck = [u for u in plan.recheck if context.url_key(u) not in read_keys]
    unused += len(plan.recheck) - len(recheck)
    if unused > 0:
        InsightRun.objects.filter(id=run.id).update(units_total=F("units_total") - unused)

    ids = [f"p{n}" for n in range(2, len(urls) + 2)]
    ids += [f"r{n}" for n in range(1, len(recheck) + 1)]
    with ctx.pool(READ_WORKERS) as pool:
        read = pool.map(lambda url, page_id: _read(ctx, url, page_id), [*urls, *recheck], ids)
    built = [p for p in read[:len(urls)] if p is not None]
    pages += built
    checked = [p for p in read[len(urls):] if p is not None]
    by_key = {}
    for page in [*pages, *checked]:
        by_key.setdefault(context.url_key(page.url), page)
        by_key.setdefault(context.url_key(page.final_url), page)
    rechecked = context.recheck_pages(website, by_key)

    suggested: dict[str, int] = {}
    if plan.synthesize and not ctx.cancelled():
        sources, inputs = _sources(website, pages, plan)
        if not (inputs["pages"] or inputs["documents"] or inputs["facts"]):
            # Nothing to draft from: the requests are skipped and nothing is billed.
            for _ in prompt.GROUPS:
                ctx.end_item("skipped", started=False)
        else:
            existing = context.existing_titles(website)
            drafted, sections_done = [], []
            for group, sections in prompt.GROUPS:
                if ctx.cancelled():
                    break
                ctx.start_item()
                result = ask_haiku_standalone(
                    user=ctx.user, website=website, system=prompt.SYSTEM,
                    content=build_content(website, sections, inputs, existing),
                    schema=prompt.schema(sections), output_model=prompt.ContextDraft,
                    timeout=REQUEST_TIMEOUT, audit_id=ctx.audit_id, role="context_build",
                    step=f"{run.id}:{group}", module="agent",
                    max_tokens=conf.context_build_max_tokens, should_stop=ctx.cancelled,
                )
                if not result.ok:
                    status = "cancelled" if result.code == "cancelled" else "failed"
                    ctx.end_item(status, error=result.code)
                    continue
                drafted += validate_drafts(result.data, sections, sources, existing)
                sections_done += list(sections)
                ctx.end_item("done")
            if sections_done:
                suggested = context.replace_suggestions(
                    website, ctx.user, run_id=run.id, sections=sections_done, drafts=drafted)
    InsightRun.objects.filter(id=run.id).update(result={
        "pages": [_page_row(p) for p in pages],
        "suggested": suggested,
        "rechecked": rechecked,
    })


KIND = runs.Kind(key=KEY, label=LABEL, queue="ai", validate=validate, estimate=estimate,
                 choices=choices, plan_items=plan_items, execute=execute)
runs.register(KIND)
