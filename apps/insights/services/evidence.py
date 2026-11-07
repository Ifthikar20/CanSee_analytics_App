"""The brand's own evidence for claim checks (backend 3c).

Two kinds: the project's approved brand facts (``BrandFact`` approved or
auto, current version) and up to INSIGHTS_EVIDENCE_PAGES pages of the
project's own site: the home page, the pages its Offerings context entries
cite, then the first home-page link to pricing, about, features,
integrations, security and support. Pages are read once per run with the
SSRF-safe browse fetcher (public addresses only) and reused for
INSIGHTS_EVIDENCE_TTL_HOURS. Reading them costs nothing.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from datetime import timedelta
from urllib.parse import urlsplit

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

from apps.citations.services.url_normalizer import normalize_url
from apps.insights.models import EvidencePage
from apps.insights.services.text import fold
from apps.search_gap.services.google_rank import is_same_site, site_apex

logger = logging.getLogger("apps")

USABLE_FACT_STATUSES = ("approved", "auto")
MAX_FACTS = 80
FACT_TEXT_CHARS = 300
PAGE_TEXT_CHARS = 6_000
# What one answer's check reads: facts while they fit (at most 40), and the
# page texts sharing one budget.
ANSWER_FACT_CHARS = 6_000
ANSWER_MAX_FACTS = 40
ANSWER_PAGE_CHARS = 16_000
BUSY_RETRY_SECONDS = 2.0
# The home-page links read as evidence, one per pattern, in this order.
LINK_PATTERNS = tuple(
    re.compile(rf"/(?:{words})(?:[-_./]|$)")
    for words in (
        "pricing|plans", "about", "features|product|products", "integrations", "security",
        "support",
    )
)
_WORD = re.compile(r"\w+")


@dataclass
class Evidence:
    """What a run checks claims against: ``facts`` ``[{id, fact_id, text,
    source_url}]`` and ``pages`` ``[{id, url, final_url, text, state,
    read_at}]`` (only pages that loaded carry an id and text)."""

    facts: list = field(default_factory=list)
    pages: list = field(default_factory=list)

    @property
    def readable_pages(self) -> list:
        return [p for p in self.pages if p["state"] == "ok"]

    @property
    def empty(self) -> bool:
        return not self.facts and not self.readable_pages


def home_url(website) -> str:
    url = (website.url or "").strip()
    return url if "://" in url else f"https://{url}"


def _links_key(website) -> str:
    return f"insights:home-links:{website.id}"


def home_links(website, *, fetch: bool = True) -> list[dict]:
    """The home page's links ``[{url, text}]`` (menus included), cached for
    INSIGHTS_EVIDENCE_TTL_HOURS. Without ``fetch`` only the cached ones."""
    key = _links_key(website)
    links = cache.get(key)
    if links is not None or not fetch:
        return links or []
    from apps.agent.services import browse

    try:
        result = browse.fetch_page(home_url(website), with_links=True)
    except Exception:
        logger.warning("insights: home page links of %s could not be read", website.id,
                       exc_info=True)
        return []
    if result.get("error"):
        return []
    links = [
        {"url": str(link.get("url") or ""), "text": str(link.get("text") or "")}
        for link in result.get("links") or [] if isinstance(link, dict)
    ]
    cache.set(key, links, timeout=int(settings.INSIGHTS_EVIDENCE_TTL_HOURS) * 3600)
    return links


def _offering_urls(website) -> list[str]:
    try:
        from apps.agent.services.context import offering_urls

        return list(offering_urls(website))
    except Exception:
        logger.warning("insights: offering pages of %s could not be read", website.id,
                       exc_info=True)
        return []


def planned_pages(website, *, fetch: bool = True) -> list[str]:
    """The pages read as evidence, at most INSIGHTS_EVIDENCE_PAGES. Options
    and estimates call it without ``fetch`` (no network): then the home
    page's links come only from the cache."""
    apex = site_apex(website.url)
    limit = max(0, int(settings.INSIGHTS_EVIDENCE_PAGES))
    out: list[str] = []
    seen: set[str] = set()

    def add(url: str) -> None:
        normalized, host, _apex = normalize_url(url)
        if normalized and normalized not in seen and is_same_site(host, apex):
            seen.add(normalized)
            out.append(url)

    add(home_url(website))
    for url in _offering_urls(website):
        add(url)
    if len(out) < limit:
        links = home_links(website, fetch=fetch)
        for pattern in LINK_PATTERNS:
            for link in links:
                url = link["url"]
                try:
                    parts = urlsplit(url)
                except ValueError:
                    continue
                if (parts.scheme in ("http", "https") and is_same_site(parts.hostname or "", apex)
                        and pattern.search(parts.path.lower())):
                    add(url)
                    break
    return out[:limit]


def current_facts(website):
    from apps.brand_vault.models import BrandFact

    return BrandFact.objects.filter(
        website=website, version_to__isnull=True, status__in=USABLE_FACT_STATUSES,
    )


def fact_counts(website) -> dict:
    """``{facts, facts_pending}``: current approved (or auto) and pending facts."""
    from apps.brand_vault.models import BrandFact

    pending = BrandFact.objects.filter(
        website=website, version_to__isnull=True, status="pending",
    ).count()
    return {"facts": current_facts(website).count(), "facts_pending": pending}


def fact_text(fact) -> str:
    return f"{fact.subject} {fact.predicate} {fact.object}"[:FACT_TEXT_CHARS]


def reader_text(result: dict) -> str:
    """The reader view's text blocks, joined by blank lines."""
    reader = result.get("reader") or {}
    chunks = []
    for block in reader.get("blocks") or []:
        kind = block.get("type")
        if kind in ("heading", "paragraph", "quote", "code"):
            chunks.append(block.get("text") or "")
        elif kind == "list":
            chunks.extend(block.get("items") or [])
        elif kind == "table":
            chunks.extend(" | ".join(row) for row in (block.get("rows") or []))
    return "\n\n".join(c for c in chunks if c)[:PAGE_TEXT_CHARS]


def _read(url: str) -> dict:
    from apps.agent.services import browse

    try:
        result = browse.fetch_page(url)
        if (result.get("error") or {}).get("code") == "busy":
            time.sleep(BUSY_RETRY_SECONDS)
            result = browse.fetch_page(url)
    except Exception:
        logger.warning("insights: evidence page %s could not be read", url, exc_info=True)
        return {"url": url, "final_url": "", "status_code": None, "error": {"code": "fetch_failed"}}
    return result


def read_page(website, url: str) -> EvidencePage:
    """The stored read of ``url``, or a fresh one when it is older than
    INSIGHTS_EVIDENCE_TTL_HOURS or did not load. A failed read (a timeout,
    a busy parser) is tried again by the next run instead of standing for
    a day: kept, it would leave every claim checked meanwhile without that
    page, stored as not verifiable."""
    fresh_after = timezone.now() - timedelta(hours=int(settings.INSIGHTS_EVIDENCE_TTL_HOURS))
    page = EvidencePage.objects.filter(website=website, url=url[:2000]).first()
    if page is not None and page.read_at >= fresh_after and not page.error_code:
        return page
    result = _read(url)
    error = (result.get("error") or {}).get("code") or ""
    status = result.get("status_code")
    if not error and not (isinstance(status, int) and 200 <= status < 300):
        error = "fetch_failed"
    values = {
        "final_url": str(result.get("final_url") or "")[:2000],
        "status_code": status if isinstance(status, int) else None,
        "title": str(result.get("title") or "")[:300] if not error else "",
        "text": reader_text(result) if not error else "",
        "error_code": error[:32],
        "read_at": timezone.now(),
    }
    page, _created = EvidencePage.objects.update_or_create(
        website=website, url=url[:2000], defaults=values,
    )
    return page


def gather(ctx) -> Evidence:
    """The run's evidence: facts, and its planned pages read (or reused)."""
    website = ctx.website
    facts = []
    for i, fact in enumerate(current_facts(website).order_by("-updated_at")[:MAX_FACTS], 1):
        facts.append({
            "id": f"f{i}", "fact_id": str(fact.id), "text": fact_text(fact),
            "source_url": fact.source_url or "",
        })
    pages = []
    n = 0
    for url in planned_pages(website, fetch=True):
        if ctx.cancelled():
            break
        page = read_page(website, url)
        ok = not page.error_code and bool(page.text)
        entry = {
            "id": None, "url": url, "final_url": page.final_url or url, "text": page.text,
            "state": "ok" if ok else "failed", "read_at": page.read_at,
        }
        if ok:
            n += 1
            entry["id"] = f"p{n}"
        pages.append(entry)
    return Evidence(facts=facts, pages=pages)


def water_caps(sizes: list[int], total: int) -> list[int]:
    """Per-item caps sharing ``total``: short items whole, the long ones an
    equal share of what is left."""
    caps = [0] * len(sizes)
    left = max(0, total)
    order = sorted(range(len(sizes)), key=lambda i: sizes[i])
    for done, i in enumerate(order):
        share = left // (len(sizes) - done)
        caps[i] = min(sizes[i], share)
        left -= caps[i]
    return caps


def _words(text: str) -> set[str]:
    return set(_WORD.findall(fold(text)))


def for_answer(evidence: Evidence, claims: list[dict]) -> tuple[list[dict], list[dict]]:
    """(facts, pages) one answer's check reads: facts ranked by word overlap
    with its claims (ties: newest first) while they fit, and the readable
    pages' texts sharing one budget."""
    wanted: set[str] = set()
    for claim in claims:
        wanted |= _words(claim["text"])
    ranked = sorted(
        enumerate(evidence.facts),
        key=lambda pair: (-len(_words(pair[1]["text"]) & wanted), pair[0]),
    )
    facts, used = [], 0
    for _i, fact in ranked:
        if len(facts) >= ANSWER_MAX_FACTS or used + len(fact["text"]) > ANSWER_FACT_CHARS:
            break
        facts.append(fact)
        used += len(fact["text"])
    readable = evidence.readable_pages
    caps = water_caps([len(p["text"]) for p in readable], ANSWER_PAGE_CHARS)
    pages = [{**page, "text": page["text"][:cap]} for page, cap in zip(readable, caps, strict=True)]
    return facts, pages
