"""Crawler access (contract 5): what AI crawlers may read and what they get.

A ``site_audit`` run reads, for the project's domain and then each tracked
competitor's, ``/robots.txt`` (RFC 9309), ``/llms.txt`` and
``/llms-full.txt``, then fetches pages the way a crawler first receives
them: the HTML as served, no JavaScript run. Own pages are the home page,
the project's known URLs (Pages, most signals first) and, when fewer than
five are known, the home page's internal links; a competitor gets its home
page. Pages are fetched with the browse fetcher's own user agent: no
crawler is impersonated, so a site that answers AI crawlers differently can
serve them other pages. Nothing is billed; a cooldown and a daily run cap
bound the fetching.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import timedelta
from urllib.parse import urlsplit

from django.conf import settings
from django.db.models import F
from django.utils import timezone

from apps.agent.services import browse
from apps.citations.services.url_normalizer import extract_apex_domain
from apps.insights.models import InsightRun, SiteAudit, SiteAuditPage
from apps.insights.services import llms_txt, robots, runs
from apps.insights.services.runs import ITEM_MESSAGES, iso
from apps.search_gap.services.google_rank import site_apex
from apps.web_analytics.services.ai_crawlers import PURPOSES
from core.exceptions import CanseeException

KEY = "site_audit"
# Own pages are filled from the home page's links while fewer are known.
LINKS_BELOW = 5
BUSY_RETRY_SECONDS = 2.0
MAX_COMPETITORS = 5

# Contract 5a. Tokens and whether each bot follows robots.txt, from the
# operators' own pages (read 2026-11-03): OpenAI's bots page, Anthropic
# support article 8896518, Perplexity's bots guide, Google's common
# crawlers page and Apple support 119829. The keys and names the wave 1
# Cloudflare catalog (web_analytics.services.ai_crawlers) shares are kept.
# (key, robots.txt token, operator, purpose, follows robots.txt)
SITE_AUDIT_BOTS = (
    ("gptbot", "GPTBot", "OpenAI", "training", "yes"),
    ("oai-searchbot", "OAI-SearchBot", "OpenAI", "search", "yes"),
    ("chatgpt-user", "ChatGPT-User", "OpenAI", "user", "may_not"),
    ("claudebot", "ClaudeBot", "Anthropic", "training", "yes"),
    ("claude-searchbot", "Claude-SearchBot", "Anthropic", "search", "yes"),
    ("claude-user", "Claude-User", "Anthropic", "user", "yes"),
    ("perplexitybot", "PerplexityBot", "Perplexity", "search", "yes"),
    ("perplexity-user", "Perplexity-User", "Perplexity", "user", "no"),
    ("google-extended", "Google-Extended", "Google", "training", "token_only"),
    ("googlebot", "Googlebot", "Google", "search", "yes"),
    ("applebot-extended", "Applebot-Extended", "Apple", "training", "token_only"),
    ("ccbot", "CCBot", "Common Crawl", "training", "unknown"),
    ("bytespider", "Bytespider", "ByteDance", "training", "unknown"),
    ("amazonbot", "Amazonbot", "Amazon", "search", "unknown"),
    ("meta-externalagent", "meta-externalagent", "Meta", "training", "unknown"),
    ("meta-externalfetcher", "meta-externalfetcher", "Meta", "user", "unknown"),
    ("duckassistbot", "DuckAssistBot", "DuckDuckGo", "search", "unknown"),
    ("mistralai-user", "MistralAI-User", "Mistral", "user", "unknown"),
)
BOT_KEYS = tuple(key for key, *_rest in SITE_AUDIT_BOTS)
# The ai_search_allowed check: robots.txt lets all three fetch the page.
AI_SEARCH_BOTS = ("oai-searchbot", "claude-searchbot", "perplexitybot")
_TOKENS = frozenset(token.casefold() for _key, token, *_rest in SITE_AUDIT_BOTS)
_PURPOSE_LABELS = dict(PURPOSES)

# Contract 5b: (key, label, rule). A check is true, false or None (not checked).
SITE_AUDIT_CHECKS = (
    ("status_ok", "Loads", "The final response status is 200-299."),
    ("indexable", "Indexable", "No noindex in the robots meta tag or the X-Robots-Tag header."),
    ("title", "Has a title", "The <title> is not empty."),
    ("meta_description", "Has a meta description", "The meta description is not empty."),
    ("h1", "Has an H1", "At least one H1."),
    ("text_without_js", "Text without JavaScript",
     "At least {min_words} words are in the HTML before any script runs."),
    ("structured_data", "Structured data", "At least one JSON-LD type."),
    ("canonical", "Canonical link", "A rel=canonical link is present."),
    ("ai_search_allowed", "Open to AI search bots",
     "robots.txt lets OAI-SearchBot, Claude-SearchBot and PerplexityBot fetch the URL."),
)
CHECK_KEYS = tuple(key for key, _label, _rule in SITE_AUDIT_CHECKS)
_PUBLIC_ERRORS = ("blocked_url", "timeout")


def bot_catalog() -> list[dict]:
    return [
        {"key": key, "token": token, "operator": operator, "purpose": purpose,
         "purpose_label": _PURPOSE_LABELS.get(purpose, purpose), "robots": follows}
        for key, token, operator, purpose, follows in SITE_AUDIT_BOTS
    ]


def checks_catalog() -> list[dict]:
    words = settings.SITE_AUDIT_MIN_WORDS
    return [
        {"key": key, "label": label, "rule": rule.format(min_words=words)}
        for key, label, rule in SITE_AUDIT_CHECKS
    ]


def public_error(code: str) -> dict | None:
    """A page's fetch error as a contract 1e item error."""
    if not code:
        return None
    code = code if code in _PUBLIC_ERRORS else "fetch_failed"
    return {"code": code, "message": ITEM_MESSAGES[code]}


# -- brands ----------------------------------------------------------------------


@dataclass(frozen=True)
class Brand:
    """A domain an audit reads: ``you`` or a tracked competitor."""

    key: str
    name: str
    domain: str       # host without www.
    origin: str       # scheme://host[:port], where robots.txt and llms.txt live
    home_url: str
    apex: str


def _bare(host: str) -> str:
    host = (host or "").strip().lower().rstrip(".")
    return host[4:] if host.startswith("www.") else host


def own_brand(website) -> Brand | None:
    from apps.insights.services.pages import site_origin

    origin = site_origin(website)
    if not origin:
        return None
    url = (website.url or "").strip()
    home = url if "://" in url else f"https://{url}"
    host = urlsplit(origin).hostname or ""
    return Brand("you", website.name, _bare(host), origin, home, site_apex(website.url))


def competitor_brands(website) -> list[Brand]:
    """The tracked competitors whose domain can be read, keyed b1..b5 by
    their place in the stored list."""
    own_apex = site_apex(website.url)
    out = []
    for i, item in enumerate((website.competitors or [])[:MAX_COMPETITORS], start=1):
        if not isinstance(item, dict):
            continue
        raw = str(item.get("domain") or "").strip().lower()
        host = urlsplit(raw if "://" in raw else f"https://{raw}").hostname if raw else ""
        apex = extract_apex_domain(host or "")
        if not host or "." not in host or not apex or apex == own_apex:
            continue
        name = " ".join(str(item.get("name") or "").split()) or _bare(host)
        origin = f"https://{host}"
        out.append(Brand(f"b{i}", name, _bare(host), origin, f"{origin}/", apex))
    return out


# -- planning --------------------------------------------------------------------


def _page_key(url: str, host: str) -> str | None:
    from apps.insights.services.pages import page_key

    return page_key(url, host)


def known_pages(website, own: Brand) -> list[str]:
    """The home page, then the project's known pages, most signals first,
    up to SITE_AUDIT_MAX_PAGES."""
    from apps.insights.services import pages

    limit = max(1, settings.SITE_AUDIT_MAX_PAGES)
    planned = [own.home_url]
    seen = {_page_key(own.home_url, own.domain)}
    ranked = sorted(pages.known_urls(website).values(),
                    key=lambda page: (-page.signal_count(), page.path))
    for page in ranked:
        if len(planned) >= limit:
            break
        if page.key not in seen:
            seen.add(page.key)
            planned.append(page.url)
    return planned


def _link_ok(url: str, host: str) -> bool:
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    if parts.scheme.lower() not in ("http", "https") or parts.query or parts.fragment:
        return False
    if _bare(parts.hostname or "") != host:
        return False
    last = parts.path.rsplit("/", 1)[-1]
    return "." not in last or last.rsplit(".", 1)[-1].lower() == "html"


def links_to_add(links: list, planned: list[str], host: str) -> list[str]:
    """Internal links of the home page to audit after ``planned`` (same
    host, no query, no file other than .html), up to SITE_AUDIT_MAX_PAGES."""
    limit = max(1, settings.SITE_AUDIT_MAX_PAGES)
    seen = {_page_key(url, host) for url in planned}
    out = []
    for link in links or []:
        url = str((link or {}).get("url") or "")
        key = _page_key(url, host)
        if len(planned) + len(out) >= limit:
            break
        if key and key not in seen and _link_ok(url, host):
            seen.add(key)
            out.append(url)
    return out


@dataclass
class AuditPlan:
    own: Brand
    pages: list[str]
    competitors: list[Brand] = field(default_factory=list)

    @property
    def more_from_links(self) -> bool:
        return len(self.pages) < LINKS_BELOW

    @property
    def pages_max(self) -> int:
        """Own pages at most: the home page's links fill up to the cap."""
        if self.more_from_links:
            return max(len(self.pages), settings.SITE_AUDIT_MAX_PAGES)
        return len(self.pages)


def _plan(website, *, competitors: bool = True) -> AuditPlan:
    own = own_brand(website)
    if own is None:
        raise CanseeException(
            "This project has no website address to audit.", code="nothing_to_run",
            status_code=400,
        )
    return AuditPlan(own, known_pages(website, own),
                     competitor_brands(website) if competitors else [])


def validate(website, params) -> AuditPlan:
    include = params.get("competitors", True)
    if not isinstance(include, bool):
        message = "Choose whether to include competitors."
        raise CanseeException(message, code="invalid", status_code=400,
                              details={"field": "competitors", "message": message})
    return _plan(website, competitors=include)


def _latest_own_at(website):
    return (
        SiteAudit.objects.filter(website=website, brand_key="you")
        .order_by("-audited_at").values_list("audited_at", flat=True).first()
    )


def _cooldown_until(website):
    latest = _latest_own_at(website)
    if latest is None:
        return None
    until = latest + timedelta(minutes=settings.SITE_AUDIT_COOLDOWN_MINUTES)
    return until if until > timezone.now() else None


def blockers(website) -> list[dict]:
    """The audit's own caps (contract 1e): one audit per cooldown and a
    number of audits per UTC day."""
    now = timezone.now()
    out = []
    until = _cooldown_until(website)
    if until is not None:
        latest = _latest_own_at(website)
        minutes = max(1, int((now - latest).total_seconds() // 60))
        out.append({
            "code": "cooldown", "status": 409,
            "message": f"An audit ran {minutes} minutes ago. Run it again after "
                       f"{until:%H:%M} UTC.",
            "details": {"cooldown_until": iso(until)},
        })
    limit = settings.SITE_AUDIT_DAILY_RUNS
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    count = InsightRun.objects.filter(website=website, kind=KEY, created_at__gte=today).count()
    if count >= limit:
        out.append({
            "code": "daily_runs", "status": 409,
            "message": f"This project ran {count} audits today; the limit is {limit}.",
        })
    return out


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def estimate(website, user, plan: AuditPlan) -> dict:
    pages = plan.pages_max
    competitors = len(plan.competitors)
    summary = f"{_plural(pages, 'page')} of {plan.own.domain}"
    if competitors:
        summary += f" and {competitors} competitor home {'page' if competitors == 1 else 'pages'}"
    return {
        "summary": summary,
        "units": {"pages": pages, "competitors": competitors},
        "ai": {"calls_max": 0, "usd_max": "0"},
        "google": {"pages_max": 0},
        # Pages, plus robots.txt, llms.txt and llms-full.txt of every domain.
        "fetches": {"pages_max": pages + competitors + 3 * (1 + competitors)},
        "warnings": [],
        "blockers": blockers(website),
    }


def choices(website, user) -> dict:
    try:
        plan = _plan(website)
    except CanseeException:
        return {"domain": None, "competitors": [], "pages_planned": 0, "cooldown_until": None}
    return {
        "domain": plan.own.domain,
        "competitors": [{"key": b.key, "name": b.name, "domain": b.domain}
                        for b in plan.competitors],
        "pages_planned": plan.pages_max,
        "cooldown_until": iso(_cooldown_until(website)),
    }


def plan_items(run, plan: AuditPlan) -> int:
    # No item rows: progress uses the run's unit counters.
    return plan.pages_max + len(plan.competitors)


# -- checks ------------------------------------------------------------------------


def _directives(value) -> list[str]:
    return [part.strip().casefold() for part in str(value or "").split(",") if part.strip()]


def meta_noindex(content) -> bool:
    """``noindex`` or ``none`` in a robots meta tag's content."""
    return any(d in ("noindex", "none") for d in _directives(content))


def header_noindex(value) -> bool:
    """``noindex`` or ``none`` in an X-Robots-Tag header. A directive with a
    ``name:`` prefix applies only when the name is a catalog bot's token
    (``GPTBot: noindex``); other prefixes name other bots or carry a value
    (``unavailable_after: ...``)."""
    for directive in _directives(value):
        if ":" in directive:
            name, directive = directive.split(":", 1)
            if name.strip() not in _TOKENS:
                continue
            directive = directive.strip()
        if directive in ("noindex", "none"):
            return True
    return False


class RobotsRules:
    """A domain's robots.txt evaluated for every catalog bot."""

    def __init__(self, fetched: robots.Fetched):
        self.state = fetched.state
        # (matched user agents, compiled rules) per bot; bots that fall back
        # to the same groups share one compiled set.
        self.rules: dict = {}
        compiled: dict = {}
        if fetched.robots is not None:
            for key, token, *_rest in SITE_AUDIT_BOTS:
                agents, rules = robots.rules_for(fetched.robots, token)
                if tuple(rules) not in compiled:
                    compiled[tuple(rules)] = robots.Matcher(rules)
                self.rules[key] = (agents, compiled[tuple(rules)])

    def blocked(self, url: str) -> list[str] | None:
        """Catalog bots robots.txt keeps from ``url``; None when unknown."""
        if self.state == robots.FAILED:
            return None
        if self.state == robots.UNREACHABLE:
            # RFC 9309 2.3.1.4: assume complete disallow.
            return list(BOT_KEYS)
        if self.state != robots.FOUND:
            return []
        path = robots.url_path(url)
        return [key for key in BOT_KEYS if not self.rules[key][1].check(path)[0]]

    def bots(self, home_url: str, rows: list) -> list[dict]:
        """Each bot's verdict on the home page and the pages it may not read."""
        out = []
        for key in BOT_KEYS:
            entry = {"key": key, "home": "unknown", "group": None, "rule": None,
                     "pages_blocked": None}
            if self.state == robots.FOUND:
                agents, matcher = self.rules[key]
                allowed, rule = matcher.check(robots.url_path(home_url))
                entry.update(home="allowed" if allowed else "blocked",
                             group=", ".join(agents) or None, rule=rule,
                             pages_blocked=sum(1 for row in rows if key in row.blocked_bots))
            elif self.state == robots.MISSING:
                entry.update(home="allowed", pages_blocked=0)
            elif self.state == robots.UNREACHABLE:
                entry.update(home="blocked_unreachable", pages_blocked=len(rows))
            out.append(entry)
        return out


def page_fields(result: dict, url: str, rules: RobotsRules) -> dict:
    """A SiteAuditPage's fields from a fetch_page result (contract 5b). A
    page that did not load fails ``status_ok`` and checks nothing else."""
    error = result.get("error") or None
    status = result.get("status_code")
    loaded = error is None and isinstance(status, int) and 200 <= status < 300
    checks = dict.fromkeys(CHECK_KEYS)
    checks["status_ok"] = loaded
    blocked = rules.blocked(url)
    seo = result.get("seo") or {}
    fields = {"title": "", "words": None, "jsonld_types": [], "canonical": "",
              "noindex_source": ""}
    if loaded:
        headers = result.get("headers") or {}
        if meta_noindex(seo.get("robots")):
            fields["noindex_source"] = "meta"
        elif header_noindex(headers.get("x-robots-tag")):
            fields["noindex_source"] = "header"
        words = seo.get("word_count")
        types = [t for t in seo.get("structured_data") or [] if isinstance(t, str)]
        canonical = seo.get("canonical") or ""
        checks.update({
            "indexable": not fields["noindex_source"],
            "title": bool(seo.get("title")),
            "meta_description": bool(seo.get("meta_description")),
            "h1": bool(seo.get("h1")),
            "text_without_js": isinstance(words, int) and words >= settings.SITE_AUDIT_MIN_WORDS,
            "structured_data": bool(types),
            "canonical": bool(canonical),
            "ai_search_allowed": (
                None if blocked is None else not any(k in blocked for k in AI_SEARCH_BOTS)
            ),
        })
        fields.update(title=(seo.get("title") or "")[:300], words=words if isinstance(words, int)
                      else None, jsonld_types=types, canonical=canonical[:2000])
    measured = [value for value in checks.values() if value is not None]
    return {
        **fields,
        "url": url[:2000],
        "final_url": str(result.get("final_url") or url)[:2000],
        "status_code": status if isinstance(status, int) else None,
        "checks": checks,
        "passed": sum(1 for value in measured if value),
        "total": len(measured),
        "blocked_bots": blocked or [],
        "error_code": str((error or {}).get("code") or "")[:32],
    }


def summary(rows: list) -> dict:
    by_check = {key: {"passed": 0, "failed": 0, "not_checked": 0} for key in CHECK_KEYS}
    for row in rows:
        for key in CHECK_KEYS:
            value = (row.checks or {}).get(key)
            by_check[key]["not_checked" if value is None else "passed" if value else "failed"] += 1
    return {
        "pages": len(rows),
        "passed": sum(row.passed or 0 for row in rows),
        "total": sum(row.total or 0 for row in rows),
        "by_check": by_check,
    }


# -- execution ---------------------------------------------------------------------


def _fetch(url: str, *, with_links: bool = False) -> dict:
    result = browse.fetch_page(url, with_headers=True, with_links=with_links)
    if (result.get("error") or {}).get("code") == "busy":
        # The parser's slots were taken: one retry once they may be free.
        time.sleep(BUSY_RETRY_SECONDS)
        result = browse.fetch_page(url, with_headers=True, with_links=with_links)
    return result


def _save_page(ctx, audit, url: str, result: dict, rules: RobotsRules) -> None:
    fields = page_fields(result, url, rules)
    SiteAuditPage.objects.create(audit=audit, **fields)
    error = public_error(fields["error_code"])
    if error is None:
        ctx.end_item("done")
    else:
        ctx.end_item("failed", error=error["code"])


def _audit_page(ctx, audit, url: str, rules: RobotsRules) -> None:
    if ctx.cancelled():
        # Left unfinished: finalize ends it as stopped.
        return
    ctx.start_item()
    _save_page(ctx, audit, url, _fetch(url), rules)


def _open(ctx, brand: Brand) -> tuple[SiteAudit, RobotsRules]:
    fetched = robots.fetch(brand.origin)
    llms = llms_txt.fetch(brand.origin)
    audit = SiteAudit.objects.create(
        run=ctx.run, website=ctx.website, brand_key=brand.key, domain=brand.domain[:253],
        home_url=brand.home_url[:2000], robots=fetched.info, robots_text=fetched.text,
        bots=[], llms_txt=llms, summary={}, audited_at=timezone.now(),
    )
    return audit, RobotsRules(fetched)


def _close(audit: SiteAudit, rules: RobotsRules) -> None:
    """Each bot's verdicts and the check totals, once the pages are read."""
    rows = list(audit.pages.order_by("fetched_at", "id"))
    audit.bots = rules.bots(audit.home_url, rows)
    audit.summary = summary(rows)
    audit.save(update_fields=["bots", "summary"])


def _audit_own(ctx, own: Brand, competitors: int) -> None:
    """The project's domain: the home page first, whose links fill the
    plan when fewer than LINKS_BELOW pages are known, then the rest."""
    if ctx.cancelled():
        return
    audit, rules = _open(ctx, own)
    if not ctx.cancelled():
        ctx.start_item()
        home = _fetch(own.home_url, with_links=True)
        planned = known_pages(ctx.website, own)
        rest = planned[1:]
        if len(planned) < LINKS_BELOW:
            rest += links_to_add(home.get("links"), planned, own.domain)
        total = 1 + len(rest) + competitors
        if total != ctx.run.units_total:
            # The units were planned before the home page's links were read.
            InsightRun.objects.filter(id=ctx.run.id).update(
                units_total=F("units_total") + (total - ctx.run.units_total),
            )
        _save_page(ctx, audit, own.home_url, home, rules)
        with ctx.pool(settings.SITE_AUDIT_CONCURRENCY) as pool:
            for url in rest:
                pool.submit(_audit_page, ctx, audit, url, rules)
    _close(audit, rules)


def _audit_competitor(ctx, brand: Brand) -> None:
    if ctx.cancelled():
        return
    audit, rules = _open(ctx, brand)
    _audit_page(ctx, audit, brand.home_url, rules)
    _close(audit, rules)


def execute(ctx) -> None:
    own = own_brand(ctx.website)
    if own is None:
        ctx.errors["fetch_failed"] += 1
        return
    include = (ctx.run.params or {}).get("competitors", True) is not False
    competitors = competitor_brands(ctx.website) if include else []
    _audit_own(ctx, own, len(competitors))
    for brand in competitors:
        _audit_competitor(ctx, brand)


# -- presenters ------------------------------------------------------------------


def _complete(website):
    # An audit whose bookkeeping finished (a crash mid-domain leaves none).
    return SiteAudit.objects.filter(website=website, summary__has_key="pages")


def latest_own_audit(website) -> SiteAudit | None:
    """The latest finished audit of the project's own domain."""
    apex = site_apex(website.url)
    for audit in _complete(website).filter(brand_key="you").order_by("-audited_at")[:20]:
        if extract_apex_domain(audit.domain) == apex:
            return audit
    return None


def latest_by_apex(website, apexes) -> dict:
    """{apex: the latest finished audit of a domain with that apex}."""
    wanted, found = set(apexes), {}
    for audit in _complete(website).order_by("-audited_at").iterator(chunk_size=200):
        apex = extract_apex_domain(audit.domain)
        if apex in wanted and apex not in found:
            found[apex] = audit
            if len(found) == len(wanted):
                break
    return found


def page_payload(row) -> dict:
    return {
        "url": row.url,
        "final_url": row.final_url or row.url,
        "status_code": row.status_code,
        "checks": row.checks,
        "passed": row.passed,
        "total": row.total,
        "words": row.words,
        "jsonld_types": row.jsonld_types,
        "canonical": row.canonical or None,
        "noindex_source": row.noindex_source or None,
        "blocked_bots": row.blocked_bots,
        "error": public_error(row.error_code),
    }


def audit_payload(audit: SiteAudit, brand: Brand | None) -> dict:
    return {
        "brand_key": brand.key if brand else audit.brand_key,
        "name": brand.name if brand else audit.domain,
        "domain": audit.domain,
        "audited_at": iso(audit.audited_at),
        "run_id": str(audit.run_id),
        "robots": audit.robots,
        "bots": audit.bots,
        "llms_txt": audit.llms_txt,
        "pages": [page_payload(row) for row in audit.pages.order_by("fetched_at", "id")],
        "summary": audit.summary,
    }


def crawler_access(website) -> dict:
    """The Crawler access payload (contract 5d)."""
    own = own_brand(website)
    you = latest_own_audit(website)
    brands = competitor_brands(website)
    audits = latest_by_apex(website, [b.apex for b in brands]) if brands else {}
    return {
        "bots": bot_catalog(),
        "checks": checks_catalog(),
        "you": audit_payload(you, own) if you is not None else None,
        "competitors": [audit_payload(audits[b.apex], b) for b in brands if b.apex in audits],
        **runs.run_state(website, KEY),
    }


def latest_own_pages(website) -> list[dict]:
    """``[{url, final_url, title, status_code, passed, total}]`` of the
    latest own audit ([] before the first one)."""
    audit = latest_own_audit(website)
    if audit is None:
        return []
    return [
        {"url": row.url, "final_url": row.final_url or row.url, "title": row.title,
         "status_code": row.status_code, "passed": row.passed, "total": row.total}
        for row in audit.pages.order_by("fetched_at", "id")
    ]


KIND = runs.Kind(
    key=KEY, label="Crawler audit", queue="integrations", validate=validate,
    estimate=estimate, choices=choices, plan_items=plan_items, execute=execute,
)
runs.register(KIND)
