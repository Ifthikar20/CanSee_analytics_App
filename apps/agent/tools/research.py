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
import re
import threading
import uuid
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timedelta

from django.db import close_old_connections, connections
from django.db.models import Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from apps.agent.conf import agent_conf
from apps.agent.constants import EventType, MessageRole, PartType
from apps.agent.runtime.events import now_iso
from apps.agent.tools.context import ToolContext, ToolError, ToolResult
from apps.agent.tools.shaping import clip, display_url, host_of, untrusted

logger = logging.getLogger("apps")

PAGE_SIZE = 10
PAGE_EXCERPT_CHARS = 1500
PAGE_TEXT_CHARS = 8000
PAGE_VALUE_CHARS = 300
ENGINE_WORKERS = 3
# How often a running AI engine check looks for Stop.
CANCEL_POLL_SECONDS = 0.5
# SerpAPI answers a repeated search from its cache for an hour, so a page
# this chat captured within the hour is shown again instead of fetched.
SERP_REUSE_SECONDS = 3600
SERP_ERRORS = {
    "network": "Google search could not be reached.",
    "decode": "Google search returned an unreadable response.",
    "empty_query": "The search query was empty.",
    "empty_results": "Google's results page came back empty, so this search could not be read.",
}
DEFERRED_OVERVIEW_NOTE = (
    "Google loaded its AI Overview separately and its content was not read, so whether it "
    "names or cites the brand is not measured."
)
NOT_SELECTED = "Not in this prompt's model selection"
# Punctuation Google ignores in a query: a tracked prompt "What is the best
# CRM?" is the same search as "what is the best crm".
_QUERY_NOISE = re.compile(r"[?!,;:\"'“”‘’«»()\[\]{}]|\.(?=\s|$)")


def _capture_id() -> str:
    return uuid.uuid4().hex[:12]


def query_key(text: str) -> str:
    """``text`` as a search: case, spacing and ignorable punctuation dropped."""
    return " ".join(_QUERY_NOISE.sub(" ", (text or "").lower()).split())


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


def _ai_overview(block: dict, *, website, apex: str, deferred: bool = False) -> dict:
    """One results page's AI Overview (SerpCapture.ai_overview). When Google
    loads the overview separately, SerpAPI returns only a token for it
    (``deferred``): it was shown, but what it says, and so whether it names
    or cites the brand, was never read (content_captured False, mentions_you
    and cites_you None)."""
    from apps.search_gap.services.google_rank import summarize_ai_overview

    summary = summarize_ai_overview(block, brand=website.name, apex=apex, deferred=deferred)
    return {
        "shown": summary["shown"],
        "content_captured": summary["content_captured"],
        "mentions_you": summary["mentions_you"],
        "cites_you": summary["cites_you"],
        "references": [
            {"title": r.get("title") or "", "url": r.get("url") or "",
             "domain": r.get("domain") or ""}
            for r in summary["references"]
        ],
    }


def build_serp_capture(payload: dict, *, website, query: str, page: int, location: str,
                       capture_id: str) -> dict:
    """A SerpCapture (contract section 2e) from one serp_google.search payload."""
    from apps.citations.services.url_normalizer import extract_apex_domain
    from apps.search_gap.services.google_rank import is_same_site, site_apex

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
            # Only the site is known for some ScraperAPI rows; Google's own
            # breadcrumb then says more than the bare site address would.
            "display_url": (row.get("breadcrumb") if row.get("site_only") else "")
            or display_url(row.get("url") or ""),
            "domain": domain,
            "snippet": row.get("snippet") or "",
            "is_yours": is_yours,
            "is_competitor": is_competitor,
        }
        if is_competitor:
            item["competitor_name"] = competitors[comp_apex]
        results.append(item)
    overview = None
    block = payload.get("ai_overview") or {}
    deferred = bool(payload.get("ai_overview_deferred"))
    if page == 1 or block or deferred:
        overview = _ai_overview(block, website=website, apex=apex, deferred=deferred)
    return {
        "capture_id": capture_id,
        "query": query,
        "page": page,
        "start": start,
        "location": location,
        "captured_at": now_iso(),
        "provider": payload.get("provider") or "",
        "brand": {"name": website.name, "domain": apex},
        "brand_positions": [r["position"] for r in results if r["is_yours"]],
        "results": results,
        "ai_overview": overview,
        "people_also_ask": [
            q["question"] for q in (payload.get("questions") or []) if q.get("question")
        ],
        "related_searches": payload.get("related_searches") or [],
        "error": None,
    }


def _tracked_prompt_for(website, query: str):
    """The tracked prompt ``query`` searches (the same words; case, spacing
    and ignorable punctuation aside), active prompts first."""
    from apps.prompt_library.models import BrandPrompt

    wanted = query_key(query)
    for bp in (BrandPrompt.objects.select_related("prompt").filter(website=website)
               .order_by("is_archived", "created_at")):
        if query_key(bp.prompt.text) == wanted:
            return bp
    return None


def _where(location: str) -> str:
    return f"from {location.upper()}" if location else "with no country set"


def _record_rank_check(website, brand_prompt, *, query: str, location: str, pages: list[dict],
                       fetched: int, exhausted: bool):
    """A GoogleRankCheck from the captured pages, so Overview and the prompt
    page show this search. ``depth`` is how deep it looked: the pages
    searched, or the full depth when Google ran out of results first (as
    search_gap's own check records it). ``pages_fetched`` and the cost
    count the pages this search paid for."""
    from apps.citations.services import serp_google
    from apps.search_gap.models import GoogleRankCheck
    from apps.search_gap.services.google_rank import max_depth

    first = pages[0]
    found = next((r for p in pages for r in p["results"] if r["is_yours"]), None)
    overview = first.get("ai_overview") or {}
    depth = len(pages) * PAGE_SIZE
    if exhausted and found is None:
        depth = max(depth, max_depth())
    return GoogleRankCheck.objects.create(
        website=website,
        brand_prompt=brand_prompt,
        query=query[:500],
        location=location,
        depth=depth,
        position=found["position"] if found else None,
        url=(found["url"] if found else "")[:2000],
        title=(found["title"] if found else "")[:500],
        top_results=[
            {"position": r["position"], "url": r["url"], "domain": r["domain"], "title": r["title"]}
            for r in first["results"]
        ],
        ai_overview={
            "shown": bool(overview.get("shown")),
            "content_captured": overview.get("content_captured"),
            "mentions_you": overview.get("mentions_you"),
            "cites_you": overview.get("cites_you"),
            "references": overview.get("references") or [],
        },
        # Page one's People also ask and related searches (prompt research).
        serp_features={
            "questions": [
                {"question": q, "url": ""} for q in first.get("people_also_ask") or []
                if isinstance(q, str) and q
            ],
            "related_searches": [
                s for s in first.get("related_searches") or [] if isinstance(s, str) and s
            ],
        },
        pages_fetched=fetched,
        cost_usd=serp_google.cost_per_search_usd() * fetched,
        error="",
        # When Google was searched: a page shown again keeps its capture time.
        checked_at=min(parse_datetime(p["captured_at"]) for p in pages),
    )


def _search_target(ctx: ToolContext, args):
    """(the tracked prompt or None, the query to search). With prompt_id
    the search is that prompt's own text."""
    if args.prompt_id is not None:
        from apps.agent.tools.data import tracked_prompt

        bp = tracked_prompt(ctx.website, args.prompt_id)
        return bp, " ".join(bp.prompt.text.split())
    return _tracked_prompt_for(ctx.website, args.query), args.query


def _recent_captures(ctx: ToolContext, query: str, location: str) -> dict[int, dict]:
    """Pages of this search (same query and location) that this chat
    captured without an error in the last SERP_REUSE_SECONDS, newest per
    page."""
    from apps.agent.models import AgentMessage

    cutoff = timezone.now() - timedelta(seconds=SERP_REUSE_SECONDS)
    stores = [ctx.emitter.research]
    stores += AgentMessage.objects.filter(
        Q(finished_at__isnull=True) | Q(finished_at__gte=cutoff),
        chat_id=ctx.chat.id, role=MessageRole.ASSISTANT,
    ).values_list("research", flat=True)
    wanted = query_key(query)
    found: dict[int, tuple] = {}
    for store in stores:
        for capture in ((store or {}).get("serp") or {}).values():
            if (capture.get("error") or (capture.get("location") or "") != location
                    or query_key(capture.get("query")) != wanted):
                continue
            captured = parse_datetime(capture.get("captured_at") or "")
            page = capture.get("page")
            if captured is None or captured < cutoff:
                continue
            if page not in found or captured > found[page][0]:
                found[page] = (captured, capture)
    return {page: capture for page, (_, capture) in found.items()}


def _show_again(ctx: ToolContext, capture: dict) -> None:
    """Open an earlier capture of a page again: its own tab (the pane
    merges it into one it still has), a done serp part and this reply's
    research store. No SerpAPI call, nothing counted."""
    em = ctx.emitter
    tab_id = f"serp:{capture['capture_id']}"
    em.add_part(
        PartType.SERP, request=ctx.request, query=capture["query"], page=capture["page"],
        location=capture["location"], status="done", tab_id=tab_id,
        brand_positions=capture["brand_positions"], result_count=len(capture["results"]),
        ai_overview_shown=bool(capture["ai_overview"] and capture["ai_overview"]["shown"]),
        error=None,
    )
    em.emit(EventType.BROWSER_OPEN, tab={
        "id": tab_id,
        "kind": "serp",
        "title": f"Google · {clip(capture['query'], 60)} · p{capture['page']}",
        "status": "ready",
        "data": capture,
    }, activate=capture["page"] == 1)
    em.record_research("serp", capture["capture_id"], capture)


def _fetch_page(ctx: ToolContext, *, query: str, page: int, location: str, gl: str, hl: str,
                quota) -> tuple[dict | None, str | None]:
    """Capture one results page from SerpAPI: (capture, None), or (None,
    why it stopped: daily_limit or error)."""
    from apps.citations.services import serp_google

    em = ctx.emitter
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
        return None, "daily_limit"
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
    capture["cost_usd"] = float(serp_google.cost_per_search_usd())
    if payload.get("error"):
        capture["error"] = _serp_error(payload["error"])
    em.record_research("serp", capture_id, capture)
    em.emit(EventType.BROWSER_UPDATE, tab_id=tab_id,
            status="error" if capture["error"] else "ready", patch=capture)
    if capture["error"]:
        em.update_part(part_id, status="error", error=capture["error"])
        return None, "error"
    em.update_part(
        part_id, status="done", brand_positions=capture["brand_positions"],
        result_count=len(capture["results"]),
        ai_overview_shown=bool(capture["ai_overview"] and capture["ai_overview"]["shown"]),
    )
    return capture, None


def google_search(ctx: ToolContext, args) -> ToolResult:
    from apps.agent.tools.data import ai_overview_summary
    from apps.citations.services import serp_google
    from apps.search_gap.services.google_rank import locale, max_depth

    em = ctx.emitter
    brand_prompt, query = _search_target(ctx, args)
    tracked_location = (brand_prompt.location or "").lower() if brand_prompt else ""
    # A tracked prompt is searched from its own country unless told otherwise.
    location = (args.location or tracked_location).lower()
    if not serp_google.is_configured():
        em.add_part(
            PartType.SERP, request=ctx.request, query=query, page=1, location=location,
            status="error", tab_id=None, brand_positions=[], result_count=0,
            ai_overview_shown=False,
            error={"code": "serp_not_configured",
                   "message": "Google search isn't available: no Google results key (SERPAPI_KEY or SCRAPERAPI_KEY) is set."},
        )
        raise ToolError("Google search isn't available: no Google results key "
                        "(SERPAPI_KEY or SCRAPERAPI_KEY) is set.",
                        summary="Google search not configured")

    gl, hl = locale(location)
    pages_allowed = min(args.pages, agent_conf().serp_max_pages)
    # Never past the pages the provider reads reliably (ScraperAPI: page 1).
    reliable = serp_google.max_reliable_pages()
    pages_wanted = min(pages_allowed, reliable)
    quota = _serp_quota()
    earlier = _recent_captures(ctx, query, location)
    captures: list[dict] = []
    fetched = reused = 0
    stopped_reason = None
    for page in range(1, pages_wanted + 1):
        if ctx.cancelled():
            stopped_reason = "cancelled"
            break
        capture = earlier.get(page)
        if capture is not None:
            _show_again(ctx, capture)
            reused += 1
        else:
            capture, stopped_reason = _fetch_page(ctx, query=query, page=page,
                                                  location=location, gl=gl, hl=hl, quota=quota)
            if capture is None:
                break
            fetched += 1
        captures.append(capture)
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

    positions = [p for c in captures for p in c["brand_positions"]]
    searched = len(captures) * PAGE_SIZE
    exhausted = not captures[-1]["results"]
    rank_check_id = None
    rank_note = None
    if brand_prompt is not None:
        if location != tracked_location:
            rank_note = (
                "Not saved as the prompt's Google rank: the prompt is tracked "
                f"{_where(tracked_location)}, and this search ran {_where(location)}."
            )
        elif positions or exhausted or searched >= max_depth():
            # Only a definite answer replaces the prompt's rank: a position
            # found, or a miss over the full depth a check measures.
            check = _record_rank_check(ctx.website, brand_prompt, query=query,
                                       location=location, pages=captures, fetched=fetched,
                                       exhausted=exhausted)
            rank_check_id = str(check.id)
        else:
            rank_note = (
                "Not saved as the prompt's Google rank: the brand is not in the top "
                f"{searched}, and a miss there says nothing about positions "
                f"{searched + 1}-{max_depth()}. The full "
                f"top-{max_depth()} check runs from the prompt page or on "
                "its schedule."
            )

    first = captures[0]
    result = {
        "query": query,
        "location": location or "global",
        "pages": [
            {
                "page": c["page"],
                "brand_positions": c["brand_positions"],
                "top": [
                    {"position": r["position"], "url": r["url"], "domain": r["domain"],
                     "title": clip(r["title"], 120), "is_yours": r["is_yours"],
                     "is_competitor": r["is_competitor"]}
                    for r in c["results"][:PAGE_SIZE]
                ],
            }
            for c in captures
        ],
        "pages_searched": len(captures),
        "pages_fetched": fetched,
        "pages_reused": reused,
        "cost_usd": float(serp_google.cost_per_search_usd() * fetched),
        "pages_left_today": quota.remaining(str(ctx.website.id)),
        "brand_best_position": min(positions) if positions else None,
        "ai_overview": ai_overview_summary(first["ai_overview"] or {}),
        "people_also_ask": first["people_also_ask"],
        "tracked_prompt_id": str(brand_prompt.prompt_id) if brand_prompt else None,
        "rank_check_recorded": rank_check_id is not None,
        "captured_at": first["captured_at"],
    }
    notes = []
    if reused:
        notes.append(f"{reused} page(s) were captured in this chat within the hour and are "
                     "shown again without a new search.")
    if args.prompt_id is not None and args.query and query_key(args.query) != query_key(query):
        notes.append("Searched the tracked prompt's own text; the query given was not used.")
    if reliable < pages_allowed:
        notes.append(
            f"Only the top {reliable * PAGE_SIZE} were searched: Google results past page "
            f"{reliable} are not reliable through "
            f"{serp_google.PROVIDER_LABELS.get(serp_google.provider(), 'this provider')}, so a "
            "rank deeper than that cannot be measured."
        )
    if stopped_reason == "daily_limit":
        notes.append("The daily Google search allowance ran out; later pages were not searched.")
    elif stopped_reason == "error":
        notes.append("A later page failed; the pages listed were captured.")
    if notes:
        result["note"] = " ".join(notes)
    if rank_note:
        result["rank_note"] = rank_note
    best = result["brand_best_position"]
    summary = f"you are #{best}" if best else f"not in the top {searched}"
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


def _google_urls(store: dict) -> set:
    """Result and AI Overview URLs Google listed in one research store."""
    urls = set()
    for capture in (store.get("serp") or {}).values():
        overview = capture.get("ai_overview") or {}
        urls.update(r.get("url") for r in capture.get("results") or [])
        urls.update(r.get("url") for r in overview.get("references") or [])
    for gap in store.get("gaps") or []:
        google = gap.get("google") or {}
        urls.add(google.get("url"))
        urls.update(r.get("url") for r in google.get("top_results") or [])
    urls.discard(None)
    urls.discard("")
    return urls


def _may_fetch(ctx: ToolContext, url: str) -> bool:
    """Whether inspect_page may fetch ``url`` and open it without a click:
    it is on the project's or a competitor's site, the user wrote it in
    this chat, or Google listed it in this chat's research. Any other
    address (one the model built, or read in page text or an AI answer)
    could carry the chat's data out in its query string, or put a page of
    a third party's choosing in front of the user."""
    from apps.agent.models import AgentMessage
    from apps.agent.tools.actions import project_hosts
    from apps.citations.services.url_normalizer import extract_apex_domain

    if extract_apex_domain(host_of(url)) in project_hosts(ctx.website):
        return True
    if url in _google_urls(ctx.emitter.research):
        return True
    for role, parts, research in AgentMessage.objects.filter(chat_id=ctx.chat.id).values_list(
            "role", "parts", "research"):
        if role == MessageRole.USER:
            if any(p.get("type") == "text" and url in (p.get("text") or "") for p in parts or []):
                return True
        elif url in _google_urls(research or {}):
            return True
    return False


def _model_seo(seo: dict) -> tuple[dict, str]:
    """SeoFacts for the model: counts and lengths as they are, and every
    string the page wrote itself (title, description, headings, canonical,
    robots, language, structured data, Open Graph, hreflang) as lines of
    text, which the result marks as third-party data."""
    og = seo.get("open_graph") or {}
    facts = {
        "title_length": seo.get("title_length"),
        "meta_description_length": seo.get("meta_description_length"),
        "h1_count": len(seo.get("h1") or []),
        "h2_count": len(seo.get("h2") or []),
        "word_count": seo.get("word_count"),
        "links": seo.get("links"),
        "images": seo.get("images"),
        "structured_data_count": len(seo.get("structured_data") or []),
        "hreflang_count": len(seo.get("hreflang") or []),
        "bytes": seo.get("bytes"),
        "fetch_ms": seo.get("fetch_ms"),
    }
    lines = [
        ("Title", seo.get("title")),
        ("Meta description", seo.get("meta_description")),
        *(("H1", h) for h in seo.get("h1") or []),
        *(("H2", h) for h in seo.get("h2") or []),
        ("Canonical", seo.get("canonical")),
        ("Robots", seo.get("robots")),
        ("Language", seo.get("lang")),
        ("Structured data", ", ".join(seo.get("structured_data") or [])),
        ("Open Graph title", og.get("title")),
        ("Open Graph description", og.get("description")),
        ("Open Graph image", og.get("image")),
        *(("Hreflang", f"{h.get('lang')} {h.get('url')}") for h in seo.get("hreflang") or []),
    ]
    text = "\n".join(f"{label}: {clip(value, PAGE_VALUE_CHARS)}" for label, value in lines
                     if value)
    return facts, text


def inspect_page(ctx: ToolContext, args) -> ToolResult:
    url = args.url
    if not url.lower().startswith(("http://", "https://")):
        raise ToolError("inspect_page needs an http or https URL.", summary="Invalid URL")
    em = ctx.emitter
    host = host_of(url) or url
    if not _may_fetch(ctx, url):
        em.add_part(PartType.BROWSER_OPEN, request=ctx.request, url=url, title=clip(host, 120),
                    requires_confirmation=True)
        raise ToolError(
            f"Not inspected: {url} did not come from the user, from Google results in this "
            "chat or from the project's or a competitor's site, so it is shown to the user as "
            "a link to open instead. Inspect a URL from those sources, or ask the user to open "
            "this one.",
            summary="Shown as a link to open",
        )
    capture_id = _capture_id()
    tab_id = f"page:{capture_id}"
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
    facts, page_text = _model_seo(seo)
    return ToolResult.ok({
        "url": capture["url"],
        "final_url": capture["final_url"],
        "status_code": capture["status_code"],
        "frameable": capture["frameable"],
        "seo": facts,
        "page_text": untrusted(capture["final_url"], page_text, PAGE_TEXT_CHARS),
        "excerpt": untrusted(capture["final_url"], _reader_text(capture["reader"]),
                             PAGE_EXCERPT_CHARS),
    }, summary=summary)


# ── track_prompts ───────────────────────────────────────────────────────


def _shown_to_user(ctx: ToolContext) -> str:
    """What the user wrote or was shown before this run, as one query_key
    string: the text of their messages and of the agent's replies on this
    branch. Tool results and this run's own output are left out, so text
    that reached the model during this run cannot vouch for itself."""
    from apps.agent.models import AgentMessage
    from apps.agent.runtime.wire import path_to

    path = [m.id for m in path_to(ctx.run.user_message)]
    chunks = []
    for parts in AgentMessage.objects.filter(id__in=path).values_list("parts", flat=True):
        chunks.extend(p.get("text") or "" for p in parts or [] if p.get("type") == "text")
    return query_key("\n".join(chunks))


def track_prompts(ctx: ToolContext, args) -> ToolResult:
    """Add tracked prompts the way the prompt library's create endpoint does
    (WebsitePromptCreateView): one Prompt per text, matched by text hash and
    filed under the website's own industry (else the inactive
    "Uncategorized" bucket, never an unrelated industry, whose prompts are
    suggested to other websites), and a BrandPrompt for this website.

    Only prompts the user wrote or was shown in an earlier message are
    tracked: the confirmation the system prompt asks for is checked here
    too. It does not start a scan: the agent asks the engines next with
    check_ai_engines."""
    from apps.prompt_library.models import BrandPrompt, Prompt, PromptSource
    from apps.prompt_library.services._hash import text_hash
    from apps.prompt_library.services.industry_match import industry_for_website
    from apps.prompt_library.services.template_parser import extract_variables

    shown = _shown_to_user(ctx)
    unconfirmed = [item.text for item in args.prompts if query_key(item.text) not in shown]
    if unconfirmed:
        listed = "; ".join(f'"{clip(text, 120)}"' for text in unconfirmed)
        raise ToolError(
            f"Not tracked: the user has not seen these prompts in this chat: {listed}. List the "
            "prompts in your reply and ask the user to confirm them; call track_prompts after "
            "they say yes.",
            summary="Needs the user's confirmation",
        )
    industry = None
    created = []
    for item in args.prompts:
        text = " ".join(item.text.split())
        digest = text_hash(text)
        prompt = Prompt.objects.filter(text_hash=digest).order_by("created_at").first()
        if prompt is None:
            industry = industry or industry_for_website(ctx.website)
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


def public_engine_error(raw: str, default: str = "The engine returned an error.") -> str:
    """A provider error as the chat, the model and saved reports show it.
    Raw provider text can hold key prefixes, console URLs or account
    details, so it stays in the logs and the scan's own records (the
    prompt library shows its errors the same way, _public_model_error)."""
    e = (raw or "").strip().lower()
    # Answers the engine gave that are not measurements (ClaudeProvider marks
    # them; the prompt library shows them the same way, _public_model_error).
    if e.startswith("refusal:"):
        return "The engine declined to answer this prompt."
    if e.startswith(("max_tokens:", "pause_turn:")):
        return "The engine's answer was cut off before it finished."
    if e.startswith("empty_answer:"):
        return "The engine returned no answer."
    if "allowance_check_failed" in e:
        return "The AI allowance could not be checked, so the engine was not asked."
    if "allowance" in e:
        return "The monthly AI allowance is used up."
    if "not configured" in e or "not enabled" in e:
        return "The engine is not configured."
    if any(s in e for s in ("rate limit", "429", "quota", "overloaded", "circuit open")):
        return "The engine was busy or rate limited."
    if "timeout" in e or "timed out" in e:
        return "The engine did not respond in time."
    return default


def _engine_errors(outcome, providers) -> tuple[dict[str, str], str | None]:
    """The crawl's error lines, public: by catalog provider, and one line
    for the errors that name no engine (an empty model selection, the
    model registry)."""
    by_provider: dict[str, str] = {}
    other = []
    for line in getattr(outcome, "errors", None) or []:
        head, sep, rest = str(line).partition(":")
        provider = head.strip().lower()
        if sep and provider in providers:
            by_provider[provider] = public_engine_error(rest)
        else:
            other.append(str(line))
    if not other:
        return by_provider, None
    text = " ".join(other).lower()
    if "selection" in text:
        return by_provider, "None of the models selected for this prompt is configured."
    return by_provider, public_engine_error(text, "The scan could not ask the AI engines.")


def _asked_providers(bp) -> set[str] | None:
    """The providers crawl_prompt asks for ``bp``: None when the prompt has
    no model selection (every configured engine), else the providers its
    selected variants name (possibly none)."""
    from apps.llm_ranking.providers import parse_variant

    selected = list(bp.model_variants or [])
    if not selected:
        return None
    parsed = (parse_variant(str(v or "").strip()) for v in selected)
    return {p[0] for p in parsed if p is not None}


def _crawl(website, prompt, user, *, close_db: bool, should_stop=None):
    """crawl_prompt for one prompt. ``should_stop`` (Stop) is checked before
    each engine is asked, so a stopped check asks no further engine."""
    from apps.prompt_library.services import prompt_crawler

    try:
        return prompt_crawler.crawl_prompt(website, prompt, only_missing=False, acting_user=user,
                                           should_stop=should_stop)
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
    providers = {e["provider"] for e in engines}
    configured = [e for e in engines if e["configured"]]
    # Only engines the crawl will ask get a part to wait on.
    parts: dict[str, dict[str, str]] = {}
    for pid, bp in found.items():
        parts[pid] = {}
        asked = _asked_providers(bp)
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
            if not e["configured"]:
                em.add_part(
                    PartType.ENGINE_CHECK, request=ctx.request, status="unavailable",
                    error=f"{e['key_setting']} is not set", **fields,
                )
            elif asked is not None and e["provider"] not in asked:
                em.add_part(
                    PartType.ENGINE_CHECK, request=ctx.request, status="unavailable",
                    error=NOT_SELECTED, **fields,
                )
            else:
                parts[pid][e["key"]] = em.add_part(
                    PartType.ENGINE_CHECK, request=ctx.request, status="queued", error=None,
                    **fields,
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
        errors, prompt_error = (_engine_errors(outcome, providers) if outcome is not None
                                else ({}, None))
        if not parts[pid]:
            prompt_error = "No configured AI engine is in this prompt's model selection."
        detail = None
        if failure is None:
            try:
                detail = build_prompt_detail(ctx.website, bp)
            except Exception:
                logger.warning("agent: prompt detail failed after crawl", exc_info=True)
                failure = "The answers could not be read back."
        if outcome is not None and getattr(outcome, "errors", None):
            logger.info("agent: AI engine check errors for prompt %s: %s", pid, outcome.errors)
        by_key = {e["key"]: e for e in (detail or {}).get("engines") or []}
        engines_out = {}
        for e in engines:
            if not e["configured"]:
                engines_out[e["key"]] = {"configured": False, "mentioned": None, "rank": None,
                                         "sentiment": None}
                continue
            part_id = parts[pid].get(e["key"])
            if part_id is None:
                engines_out[e["key"]] = {"configured": True, "selected": False,
                                         "mentioned": None, "rank": None, "sentiment": None}
                continue
            message = failure or errors.get(e["provider"])
            view = by_key.get(e["key"])
            if message or not fresh(view, pid):
                unanswered = ("Stopped" if getattr(outcome, "stopped", False)
                              else "The engine did not answer.")
                message = message or prompt_error or unanswered
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
        if prompt_error:
            results[pid]["error"] = prompt_error

    def start(pid: str) -> None:
        started[pid] = timezone.now()
        for part_id in parts[pid].values():
            em.update_part(part_id, status="running")

    def stop_remaining(pids) -> None:
        for pid in pids:
            for part_id in parts[pid].values():
                em.update_part(part_id, status="error", error="Stopped")

    order = list(found)
    # A prompt none of whose engines would be asked is reported, not crawled.
    for pid in order:
        if not parts[pid]:
            finish(pid, None, None)
    to_crawl = [pid for pid in order if parts[pid]]
    if ctx.inline:
        # Tests and inline runs: the crawl sees this thread's transaction.
        for i, pid in enumerate(to_crawl):
            if ctx.cancelled():
                stop_remaining(to_crawl[i:])
                break
            start(pid)
            try:
                outcome, failure = _crawl(ctx.website, found[pid].prompt, ctx.user,
                                          close_db=False, should_stop=ctx.cancelled), None
            except Exception:
                logger.warning("agent: crawl_prompt failed", exc_info=True)
                outcome, failure = None, "The scan failed."
            finish(pid, outcome, failure)
            em.checkpoint(force=True)
    else:
        # Workers start a prompt only while the check is still listening;
        # after Stop they leave its parts alone.
        lock = threading.Lock()
        halted = threading.Event()

        def work(pid: str):
            with lock:
                if halted.is_set() or ctx.cancelled():
                    return pid, None, "stopped"
                start(pid)
            close_old_connections()
            try:
                return pid, _crawl(ctx.website, found[pid].prompt, ctx.user, close_db=True,
                                   should_stop=ctx.cancelled), None
            except Exception:
                logger.warning("agent: crawl_prompt failed", exc_info=True)
                return pid, None, "The scan failed."

        pool = ThreadPoolExecutor(max_workers=ENGINE_WORKERS, thread_name_prefix="agent-engines")
        pending = {pool.submit(work, pid): pid for pid in to_crawl}
        try:
            while pending:
                done, _ = wait(pending, timeout=CANCEL_POLL_SECONDS, return_when=FIRST_COMPLETED)
                for future in done:
                    del pending[future]
                    pid, outcome, failure = future.result()
                    if failure == "stopped":
                        stop_remaining([pid])
                        continue
                    finish(pid, outcome, failure)
                    em.checkpoint(force=True)
                if pending and ctx.cancelled():
                    with lock:
                        halted.set()
                    stop_remaining(pending.values())
                    break
        finally:
            # After Stop the crawls already running ask no further engine
            # (should_stop) and end in the background, storing the answers
            # they already have as usual; the run does not wait for them.
            pool.shutdown(wait=not halted.is_set(), cancel_futures=True)

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
