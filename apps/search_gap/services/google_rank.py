"""Where the customer's site ranks on Google for a tracked prompt.

SerpAPI returns 10 organic results per call (Google stopped honouring
``num=100``), so a check pages through results until the site appears or
``SEARCH_GAP_MAX_DEPTH`` is reached. A page-one rank costs one call; "not
in the top 100" costs ten. Recent checks are reused and each website has
a rolling daily cap, so the bill stays bounded.
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


def find_rank(query: str, apex: str, *, max_depth: int = 100, gl: str = "", hl: str = "",
              search=serp_google.search) -> dict:
    """Walk Google's organic results until ``apex`` appears.

    Returns ``{position, url, title, top_results, ai_overview,
    ai_overview_deferred, pages_fetched, error}``. ``pages_fetched`` counts
    successful (billed) calls only. ``ai_overview_deferred``: page one showed
    an AI Overview whose content was not read. ``search`` is injectable so
    tests never hit the network.
    """
    out = {
        "position": None, "url": "", "title": "", "top_results": [],
        "ai_overview": {}, "ai_overview_deferred": False, "pages_fetched": 0, "error": "",
    }
    pages = max(1, math.ceil(max(1, int(max_depth)) / PAGE_SIZE))
    for page in range(pages):
        start = page * PAGE_SIZE
        payload = search(query, max_results=PAGE_SIZE, start=start, gl=gl, hl=hl)
        if payload.get("error"):
            # A failure past page one keeps what was learned; on page one
            # there is nothing to keep.
            if page == 0:
                out["error"] = payload["error"]
            break
        out["pages_fetched"] += 1
        if page == 0:
            out["ai_overview"] = payload.get("ai_overview") or {}
            out["ai_overview_deferred"] = bool(payload.get("ai_overview_deferred"))
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


def check(brand_prompt, *, force: bool = False, now=None):
    """Look up (or reuse) the Google rank for one tracked prompt.

    Raises NotConfigured without a SerpAPI key and DailyLimitReached when
    the website has used its rolling 24h allowance.
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

    used = GoogleRankCheck.objects.filter(
        website=website, checked_at__gte=now - timedelta(days=1),
    ).count()
    if used >= settings.SEARCH_GAP_DAILY_CHECKS_PER_WEBSITE:
        raise DailyLimitReached()

    query = brand_prompt.prompt.text.strip()
    gl, hl = locale(brand_prompt.location)
    apex = site_apex(website.url)
    depth = settings.SEARCH_GAP_MAX_DEPTH
    found = find_rank(query, apex, max_depth=depth, gl=gl, hl=hl)
    if found["error"]:
        logger.warning("search_gap: Google check failed for %r: %s", query, found["error"])

    pages = found["pages_fetched"]
    return GoogleRankCheck.objects.create(
        website=website,
        brand_prompt=brand_prompt,
        query=query[:500],
        location=brand_prompt.location or "",
        depth=depth,
        position=found["position"],
        url=found["url"][:2000],
        title=found["title"][:500],
        top_results=found["top_results"],
        ai_overview=summarize_ai_overview(found["ai_overview"], brand=website.name, apex=apex,
                                          deferred=bool(found.get("ai_overview_deferred"))),
        pages_fetched=pages,
        cost_usd=serp_google.cost_per_search_usd() * pages,
        error=found["error"][:200],
        checked_at=now,
    )
