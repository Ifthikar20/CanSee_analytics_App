"""Read tools: the project's measured data, trimmed for the model.

They wrap the services the product screens use (search_gap.overview,
prompt_stats, citations, rag), so the agent and the UI always agree on
what a number means.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from apps.agent.tools.context import ToolContext, ToolError, ToolResult
from apps.agent.tools.shaping import clip, untrusted

logger = logging.getLogger("apps")

OVERVIEW_PROMPTS = 15
ANSWER_CHARS = 1500
SNIPPET_CHARS = 400
MAX_SOURCES = 20
MAX_ARTIFACTS = 20
KNOWLEDGE_CHUNKS = 5
KNOWLEDGE_CHARS = 800


def tracked_prompt(website, prompt_id):
    """The website's BrandPrompt for ``prompt_id``, or a ToolError."""
    from apps.prompt_library.models import BrandPrompt

    bp = (
        BrandPrompt.objects.select_related("prompt", "website")
        .filter(website=website, prompt_id=prompt_id)
        .first()
    )
    if bp is None:
        raise ToolError(
            f"No tracked prompt with prompt_id {prompt_id} in this project. "
            "Use list_tracked_prompts for valid ids.",
            summary="Prompt not found",
        )
    return bp


def _prompt_row(row: dict) -> dict:
    google = row.get("google") or {}
    return {
        "prompt_id": row["prompt_id"],
        "text": row["text"],
        "location": row.get("location") or "",
        "google": {
            "status": google.get("status"),
            "position": google.get("position"),
            "page": google.get("page"),
            "checked_at": google.get("checked_at"),
        },
        "engines": {
            key: {
                "configured": e.get("configured"),
                "mentioned": e.get("mentioned") if e.get("answers") else None,
                "rank": e.get("rank"),
                "class": e.get("class"),
            }
            for key, e in (row.get("engines") or {}).items()
        },
        "gap": {"class": row["gap"]["class"], "value": row["gap"]["value"]},
        "top_action": row.get("top_action"),
    }


def get_project_overview(ctx: ToolContext, args) -> ToolResult:
    from apps.search_gap.services.overview import build_overview

    ov = build_overview(ctx.website)
    prompts = ov["prompts"]
    result = {
        "website": ov["website"],
        "serp_configured": ov["serp_configured"],
        "engines": ov["engines"],
        "kpis": ov["kpis"],
        "prompts": [_prompt_row(r) for r in prompts[:OVERVIEW_PROMPTS]],
        "prompts_total": len(prompts),
        "leaderboard": ov["leaderboard"],
    }
    return ToolResult.ok(result, summary=f"{len(prompts)} tracked prompts")


def list_tracked_prompts(ctx: ToolContext, args) -> ToolResult:
    from apps.search_gap.services.overview import build_overview

    ov = build_overview(ctx.website)
    rows = ov["prompts"]
    if args.gap_class:
        rows = [r for r in rows if r["gap"]["class"] == args.gap_class]
    result = {
        "total": len(rows),
        "serp_configured": ov["serp_configured"],
        "engines": ov["engines"],
        "prompts": [_prompt_row(r) for r in rows[: args.limit]],
    }
    noun = "prompt" if len(rows) == 1 else "prompts"
    return ToolResult.ok(result, summary=f"{len(rows)} {noun}")


def ai_overview_summary(overview: dict) -> dict:
    """Whether Google showed an AI Overview, and whether it names or cites
    the brand. Those two stay None (not measured) for an overview whose
    content was never read (``content_captured`` False), with a note saying
    so. ``content_captured`` is passed on when the capture recorded it (rank
    checks from before it existed did not)."""
    from apps.agent.tools.research import DEFERRED_OVERVIEW_NOTE

    out = {
        "shown": bool(overview.get("shown")),
        "mentions_you": overview.get("mentions_you"),
        "cites_you": overview.get("cites_you"),
    }
    if overview.get("content_captured") is not None:
        out["content_captured"] = bool(overview["content_captured"])
    if out["shown"] and overview.get("content_captured") is False:
        out["note"] = DEFERRED_OVERVIEW_NOTE
    return out


def trim_prompt_detail(detail: dict) -> dict:
    """build_prompt_detail cut to what the model and a report need."""
    google = detail.get("google") or {}
    overview = google.get("ai_overview") or {}
    engines = []
    for e in detail.get("engines") or []:
        engines.append({
            "key": e["key"],
            "label": e["label"],
            "configured": e["configured"],
            "answers": e["answers"],
            "mentioned": e["mentioned"] if e["answers"] else None,
            "rank": e["rank"],
            "sentiment": e["sentiment"],
            "visibility_pct": e["visibility_pct"],
            "score": e["score"],
            "class": e.get("class"),
            "last_answered_at": e.get("last_answered_at"),
            "brands_above": e.get("brands_above") or [],
            "cited_domains": [c["domain"] for c in (e.get("citations") or [])][:5],
            "snippet": (
                untrusted(f"{e['label']} answer", e["snippet"], SNIPPET_CHARS)
                if e.get("snippet") else None
            ),
        })
    gap = detail.get("gap") or {}
    return {
        "prompt": {
            "id": detail["prompt"]["id"],
            "text": detail["prompt"]["text"],
            "location": detail["prompt"].get("location") or "",
        },
        "google": {
            "status": google.get("status"),
            "position": google.get("position"),
            "page": google.get("page"),
            "url": google.get("url"),
            "title": google.get("title"),
            "checked_at": google.get("checked_at"),
            "depth": google.get("depth"),
            "error": google.get("error") or "",
            "top_results": [
                {
                    "position": r.get("position"),
                    "url": r.get("url") or "",
                    "domain": r.get("domain"),
                    "title": clip(r.get("title"), 120),
                    "is_yours": r.get("is_yours"),
                    "is_competitor": r.get("is_competitor"),
                }
                for r in (google.get("top_results") or [])[:10]
            ],
            "ai_overview": ai_overview_summary(overview) if overview else None,
            "search_console": google.get("gsc"),
        },
        "engines": engines,
        "gap": {
            "class": gap.get("class"),
            "value": gap.get("value"),
            "ai_score": gap.get("ai_score"),
            "search_score": gap.get("search_score"),
        },
        "verdict": detail.get("verdict"),
        "actions": [
            {
                "kind": a.get("kind"),
                "title": a.get("title"),
                "detail": a.get("detail"),
                "targets": [t.get("url") for t in (a.get("targets") or [])],
            }
            for a in (detail.get("actions") or [])[:4]
        ],
    }


def get_prompt_gap(ctx: ToolContext, args) -> ToolResult:
    from apps.search_gap.services.overview import build_prompt_detail

    bp = tracked_prompt(ctx.website, args.prompt_id)
    result = trim_prompt_detail(build_prompt_detail(ctx.website, bp))
    ctx.emitter.record_gap(result)
    gap_class = result["gap"]["class"] or "no_data"
    return ToolResult.ok(result, summary=f"{clip(bp.prompt.text, 60)}: {gap_class}")


def get_ai_answers(ctx: ToolContext, args) -> ToolResult:
    from apps.citations.services.url_analytics import model_key
    from apps.prompt_library.services.prompt_stats import MODEL_LABELS, matching_results
    from apps.search_gap.services.overview import WINDOW_DAYS

    bp = tracked_prompt(ctx.website, args.prompt_id)
    since = timezone.now() - timedelta(days=WINDOW_DAYS)
    latest: dict = {}
    for r in matching_results(ctx.website, bp.prompt, since=since):
        if not r.query_succeeded or not (r.response_text or "").strip():
            continue
        key = model_key(r.provider)
        if args.engine and key != args.engine:
            continue
        if key not in latest or r.created_at > latest[key].created_at:
            latest[key] = r
    answers = [
        {
            "engine": key,
            "label": MODEL_LABELS.get(r.provider, r.provider),
            "answered_at": r.created_at.isoformat(),
            "mentioned": r.is_mentioned,
            "rank": r.mention_rank if r.is_mentioned else None,
            "sentiment": r.sentiment if r.is_mentioned else None,
            "answer": untrusted(f"{MODEL_LABELS.get(r.provider, r.provider)} answer",
                                r.response_text, ANSWER_CHARS),
        }
        for key, r in sorted(latest.items())
    ]
    result = {"prompt": {"id": str(bp.prompt_id), "text": bp.prompt.text}, "answers": answers}
    if not answers:
        result["note"] = "No AI engine has answered this prompt yet. check_ai_engines asks them."
    return ToolResult.ok(result, summary=f"{len(answers)} answers")


def get_cited_sources(ctx: ToolContext, args) -> ToolResult:
    from django.db.models import Count, F, Q, Value
    from django.db.models.functions import Coalesce, NullIf

    from apps.citations.models import Citation
    from apps.citations.services.url_analytics import model_key
    from apps.prompt_library.services.prompt_stats import matching_results
    from apps.search_gap.services.google_rank import is_same_site, site_apex
    from apps.search_gap.services.overview import WINDOW_DAYS

    since = timezone.now() - timedelta(days=WINDOW_DAYS)
    apex = site_apex(ctx.website.url)
    qs = Citation.objects.filter(audit__website=ctx.website, created_at__gte=since)
    scope = {"prompt": None}
    if args.prompt_id:
        bp = tracked_prompt(ctx.website, args.prompt_id)
        results = matching_results(ctx.website, bp.prompt, since=since)
        qs = qs.filter(result_id__in=[r.id for r in results])
        scope["prompt"] = {"id": str(bp.prompt_id), "text": bp.prompt.text}
    # Counted in the database, so totals and ranking cover every citation
    # in the window. A citation's source is its registrable domain, else
    # its host.
    keyed = qs.annotate(source=Coalesce(NullIf(F("apex_domain"), Value("")), F("domain")))
    top = list(
        keyed.values("source")
        .annotate(
            n=Count("id"),
            competitor=Count("id", filter=Q(is_competitor=True)),
            target=Count("id", filter=Q(is_target=True)),
        )
        .order_by("-n", "source")[:MAX_SOURCES]
    )
    names = [t["source"] for t in top]
    engines: dict = defaultdict(set)
    for row in (keyed.filter(source__in=names).values("source", "result__provider")
                .order_by().distinct()):
        engines[row["source"]].add(model_key(row["result__provider"] or ""))
    result = {
        **scope,
        "window_days": WINDOW_DAYS,
        "citations": qs.count(),
        "sources": [
            {
                "domain": t["source"],
                "citations": t["n"],
                "engines": sorted(e for e in engines[t["source"]] if e),
                "is_yours": t["target"] > 0 or is_same_site(t["source"], apex),
                "is_competitor": t["competitor"] > 0,
                "example_urls": _example_urls(keyed.filter(source=t["source"])),
            }
            for t in top
        ],
    }
    return ToolResult.ok(result, summary=f"{len(top)} sources")


def _example_urls(citations, limit: int = 3) -> list[str]:
    """The newest distinct cited URLs of one source."""
    urls: list[str] = []
    for url in citations.order_by("-created_at").values_list("url", flat=True)[:limit * 10]:
        if url not in urls:
            urls.append(url)
        if len(urls) == limit:
            break
    return urls


# Why the knowledge base could not be searched, by the retriever's reason.
KNOWLEDGE_UNAVAILABLE = {
    "embeddings_not_configured": "Knowledge search isn't available: OPENAI_API_KEY is not set.",
    "embeddings_pending": (
        "Knowledge search isn't available yet: the knowledge base's sources have not been "
        "indexed, so nothing could be searched."
    ),
    "ai_allowance_exhausted": "Knowledge search isn't available: the monthly AI allowance is "
                              "used up.",
    "embeddings_failed": "Knowledge search failed: the embedding service returned an error.",
}


def _knowledge(ctx: ToolContext, query: str) -> tuple[list[dict], str | None]:
    if not getattr(settings, "OPENAI_API_KEY", ""):
        return [], KNOWLEDGE_UNAVAILABLE["embeddings_not_configured"]
    try:
        from apps.rag.services.retriever import retrieve

        hits = retrieve(user=ctx.user, website=ctx.website, query=query, top_k=KNOWLEDGE_CHUNKS)
    except Exception:
        logger.warning("agent: knowledge retrieval failed", exc_info=True)
        return [], "Knowledge search failed."
    # An empty result with a reason means the search could not run, which
    # is not the same as nothing matching.
    reason = getattr(hits, "reason", None)
    if not hits and reason:
        return [], KNOWLEDGE_UNAVAILABLE.get(
            reason, f"Knowledge search isn't available ({reason}).")
    return [
        {
            "source_url": h.source_url,
            "section": h.section_label,
            "score": round(float(h.score), 3),
            "content": untrusted("knowledge base", h.text, KNOWLEDGE_CHARS),
        }
        for h in hits
    ], None


def brand_facts(website) -> dict:
    """Website fields plus AgentBrandContext: what writers may rely on."""
    from apps.agent.models import AgentBrandContext

    ctx_row = AgentBrandContext.objects.filter(website=website).first()
    competitors = [
        {"name": c.get("name"), "domain": c.get("domain") or ""}
        for c in (website.competitors or []) if isinstance(c, dict) and c.get("name")
    ]
    return {
        "name": website.name,
        "url": website.url,
        "industry": website.industry or "",
        "description": website.description or "",
        "topics": [t for t in (website.topics or []) if isinstance(t, str)][:20],
        "competitors": competitors,
        "audience": ctx_row.audience if ctx_row else "",
        "voice": ctx_row.voice if ctx_row else "",
        "offerings": (ctx_row.offerings if ctx_row else []) or [],
        "rules": (ctx_row.rules if ctx_row else []) or [],
    }


def get_brand_context(ctx: ToolContext, args) -> ToolResult:
    facts = brand_facts(ctx.website)
    query = args.query or " ".join(
        x for x in (ctx.website.name, ctx.website.industry, "products and services") if x
    )
    knowledge, note = _knowledge(ctx, query)
    result = {**facts, "knowledge": knowledge}
    if note:
        result["knowledge_note"] = note
    summary = "Knowledge search unavailable" if note else f"{len(knowledge)} knowledge snippets"
    return ToolResult.ok(result, summary=summary)


def list_artifacts(ctx: ToolContext, args) -> ToolResult:
    from apps.agent.models import AgentArtifact

    qs = AgentArtifact.objects.filter(website=ctx.website, deleted_at__isnull=True)
    if args.kind:
        qs = qs.filter(kind=args.kind)
    rows = [
        {
            "artifact_id": str(a.id),
            "kind": a.kind,
            "title": a.title,
            "agent_key": a.agent_key,
            "latest_version": a.latest_version,
            "updated_at": a.updated_at.isoformat(),
        }
        for a in qs.order_by("-updated_at")[:MAX_ARTIFACTS]
    ]
    return ToolResult.ok({"artifacts": rows}, summary=f"{len(rows)} artifacts")
