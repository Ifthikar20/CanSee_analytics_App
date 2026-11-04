"""Where the customer's site ranks on Google for a tracked prompt.

SerpAPI returns 10 organic results per call (Google stopped honouring
``num=100``), so a check pages through results until the site appears or
``SEARCH_GAP_MAX_DEPTH`` is reached. A page-one rank costs one call; "not
in the top 100" costs ten. ScraperAPI is read on page one only (its later
pages are not Google's; serp_google.max_reliable_pages), so with it a miss
is "not in the top 10". Recent checks are reused and each website has a
rolling daily cap, so the bill stays bounded.
"""

from __future__ import annotations

import logging
import math
import re
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from apps.citations.services import serp_google
from apps.citations.services.url_normalizer import extract_apex_domain, normalize_url

logger = logging.getLogger("apps")

PAGE_SIZE = 10
MAX_AI_OVERVIEW_REFERENCES = 5


class NotConfigured(Exception):
    """Neither SERPAPI_KEY nor SCRAPERAPI_KEY is set."""


class DailyLimitReached(Exception):
    """The website used its SEARCH_GAP_DAILY_CHECKS_PER_WEBSITE allowance."""


def site_apex(url: str) -> str:
    """Registrable domain of a website URL; tolerates a missing scheme."""
    url = (url or "").strip()
    if url and "://" not in url:
        url = f"https://{url}"
    return normalize_url(url)[2]


def is_same_site(domain: str, apex: str) -> bool:
    """True for the apex itself and any subdomain (www, blog, docs...)."""
    return bool(apex) and extract_apex_domain(domain or "") == apex


def locale(location: str) -> tuple[str, str]:
    """(gl, hl) for SerpAPI from a BrandPrompt.location ISO-2 code."""
    code = (location or "").strip().lower()
    return (code if len(code) == 2 and code.isalpha() else "", "en")


def max_depth() -> int:
    """How deep a rank check searches with the provider in use:
    SEARCH_GAP_MAX_DEPTH, but never past the pages the provider reads
    reliably (ScraperAPI: the top 10)."""
    return min(int(settings.SEARCH_GAP_MAX_DEPTH),
               serp_google.max_reliable_pages() * PAGE_SIZE)


def find_rank(query: str, apex: str, *, max_depth: int = 100, gl: str = "", hl: str = "",
              search=serp_google.search) -> dict:
    """Walk Google's organic results until ``apex`` appears.

    Returns ``{position, url, title, top_results, ai_overview,
    ai_overview_deferred, questions, related_searches, pages_fetched, depth,
    error}``. ``pages_fetched`` counts successful (billed) calls only.
    ``depth`` is how deep the search really looked: ``max_depth``, capped at
    the pages the provider reads reliably, or only the pages read when a
    later page failed. ``ai_overview_deferred``: page one showed an AI
    Overview whose content was not read. ``questions`` (People also ask)
    and ``related_searches`` are page one's. ``search`` is injectable so
    tests never hit the network.
    """
    pages = max(1, math.ceil(max(1, int(max_depth)) / PAGE_SIZE))
    # Never past the pages the provider reads reliably: ScraperAPI's later
    # pages answer a different query, so a rank or a miss there is not real.
    pages = min(pages, serp_google.max_reliable_pages())
    out = {
        "position": None, "url": "", "title": "", "top_results": [],
        "ai_overview": {}, "ai_overview_deferred": False, "questions": [],
        "related_searches": [], "pages_fetched": 0, "depth": pages * PAGE_SIZE, "error": "",
    }
    for page in range(pages):
        start = page * PAGE_SIZE
        payload = search(query, max_results=PAGE_SIZE, start=start, gl=gl, hl=hl)
        if payload.get("error"):
            # A failure past page one keeps what was learned (searched only
            # as deep as the pages read); on page one there is nothing to keep.
            if page == 0:
                out["error"] = payload["error"]
            else:
                out["depth"] = page * PAGE_SIZE
            break
        out["pages_fetched"] += 1
        if page == 0:
            out["ai_overview"] = payload.get("ai_overview") or {}
            out["ai_overview_deferred"] = bool(payload.get("ai_overview_deferred"))
            out["questions"] = payload.get("questions") or []
            out["related_searches"] = payload.get("related_searches") or []
        organic = payload.get("organic") or []
        if not organic:
            break
        for i, row in enumerate(organic):
            position = row.get("position") or start + i + 1
            if page == 0:
                out["top_results"].append({
                    "position": position,
                    "url": row.get("url", ""),
                    "domain": row.get("domain", ""),
                    "title": row.get("title", ""),
                })
            if is_same_site(row.get("domain", ""), apex):
                out.update(position=position, url=row.get("url", ""), title=row.get("title", ""))
                return out
    return out


def summarize_ai_overview(block: dict, *, brand: str, apex: str, deferred: bool = False) -> dict:
    """Did Google's own AI answer show up, name the brand, or cite the site?

    ``deferred``: Google showed an overview whose content was not read
    (serp_google's ``ai_overview_deferred``). With nothing parsed it is
    reported as shown with ``content_captured`` False and mentions_you /
    cites_you None (not measured), never as absent or as not naming the
    brand. ``content_captured`` is True whenever what Google returned was
    read, including when it showed no overview.
    """
    block = block or {}
    text = block.get("text") or ""
    references = block.get("references") or []
    if deferred and not (text or references):
        return {"shown": True, "content_captured": False, "mentions_you": None,
                "cites_you": None, "references": []}
    mentions = bool(brand) and re.search(rf"\b{re.escape(brand)}\b", text, re.IGNORECASE) is not None
    return {
        "shown": bool(text or references),
        "content_captured": True,
        "mentions_you": mentions,
        "cites_you": any(is_same_site(r.get("domain", ""), apex) for r in references),
        "references": references[:MAX_AI_OVERVIEW_REFERENCES],
    }


def serp_features(found: dict) -> dict:
    """``GoogleRankCheck.serp_features`` of a find_rank result: page one's
    People also ask questions and related searches, ``{}`` when page one
    did not come back."""
    if found.get("error"):
        return {}
    return {
        "questions": [
            {"question": q["question"], "url": q.get("url", "")}
            for q in found.get("questions") or [] if isinstance(q, dict) and q.get("question")
        ],
        "related_searches": [s for s in found.get("related_searches") or [] if isinstance(s, str)],
    }


def check(brand_prompt, *, force: bool = False, now=None, depth: int | None = None,
          enforce_daily_cap: bool = True):
    """Look up (or reuse) the Google rank for one tracked prompt.

    Raises NotConfigured without a SerpAPI key and DailyLimitReached when
    the website has used its rolling 24h allowance. ``depth`` replaces
    SEARCH_GAP_MAX_DEPTH (10 reads page one only, with SerpAPI too);
    ``enforce_daily_cap=False`` skips the rolling cap (Autopilot applies
    its own daily page quotas).
    """
    from apps.search_gap.models import GoogleRankCheck

    if not serp_google.is_configured():
        raise NotConfigured()

    now = now or timezone.now()
    website = brand_prompt.website
    if not force:
        recent = (
            GoogleRankCheck.objects
            .filter(
                brand_prompt=brand_prompt, error="",
                checked_at__gte=now - timedelta(hours=settings.SEARCH_GAP_RECHECK_HOURS),
            )
            .order_by("-checked_at")
            .first()
        )
        if recent:
            return recent

    if enforce_daily_cap:
        used = GoogleRankCheck.objects.filter(
            website=website, checked_at__gte=now - timedelta(days=1),
        ).count()
        if used >= settings.SEARCH_GAP_DAILY_CHECKS_PER_WEBSITE:
            raise DailyLimitReached()

    query = brand_prompt.prompt.text.strip()
    gl, hl = locale(brand_prompt.location)
    apex = site_apex(website.url)
    depth_wanted = depth if depth is not None else settings.SEARCH_GAP_MAX_DEPTH
    found = find_rank(query, apex, max_depth=depth_wanted, gl=gl, hl=hl)
    if found["error"]:
        logger.warning("search_gap: Google check failed for %r: %s", query, found["error"])

    pages = found["pages_fetched"]
    return GoogleRankCheck.objects.create(
        website=website,
        brand_prompt=brand_prompt,
        query=query[:500],
        location=brand_prompt.location or "",
        # How deep it really looked ("Not in top 10" with ScraperAPI).
        depth=found.get("depth") or max_depth(),
        position=found["position"],
        url=found["url"][:2000],
        title=found["title"][:500],
        top_results=found["top_results"],
        ai_overview=summarize_ai_overview(found["ai_overview"], brand=website.name, apex=apex,
                                          deferred=bool(found.get("ai_overview_deferred"))),
        serp_features=serp_features(found),
        pages_fetched=pages,
        cost_usd=serp_google.cost_per_search_usd() * pages,
        error=found["error"][:200],
        checked_at=now,
    )
