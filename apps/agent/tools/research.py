"""Research tools: the user's SEO (visible) and GEO (background) flow.

google_search and inspect_page open tabs in the browser pane through
``browser.*`` events and keep their full captures in the assistant
message's research store, so the pane can rebuild its tabs after a
reload. check_ai_engines asks the AI engines through the existing scan
pipeline and only ever emits ``engine_check`` parts: it never touches
the pane.
"""
from __future__ import annotations

import logging
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from decimal import Decimal

from django.conf import settings
from django.db import close_old_connections, connections
from django.utils import timezone

from apps.agent.conf import agent_conf
from apps.agent.constants import EventType, PartType
from apps.agent.runtime.events import now_iso
from apps.agent.tools.context import ToolContext, ToolError, ToolResult
from apps.agent.tools.shaping import clip, display_url, host_of, untrusted

logger = logging.getLogger("apps")

PAGE_SIZE = 10
PAGE_EXCERPT_CHARS = 1500
ENGINE_WORKERS = 3
SERP_ERRORS = {
    "network": "Google search could not be reached.",
    "decode": "Google search returned an unreadable response.",
    "empty_query": "The search query was empty.",
}


def _capture_id() -> str:
    return uuid.uuid4().hex[:12]


# ── google_search ───────────────────────────────────────────────────────


def _serp_quota():
    from core.quota import DailyQuota

    return DailyQuota(
        namespace="agent_serp",
        setting_name="SEARCH_GAP_DAILY_CHECKS_PER_WEBSITE",
        default_limit=100,
    )


def _competitor_apexes(website) -> dict[str, str]:
    from apps.citations.services.url_normalizer import extract_apex_domain

    out = {}
    for c in website.competitors or []:
        if isinstance(c, dict) and c.get("domain"):
            apex = extract_apex_domain(str(c["domain"]).strip().lower())
            if apex:
                out[apex] = str(c.get("name") or apex)
    return out


def _serp_error(code: str) -> dict:
    if code.startswith("http_"):
        return {"code": "serp_failed", "message": f"Google search failed (HTTP {code[5:]})."}
    return {"code": "serp_failed", "message": SERP_ERRORS.get(code, "Google search failed.")}


def build_serp_capture(payload: dict, *, website, query: str, page: int, location: str,
                       capture_id: str) -> dict:
    """A SerpCapture (contract section 2e) from one serp_google.search payload."""
    from apps.citations.services.url_normalizer import extract_apex_domain
    from apps.search_gap.services.google_rank import is_same_site, site_apex, summarize_ai_overview

    apex = site_apex(website.url)
    competitors = _competitor_apexes(website)
    start = (page - 1) * PAGE_SIZE
    results = []
    for i, row in enumerate(payload.get("organic") or []):
        domain = row.get("domain") or ""
        position = row.get("position") or start + i + 1
        comp_apex = extract_apex_domain(domain)
        is_yours = is_same_site(domain, apex)
        is_competitor = (not is_yours) and comp_apex in competitors
        item = {
            "position": position,
            "title": row.get("title") or "",
            "url": row.get("url") or "",
            "display_url": display_url(row.get("url") or ""),
            "domain": domain,
            "snippet": row.get("snippet") or "",
            "is_yours": is_yours,
            "is_competitor": is_competitor,
        }
        if is_competitor:
            item["competitor_name"] = competitors[comp_apex]
        results.append(item)
    overview = None
    if page == 1 or payload.get("ai_overview"):
        summary = summarize_ai_overview(payload.get("ai_overview") or {},
                                        brand=website.name, apex=apex)
        overview = {
            "shown": summary["shown"],
            "mentions_you": summary["mentions_you"],
            "cites_you": summary["cites_you"],
            "references": [
                {"title": r.get("title") or "", "url": r.get("url") or "",
                 "domain": r.get("domain") or ""}
                for r in summary["references"]
            ],
        }
    return {
        "capture_id": capture_id,
        "query": query,
        "page": page,
        "start": start,
        "location": location,
        "captured_at": now_iso(),
        "brand": {"name": website.name, "domain": apex},
        "brand_positions": [r["position"] for r in results if r["is_yours"]],
        "results": results,
        "ai_overview": overview,
        "people_also_ask": [
            q["question"] for q in (payload.get("questions") or []) if q.get("question")
        ],
        "error": None,
    }


def _tracked_prompt_for(website, query: str):
    from apps.prompt_library.models import BrandPrompt
    from apps.prompt_library.services.prompt_stats import normalise

    wanted = normalise(query)
    for bp in BrandPrompt.objects.select_related("prompt").filter(website=website):
        if normalise(bp.prompt.text) == wanted:
            return bp
    return None


def _record_rank_check(website, brand_prompt, *, query: str, location: str, pages: list[dict]):
    """A GoogleRankCheck from the captured pages, so Overview and the prompt
    page show this search. ``depth`` is what was actually searched."""
    from apps.search_gap.models import GoogleRankCheck

    first = pages[0]
    found = next((r for p in pages for r in p["results"] if r["is_yours"]), None)
    overview = first.get("ai_overview") or {}
    return GoogleRankCheck.objects.create(
        website=website,
        brand_prompt=brand_prompt,
        query=query[:500],
        location=location or brand_prompt.location or "",
        depth=len(pages) * PAGE_SIZE,
        position=found["position"] if found else None,
        url=(found["url"] if found else "")[:2000],
        title=(found["title"] if found else "")[:500],
        top_results=[
            {"position": r["position"], "url": r["url"], "domain": r["domain"], "title": r["title"]}
            for r in first["results"]
        ],
        ai_overview={
            "shown": bool(overview.get("shown")),
            "mentions_you": bool(overview.get("mentions_you")),
            "cites_you": bool(overview.get("cites_you")),
            "references": overview.get("references") or [],
        },
        pages_fetched=len(pages),
        cost_usd=Decimal(str(settings.SERPAPI_COST_PER_SEARCH_USD)) * len(pages),
        error="",
        checked_at=timezone.now(),
    )


def google_search(ctx: ToolContext, args) -> ToolResult:
    from apps.citations.services import serp_google
    from apps.search_gap.services.google_rank import locale

    em = ctx.emitter
    query = args.query
    location = (args.location or "").lower()
    if not serp_google.is_configured():
        em.add_part(
            PartType.SERP, request=ctx.request, query=query, page=1, location=location,
            status="error", tab_id=None, brand_positions=[], result_count=0,
            ai_overview_shown=False,
            error={"code": "serp_not_configured",
                   "message": "Google search isn't available: SERPAPI_KEY is not set."},
        )
        raise ToolError("Google search isn't available: SERPAPI_KEY is not set.",
                        summary="Google search not configured")

    gl, hl = locale(location)
    pages_wanted = min(args.pages, agent_conf().serp_max_pages)
    quota = _serp_quota()
    cost_per_page = Decimal(str(settings.SERPAPI_COST_PER_SEARCH_USD))
    captures: list[dict] = []
    stopped_reason = None
    for page in range(1, pages_wanted + 1):
        if ctx.cancelled():
            stopped_reason = "cancelled"
            break
        capture_id = _capture_id()
        tab_id = f"serp:{capture_id}"
        part_id = em.add_part(
            PartType.SERP, request=ctx.request, query=query, page=page, location=location,
            status="running", tab_id=tab_id, brand_positions=[], result_count=0,
            ai_overview_shown=False, error=None,
        )
        if not quota.consume(str(ctx.website.id)):
            error = {"code": "serp_daily_limit",
                     "message": "This project used its daily Google search allowance."}
            em.update_part(part_id, status="error", tab_id=None, error=error)
            stopped_reason = "daily_limit"
            break
        em.emit(EventType.BROWSER_OPEN, tab={
            "id": tab_id,
            "kind": "serp",
            "title": f"Google · {clip(query, 60)} · p{page}",
            "status": "loading",
            "data": {"capture_id": capture_id, "query": query, "page": page,
                     "start": (page - 1) * PAGE_SIZE, "location": location},
        }, activate=page == 1)
        try:
            payload = serp_google.search(query, max_results=PAGE_SIZE,
                                         start=(page - 1) * PAGE_SIZE, gl=gl, hl=hl or "en")
        except Exception:
            logger.warning("agent: serp_google.search raised", exc_info=True)
            payload = {"error": "network"}
        capture = build_serp_capture(payload, website=ctx.website, query=query, page=page,
                                     location=location, capture_id=capture_id)
        capture["cost_usd"] = float(cost_per_page)
        if payload.get("error"):
            capture["error"] = _serp_error(payload["error"])
        em.record_research("serp", capture_id, capture)
        em.emit(EventType.BROWSER_UPDATE, tab_id=tab_id,
                status="error" if capture["error"] else "ready", patch=capture)
        if capture["error"]:
            em.update_part(part_id, status="error", error=capture["error"])
            stopped_reason = "error"
            break
        captures.append(capture)
        em.update_part(
            part_id, status="done", brand_positions=capture["brand_positions"],
            result_count=len(capture["results"]),
            ai_overview_shown=bool(capture["ai_overview"] and capture["ai_overview"]["shown"]),
        )
        if not capture["results"]:
            break
        if capture["brand_positions"] and page >= args.pages:
            break
    em.checkpoint(force=True)

    if not captures:
        if stopped_reason == "daily_limit":
            raise ToolError("This project used its daily Google search allowance; try tomorrow.",
                            summary="Daily Google search allowance used")
        if stopped_reason == "cancelled":
            raise ToolError("Stopped before the search ran.", summary="Stopped")
        raise ToolError("Google search failed; no results were captured.",
                        summary="Google search failed")

    brand_prompt = _tracked_prompt_for(ctx.website, query)
    rank_check_id = None
    if brand_prompt is not None:
        check = _record_rank_check(ctx.website, brand_prompt, query=query, location=location,
                                   pages=captures)
        rank_check_id = str(check.id)

    positions = [p for c in captures for p in c["brand_positions"]]
    first = captures[0]
    overview = first["ai_overview"] or {}
    result = {
        "query": query,
        "location": location or "global",
        "pages": [
            {
                "page": c["page"],
                "brand_positions": c["brand_positions"],
                "top": [
                    {"position": r["position"], "domain": r["domain"],
                     "title": clip(r["title"], 120), "is_yours": r["is_yours"],
                     "is_competitor": r["is_competitor"]}
                    for r in c["results"][:PAGE_SIZE]
                ],
            }
            for c in captures
        ],
        "pages_searched": len(captures),
        "brand_best_position": min(positions) if positions else None,
        "ai_overview": {
            "shown": bool(overview.get("shown")),
            "mentions_you": bool(overview.get("mentions_you")),
            "cites_you": bool(overview.get("cites_you")),
        },
        "people_also_ask": first["people_also_ask"],
        "tracked_prompt_id": str(brand_prompt.prompt_id) if brand_prompt else None,
        "rank_check_recorded": rank_check_id is not None,
        "captured_at": first["captured_at"],
    }
    if stopped_reason == "daily_limit":
        result["note"] = "The daily Google search allowance ran out; later pages were not searched."
    elif stopped_reason == "error":
        result["note"] = "A later page failed; the pages listed were captured."
    best = result["brand_best_position"]
    summary = f"you are #{best}" if best else f"not in the top {len(captures) * PAGE_SIZE}"
    return ToolResult.ok(result, summary=summary)


# ── inspect_page ────────────────────────────────────────────────────────


def _browse_quota():
    from core.quota import DailyQuota

    return DailyQuota(namespace="agent_browse", setting_name="AGENT_BROWSE_DAILY_LIMIT",
                      default_limit=300)


def _reader_text(reader: dict | None) -> str:
    if not reader:
        return ""
    chunks = []
    for block in reader.get("blocks") or []:
        kind = block.get("type")
        if kind in ("heading", "paragraph", "quote", "code"):
            chunks.append(block.get("text") or "")
        elif kind == "list":
            chunks.extend(block.get("items") or [])
        elif kind == "table":
            chunks.extend(" | ".join(row) for row in (block.get("rows") or []))
    return "\n".join(c for c in chunks if c)


def inspect_page(ctx: ToolContext, args) -> ToolResult:
    url = args.url
    if not url.lower().startswith(("http://", "https://")):
        raise ToolError("inspect_page needs an http or https URL.", summary="Invalid URL")
    em = ctx.emitter
    capture_id = _capture_id()
    tab_id = f"page:{capture_id}"
    host = host_of(url) or url
    part_id = em.add_part(PartType.PAGE_INSPECT, request=ctx.request, url=url, status="running",
                          tab_id=tab_id, title=host, summary="", error=None)
    if not _browse_quota().consume(str(ctx.user.id)):
        error = {"code": "browse_daily_limit",
                 "message": "You used today's page inspection allowance."}
        em.update_part(part_id, status="error", tab_id=None, error=error)
        raise ToolError(error["message"], summary="Daily page allowance used")
    em.emit(EventType.BROWSER_OPEN, tab={
        "id": tab_id, "kind": "page", "title": host, "status": "loading",
        "data": {"capture_id": capture_id, "url": url},
    }, activate=True)

    from apps.agent.services.browse import fetch_page

    try:
        result = fetch_page(url) or {}
    except Exception:
        logger.warning("agent: fetch_page raised for %s", url, exc_info=True)
        result = {"error": {"code": "fetch_failed", "message": "The page could not be fetched."}}
    capture = {
        "capture_id": capture_id,
        "url": result.get("url") or url,
        "final_url": result.get("final_url") or url,
        "captured_at": result.get("captured_at") or now_iso(),
        "frameable": bool(result.get("frameable")),
        "status_code": result.get("status_code"),
        "seo": result.get("seo"),
        "reader": result.get("reader"),
        "error": result.get("error"),
    }
    title = result.get("title") or (capture["seo"] or {}).get("title") or host
    em.record_research("pages", capture_id, {k: v for k, v in capture.items() if k != "reader"})
    em.emit(EventType.BROWSER_UPDATE, tab_id=tab_id,
            status="error" if capture["error"] else "ready", title=clip(title, 120), patch=capture)
    if capture["error"]:
        error = capture["error"]
        em.update_part(part_id, status="error", title=clip(title, 120), error=error)
        em.checkpoint(force=True)
        raise ToolError(f"Could not inspect {url}: {error.get('message') or error.get('code')}",
                        summary="Page could not be read")
    seo = capture["seo"] or {}
    summary = (
        f"{len(seo.get('h1') or [])} H1, {seo.get('word_count') or 0} words, "
        f"{len(seo.get('structured_data') or [])} structured data types"
    )
    em.update_part(part_id, status="done", title=clip(title, 120), summary=summary)
    em.checkpoint(force=True)
    return ToolResult.ok({
        "url": capture["url"],
        "final_url": capture["final_url"],
        "status_code": capture["status_code"],
        "frameable": capture["frameable"],
        "seo": seo,
        "excerpt": untrusted(capture["final_url"], _reader_text(capture["reader"]),
                             PAGE_EXCERPT_CHARS),
    }, summary=summary)


# ── track_prompts ───────────────────────────────────────────────────────


def _industry_for(website):
    from apps.prompt_library.models import Industry

    slug_hint = (website.industry or "").lower().strip().replace(" ", "-")
    return (
        Industry.objects.filter(slug=slug_hint, is_active=True).first()
        or Industry.objects.filter(is_active=True).order_by("name").first()
    )


def track_prompts(ctx: ToolContext, args) -> ToolResult:
    """Add tracked prompts the way the prompt library's create endpoint does
    (WebsitePromptCreateView): one Prompt per text, matched by text hash,
    and a BrandPrompt for this website. It does not start a scan: the
    agent asks the engines next with check_ai_engines."""
    from apps.prompt_library.models import BrandPrompt, Prompt, PromptSource
    from apps.prompt_library.services._hash import text_hash
    from apps.prompt_library.services.template_parser import extract_variables

    industry = _industry_for(ctx.website)
    if industry is None:
        raise ToolError("Prompts cannot be tracked: no prompt industry is set up.",
                        summary="No industry available")
    created = []
    for item in args.prompts:
        text = " ".join(item.text.split())
        digest = text_hash(text)
        prompt = Prompt.objects.filter(text_hash=digest).order_by("created_at").first()
        if prompt is None:
            prompt = Prompt.objects.create(
                industry=industry,
                text=text,
                template_text=text,
                template_variables=extract_variables(text),
                intent_bucket="category",
                style="question",
                source=PromptSource.MANUAL,
                text_hash=digest,
            )
        brand_prompt, was_created = BrandPrompt.objects.get_or_create(
            website=ctx.website, prompt=prompt,
        )
        location = (item.location or "").lower()
        changed = []
        if location and brand_prompt.location != location:
            brand_prompt.location = location
            changed.append("location")
        if brand_prompt.is_archived:
            brand_prompt.is_archived = False
            changed.append("is_archived")
        if changed:
            brand_prompt.save(update_fields=[*changed, "updated_at"])
        created.append({
            "prompt_id": str(prompt.id),
            "text": prompt.text,
            "location": brand_prompt.location or "",
            "already_tracked": not was_created,
        })
    new = sum(1 for c in created if not c["already_tracked"])
    return ToolResult.ok({"prompts": created},
                         summary=f"{new} added, {len(created) - new} already tracked")


# ── check_ai_engines ────────────────────────────────────────────────────


def engine_catalog() -> list[dict]:
    """The AI engines in display order, with whether each has its API key."""
    from apps.citations.services.url_analytics import model_key
    from apps.llm_ranking.providers import PROVIDERS
    from apps.prompt_library.services.prompt_stats import MODEL_LABELS, is_configured
    from apps.search_gap.services.overview import ENGINE_ORDER

    out = []
    for provider in ENGINE_ORDER:
        cls = PROVIDERS.get(provider)
        if cls is None:
            continue
        out.append({
            "provider": provider,
            "key": model_key(provider),
            "label": MODEL_LABELS.get(provider, provider),
            "configured": is_configured(cls),
            "key_setting": cls.api_key_setting,
        })
    return out


def _engine_errors(outcome) -> dict[str, str]:
    errors = {}
    for line in getattr(outcome, "errors", None) or []:
        provider, _, message = str(line).partition(":")
        errors[provider.split(":")[0].strip()] = message.strip() or "The engine did not answer."
    return errors


def _crawl(website, prompt, user, *, close_db: bool):
    from apps.prompt_library.services import prompt_crawler

    try:
        return prompt_crawler.crawl_prompt(website, prompt, only_missing=False, acting_user=user)
    finally:
        if close_db:
            connections.close_all()


def check_ai_engines(ctx: ToolContext, args) -> ToolResult:
    from apps.prompt_library.models import BrandPrompt
    from apps.search_gap.services.overview import build_prompt_detail

    em = ctx.emitter
    ids = list(dict.fromkeys(str(pid) for pid in args.prompt_ids))
    found = {
        str(bp.prompt_id): bp
        for bp in BrandPrompt.objects.select_related("prompt", "website").filter(
            website=ctx.website, prompt_id__in=ids,
        )
    }
    missing = [pid for pid in ids if pid not in found]
    if not found:
        raise ToolError("None of these prompt ids is tracked in this project.",
                        summary="No tracked prompts")
    engines = engine_catalog()
    configured = [e for e in engines if e["configured"]]
    parts: dict[str, dict[str, str]] = {}
    for pid, bp in found.items():
        parts[pid] = {}
        for e in engines:
            fields = {
                "prompt_id": pid,
                "prompt_text": bp.prompt.text,
                "engine": e["key"],
                "engine_label": e["label"],
                "mentioned": None,
                "rank": None,
                "sentiment": None,
            }
            if e["configured"]:
                parts[pid][e["key"]] = em.add_part(
                    PartType.ENGINE_CHECK, request=ctx.request, status="queued", error=None,
                    **fields,
                )
            else:
                em.add_part(
                    PartType.ENGINE_CHECK, request=ctx.request, status="unavailable",
                    error=f"{e['key_setting']} is not set", **fields,
                )
    em.checkpoint(force=True)
    if not configured:
        raise ToolError(
            "No AI engine is configured: none of "
            + ", ".join(e["key_setting"] for e in engines) + " is set.",
            summary="No AI engine configured",
        )

    results: dict[str, dict] = {}
    started: dict[str, datetime] = {}

    def fresh(view: dict | None, pid: str) -> bool:
        # Only an answer from this check counts; an older one is not news.
        last = (view or {}).get("last_answered_at")
        if not (view and view.get("answers") and last and pid in started):
            return False
        return datetime.fromisoformat(last) >= started[pid]

    def finish(pid: str, outcome, failure: str | None) -> None:
        bp = found[pid]
        errors = _engine_errors(outcome) if outcome is not None else {}
        detail = None
        if failure is None:
            try:
                detail = build_prompt_detail(ctx.website, bp)
            except Exception:
                logger.warning("agent: prompt detail failed after crawl", exc_info=True)
                failure = "The answers could not be read back."
        by_key = {e["key"]: e for e in (detail or {}).get("engines") or []}
        engines_out = {}
        for e in engines:
            if not e["configured"]:
                engines_out[e["key"]] = {"configured": False, "mentioned": None, "rank": None,
                                         "sentiment": None}
                continue
            part_id = parts[pid][e["key"]]
            message = failure or errors.get(e["provider"])
            view = by_key.get(e["key"])
            if message or not fresh(view, pid):
                message = message or "The engine did not answer."
                em.update_part(part_id, status="error", error=clip(message, 200))
                engines_out[e["key"]] = {"configured": True, "mentioned": None, "rank": None,
                                         "sentiment": None, "error": clip(message, 200)}
                continue
            mentioned = bool(view.get("mentioned"))
            rank = view.get("rank") if mentioned else None
            sentiment = view.get("sentiment") if mentioned else None
            em.update_part(part_id, status="done", mentioned=mentioned, rank=rank,
                           sentiment=sentiment, error=None)
            engines_out[e["key"]] = {"configured": True, "mentioned": mentioned, "rank": rank,
                                     "sentiment": sentiment}
        gap = (detail or {}).get("gap") or {}
        results[pid] = {
            "prompt_id": pid,
            "text": bp.prompt.text,
            "engines": engines_out,
            "gap": {"class": gap.get("class"), "value": gap.get("value")},
        }

    def start(pid: str) -> None:
        started[pid] = timezone.now()
        for part_id in parts[pid].values():
            em.update_part(part_id, status="running")

    def stop_remaining(pids) -> None:
        for pid in pids:
            for part_id in parts[pid].values():
                em.update_part(part_id, status="error", error="Stopped")

    order = list(found)
    if ctx.inline:
        # Tests and inline runs: the crawl sees this thread's transaction.
        for i, pid in enumerate(order):
            if ctx.cancelled():
                stop_remaining(order[i:])
                break
            start(pid)
            try:
                outcome, failure = _crawl(ctx.website, found[pid].prompt, ctx.user,
                                          close_db=False), None
            except Exception:
                logger.warning("agent: crawl_prompt failed", exc_info=True)
                outcome, failure = None, "The scan failed."
            finish(pid, outcome, failure)
            em.checkpoint(force=True)
    else:
        def work(pid: str):
            if ctx.cancelled():
                return pid, None, "stopped"
            close_old_connections()
            start(pid)
            try:
                return pid, _crawl(ctx.website, found[pid].prompt, ctx.user, close_db=True), None
            except Exception:
                logger.warning("agent: crawl_prompt failed", exc_info=True)
                return pid, None, "The scan failed."

        with ThreadPoolExecutor(max_workers=ENGINE_WORKERS,
                                thread_name_prefix="agent-engines") as pool:
            futures = [pool.submit(work, pid) for pid in order]
            for future in as_completed(futures):
                pid, outcome, failure = future.result()
                if failure == "stopped":
                    stop_remaining([pid])
                    continue
                finish(pid, outcome, failure)
                em.checkpoint(force=True)

    if ctx.cancelled() and len(results) < len(order):
        raise ToolError("Stopped before every engine answered.", summary="Stopped")
    payload = {
        "prompts": [results[pid] for pid in order if pid in results],
        "engines_not_configured": [e["label"] for e in engines if not e["configured"]],
        "checked_at": now_iso(),
    }
    if missing:
        payload["unknown_prompt_ids"] = missing
    return ToolResult.ok(payload, summary=f"{len(results)} prompts, {len(configured)} engines")
