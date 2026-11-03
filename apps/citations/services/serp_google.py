"""Google results for Brand Research, the search-gap rank checks and the
agent's research, via SerpAPI or ScraperAPI's structured Google search.

The scan's other discovery lane (``web_search`` / Perplexity) returns an
AI-era index: good recall on things Google buries, but the response carries
nothing beyond rank/url/title/snippet. Google's SERP has structure the
pipeline never saw — the forum block, People Also Ask, related searches, the
AI Overview, the knowledge panel — and that structure is most of what makes
a Brand Research scan more than a manual Google session.

One request per scan. Everything is best-effort: a failure here degrades the
scan to Perplexity-only discovery rather than failing it, so every entry
point returns a well-formed empty payload instead of raising.

Returns::

    {"configured": bool,
     "provider": "serpapi" | "scraperapi" | "",
     "error": str,                     # "" when fine
     "organic":     [{rank, url, domain, title, snippet, date}],
     "discussions": [{rank, url, domain, title, snippet, source_label,
                      comment_count, date}],
     "questions":   [{question, snippet, url, domain}],
     "related_searches": [str],
     "ai_overview": {"text": str, "references": [{url, domain, title}]},
     "ai_overview_deferred": bool,     # shown, but its content was not read
     "knowledge_graph": {"title", "type", "website", "description", "thumbnail"},
     "geo": {"country", "method", "proxy_country", "label"} | None}

Country targeting (``geo_plan``): Google's own country code for the United
Kingdom is ``uk``, not ISO's ``gb``, for both SerpAPI ``gl`` and ScraperAPI
``country_code``. ScraperAPI connects from the country only when the plan
supports it (SCRAPERAPI_GEO_COUNTRIES); otherwise it connects from the US
and sets Google's country (``gl``). SerpAPI always sets ``gl`` and the
country's Google domain.
"""

from __future__ import annotations

import logging
import re
from decimal import Decimal
from urllib.parse import parse_qs, urlsplit

from django.conf import settings

from apps.citations.services.url_normalizer import normalize_url

logger = logging.getLogger("apps")

SEARCH_ENDPOINT = "https://serpapi.com/search.json"
SCRAPERAPI_ENDPOINT = "https://api.scraperapi.com/structured/google/search"
DEFAULT_MAX_RESULTS = 10
TIMEOUT = 15.0
# ScraperAPI fetches and renders Google before parsing it, so one search
# takes far longer than a SerpAPI lookup.
SCRAPERAPI_TIMEOUT = 70.0

PROVIDER_SERPAPI = "serpapi"
PROVIDER_SCRAPERAPI = "scraperapi"

# Google's own click redirects (/goto?url=<opaque token>, /url?q=<target>)
# and the site at the start of a result's breadcrumb ("https://x.com › a").
_GOOGLE_REDIRECT = re.compile(r"^https?://(?:www\.)?google\.[a-z.]+/(?:goto|url)\b", re.IGNORECASE)
_BREADCRUMB_SITE = re.compile(r"^(https?://[^\s\u203a>]+)")

# Caps. The SERP can return a lot of PAA/related-search chaff; past these
# counts it stops informing the graph and starts crowding it.
MAX_ORGANIC = 20
MAX_DISCUSSIONS = 10
MAX_QUESTIONS = 8
MAX_RELATED = 8
MAX_AI_REFERENCES = 10

# ISO 3166-1 -> Google's gl and ScraperAPI's country_code, where they differ.
GOOGLE_COUNTRY = {"gb": "uk"}
# ISO form of ScraperAPI's documented country list (its "uk" is ISO "gb").
SCRAPERAPI_COUNTRIES = frozenset("""
    us au ae ar at bd be bg br ca ch cl cn co cy cz de dk ec ee eg es fi fr gb gr hk hr hu id ie
    il in is it jo jp ke kr li lt lv mt mx my ng nl no nz pa pe ph pk pl pt ro ru sa se sg si sk
    th tr tw ua ve vn za
""".split())
# Google domains ScraperAPI documents a ``tld`` for. A foreign domain needs
# both tld and country_code, so it is only sent when connecting from there.
SCRAPERAPI_TLDS = {
    "gb": "co.uk", "ca": "ca", "de": "de", "es": "es", "fr": "fr", "it": "it", "jp": "co.jp",
    "in": "in", "cn": "cn", "sg": "com.sg", "mx": "com.mx", "ae": "ae", "br": "com.br", "nl": "nl",
    "au": "com.au", "tr": "com.tr", "sa": "sa", "se": "se", "pl": "pl",
}
# SerpAPI google_domain per country (google.com, the default, is left out).
GOOGLE_DOMAINS = {
    "gb": "google.co.uk", "ca": "google.ca", "au": "google.com.au", "de": "google.de",
    "fr": "google.fr", "es": "google.es", "it": "google.it", "nl": "google.nl", "se": "google.se",
    "pl": "google.pl", "in": "google.co.in", "jp": "google.co.jp", "br": "google.com.br",
    "mx": "google.com.mx", "sg": "google.com.sg", "ae": "google.ae", "sa": "google.com.sa",
    "tr": "google.com.tr", "ie": "google.ie", "nz": "google.co.nz", "za": "google.co.za",
    "ch": "google.ch", "at": "google.at", "be": "google.be", "pt": "google.pt", "no": "google.no",
    "dk": "google.dk", "fi": "google.fi", "ar": "google.com.ar", "cl": "google.cl",
    "co": "google.com.co", "pe": "google.com.pe", "kr": "google.co.kr", "tw": "google.com.tw",
    "hk": "google.com.hk", "id": "google.co.id", "ph": "google.com.ph", "my": "google.com.my",
    "th": "google.co.th", "vn": "google.com.vn", "il": "google.co.il", "gr": "google.gr",
    "cz": "google.cz", "ro": "google.ro", "hu": "google.hu", "ua": "google.com.ua",
    "eg": "google.com.eg", "ng": "google.com.ng", "ke": "google.co.ke", "pk": "google.com.pk",
    "bd": "google.com.bd",
}
# How a search was localized (contract section 1c).
METHOD_PROXY, METHOD_GOOGLE_SETTING, METHOD_DEFAULT = "proxy", "google_setting", "default"


def provider() -> str:
    """The Google results provider in use, or "" when neither has a key.

    SERP_PROVIDER pins one ("serpapi" or "scraperapi"). Otherwise SerpAPI
    wins when both keys are set: it also returns the AI Overview text and
    the knowledge panel, which ScraperAPI does not.
    """
    has_key = {
        PROVIDER_SERPAPI: bool(getattr(settings, "SERPAPI_KEY", "")),
        PROVIDER_SCRAPERAPI: bool(getattr(settings, "SCRAPERAPI_KEY", "")),
    }
    pinned = (getattr(settings, "SERP_PROVIDER", "") or "").strip().lower()
    if pinned in has_key:
        return pinned if has_key[pinned] else ""
    for name in (PROVIDER_SERPAPI, PROVIDER_SCRAPERAPI):
        if has_key[name]:
            return name
    return ""


def is_configured() -> bool:
    return bool(provider())


def _provider_in_use() -> str:
    # geo_plan's ``provider`` argument shadows the function inside it.
    return provider()


def cost_per_search_usd(which: str | None = None) -> Decimal:
    """What one search costs with ``which`` provider, by default the one in
    use (0 when none is set)."""
    setting = {
        PROVIDER_SERPAPI: "SERPAPI_COST_PER_SEARCH_USD",
        PROVIDER_SCRAPERAPI: "SCRAPERAPI_COST_PER_SEARCH_USD",
    }.get(provider() if which is None else which)
    if not setting:
        return Decimal("0")
    return Decimal(str(getattr(settings, setting, 0) or 0))


# -- country targeting --------------------------------------------------------


def iso_country(code) -> str:
    """Lower-case ISO-2 code for a country, "" for global, empty or invalid.

    Accepts any case and spacing ("GB", " gb ") and Google's "uk".
    """
    value = str(code or "").strip().lower()
    if len(value) != 2 or not (value.isascii() and value.isalpha()):
        return ""
    return "gb" if value == "uk" else value


def google_code(iso: str) -> str:
    """Google's gl (and ScraperAPI's country_code) for an ISO code."""
    return GOOGLE_COUNTRY.get(iso, iso)


def scraperapi_geo_countries() -> frozenset:
    """Countries the ScraperAPI plan can connect from.

    SCRAPERAPI_GEO_COUNTRIES is "all" (plans with country targeting) or a
    comma or space separated list. The US is always included: every plan
    searches from the US, which is how the default search has always run.
    """
    raw = str(getattr(settings, "SCRAPERAPI_GEO_COUNTRIES", "us") or "").strip().lower()
    if raw == "all":
        return SCRAPERAPI_COUNTRIES
    codes = {iso_country(part) for part in raw.replace(",", " ").split()}
    return frozenset((codes & SCRAPERAPI_COUNTRIES) | {"us"})


def geo_label(method: str, country: str, provider: str) -> str:
    """How a Google search was localized, in words (contract section 1c)."""
    cc = (country or "").upper()
    scraper = provider == PROVIDER_SCRAPERAPI
    if method == METHOD_PROXY:
        return f"Searched from {cc}"
    if method == METHOD_GOOGLE_SETTING:
        return f"Google set to {cc}, US connection" if scraper else f"Google set to {cc}"
    return "Searched from US (default)" if scraper else "Google default (US)"


def geo_plan(country: str, provider: str | None = None) -> dict:
    """How a search for ``country`` runs with ``provider`` (default: the one
    in use): ``{country, method, proxy_country, params, label}``.

    ``params`` are the provider's request parameters for the country and
    ``proxy_country`` the ISO code the request connects from ("" when the
    provider does not say, as with SerpAPI).
    """
    which = _provider_in_use() if provider is None else provider
    iso = iso_country(country)
    if which == PROVIDER_SCRAPERAPI:
        if not iso:
            method, proxy_country, params = METHOD_DEFAULT, "us", {"country_code": "us"}
        elif iso in scraperapi_geo_countries():
            method, proxy_country = METHOD_PROXY, iso
            params = {"country_code": google_code(iso)}
            if iso != "us":
                params["gl"] = google_code(iso)
                if iso in SCRAPERAPI_TLDS:
                    params["tld"] = SCRAPERAPI_TLDS[iso]
        else:
            method, proxy_country = METHOD_GOOGLE_SETTING, "us"
            params = {"country_code": "us", "gl": google_code(iso)}
    elif not iso:
        method, proxy_country, params = METHOD_DEFAULT, "", {}
    else:
        method, proxy_country = METHOD_GOOGLE_SETTING, ""
        params = {"gl": google_code(iso)}
        if iso in GOOGLE_DOMAINS:
            params["google_domain"] = GOOGLE_DOMAINS[iso]
    return {
        "country": iso,
        "method": method,
        "proxy_country": proxy_country,
        "params": params,
        "label": geo_label(method, iso, which),
    }


def _geo(plan: dict) -> dict:
    return {key: plan[key] for key in ("country", "method", "proxy_country", "label")}


def _empty(error: str = "", configured: bool = True, provider: str = "",
           geo: dict | None = None) -> dict:
    return {
        "configured": configured,
        "provider": provider,
        "error": error,
        "organic": [],
        "discussions": [],
        "questions": [],
        "related_searches": [],
        "ai_overview": {},
        "ai_overview_deferred": False,
        "knowledge_graph": {},
        "geo": geo,
    }


def _clean(value, limit: int = 500) -> str:
    return str(value or "").strip()[:limit]


def _host_of(url: str) -> str:
    """Normalized display host, or "" if the URL is unusable.

    Routing every URL through normalize_url also rejects non-http(s)
    schemes, which matters because these end up as clickable links.
    """
    _, host, _ = normalize_url(url)
    return host[4:] if host.startswith("www.") else host


def _parse_comment_count(value) -> int:
    """SerpAPI reports thread size inconsistently: sometimes an int, often a
    string like "42 comments" or "1.2K answers". Pull out what we can."""
    if isinstance(value, int):
        return max(0, value)
    text = str(value or "").strip().lower()
    if not text:
        return 0
    m = re.search(r"([\d.,]+)\s*([km])?", text)
    if not m:
        return 0
    try:
        number = float(m.group(1).replace(",", ""))
    except ValueError:
        return 0
    suffix = m.group(2)
    if suffix == "k":
        number *= 1_000
    elif suffix == "m":
        number *= 1_000_000
    return int(number)


def _redacted(error: Exception, key: str) -> str:
    """An exception message without the API key (it rides in the URL)."""
    text = str(error)
    return text.replace(key, "***") if key else text


def search(
    query: str,
    *,
    max_results: int = DEFAULT_MAX_RESULTS,
    timeout: float | None = None,
    start: int = 0,
    gl: str = "",
    hl: str = "",
    country: str | None = None,
) -> dict:
    """Run one Google search and parse every block we use.

    The provider is SerpAPI or ScraperAPI (see ``provider()``); both come
    back in the same shape, with ``provider`` naming the one used. A
    ``timeout`` of None means the provider's own default. ``start`` pages
    through results (Google serves 10 per page since it
    stopped honouring ``num=100``); ``hl`` sets the interface language
    (SerpAPI only). ``country`` (ISO-2, "" for global) decides how the search
    is localized (``geo_plan``); without it ``gl`` does, read as an ISO code.
    Brand Research uses none of them: one first-page call. The search-gap
    rank check walks pages until it finds the site.

    Never raises. An unconfigured key, a network error, a non-200 or a
    non-JSON body all come back as an empty payload with ``error`` set.
    """
    query = (query or "").strip()
    if not query:
        return _empty("empty_query")
    which = provider()
    if not which:
        return _empty("serpapi_not_configured", configured=False)
    plan = geo_plan(gl if country is None else country, which)
    geo = _geo(plan)

    try:
        import requests
    except ImportError:  # pragma: no cover - requests is a hard dependency
        return _empty("requests_unavailable", provider=which, geo=geo)

    if which == PROVIDER_SCRAPERAPI:
        endpoint, key = SCRAPERAPI_ENDPOINT, settings.SCRAPERAPI_KEY
        # The structured endpoint takes the country, not the interface
        # language, and always returns one page of results. Without a
        # country ScraperAPI picks a proxy anywhere, and Google then answers
        # for that country and language (dates came back in Bengali), so
        # "global" searches as the US, as SerpAPI does by default.
        params = {
            "api_key": key,
            "query": query[:500],
            **plan["params"],
        }
        timeout = SCRAPERAPI_TIMEOUT if timeout is None else timeout
    else:
        endpoint, key = SEARCH_ENDPOINT, settings.SERPAPI_KEY
        params = {
            "q": query[:500],
            "engine": "google",
            "num": str(max(5, min(int(max_results), MAX_ORGANIC))),
            "api_key": key,
            **plan["params"],
        }
        if hl:
            params["hl"] = hl.strip().lower()[:5]
        timeout = TIMEOUT if timeout is None else timeout
    if start:
        params["start"] = str(max(0, int(start)))

    try:
        resp = requests.get(endpoint, params=params, timeout=timeout)
    except Exception as exc:
        logger.warning("serp_google (%s) network error for %r: %s",
                       which, query, _redacted(exc, key))
        return _empty("network", provider=which, geo=geo)

    if resp.status_code != 200:
        logger.warning("serp_google (%s) %r -> HTTP %s", which, query, resp.status_code)
        return _empty(f"http_{resp.status_code}", provider=which, geo=geo)

    try:
        data = resp.json()
    except Exception:
        logger.warning("serp_google (%s) %r -> non-JSON body", which, query)
        return _empty("decode", provider=which, geo=geo)

    if not isinstance(data, dict):
        return _empty("decode", provider=which, geo=geo)

    # Google's first page for a real query always lists results. An empty one
    # means the provider failed to read the page (a block or a layout it could
    # not parse); reading it as "no results" would claim the brand is not
    # ranked when nothing was measured. A later page may be empty: Google ran
    # out of results.
    if not start and not data.get("organic_results"):
        logger.warning("serp_google (%s) %r -> empty first page", which, query)
        return _empty("empty_results", provider=which, geo=geo)

    payload = _empty(provider=which, geo=geo)
    payload["organic"] = _parse_organic(
        data, max_results, start=start, page_relative=which == PROVIDER_SCRAPERAPI,
    )
    payload["discussions"] = _parse_discussions(data)
    payload["questions"] = _parse_questions(data)
    payload["related_searches"] = _parse_related_searches(data)
    if which == PROVIDER_SCRAPERAPI:
        # ScraperAPI marks where Google showed an AI Overview (``ai_box``)
        # but sends neither its text nor its sources, so it is reported as
        # shown and unread, like SerpAPI's page_token case below.
        block = data.get("ai_box")
        payload["ai_overview_deferred"] = isinstance(block, dict) and bool(block)
        return payload
    payload["ai_overview"] = _parse_ai_overview(data)
    # Google showed an AI Overview whose content did not come with the page
    # (SerpAPI sent only a page_token / serpapi_link for it). ``ai_overview``
    # stays {}: nothing was read, so nothing about it is known.
    block = data.get("ai_overview")
    payload["ai_overview_deferred"] = (
        isinstance(block, dict) and bool(block) and not payload["ai_overview"]
    )
    payload["knowledge_graph"] = _parse_knowledge_graph(data)
    return payload


# -- block parsers ------------------------------------------------------------
# Each is defensive about shape: SerpAPI's schema varies by query and these
# blocks are absent far more often than they are present.


def _scraperapi_link(item: dict) -> tuple[str, bool]:
    """(url, site_only) for one ScraperAPI organic row.

    For many results ScraperAPI hands back Google's click redirect
    (google.com/goto?url=<opaque token>) instead of the page. A /url?q=
    redirect still names its target; a /goto token does not, and then the
    page's site survives only in the breadcrumb (``displayed_link``), so
    the row keeps that site's address: right for ranking, not the exact
    page. A breadcrumb that names no site ("40+ answers") gives "".
    """
    link = _clean(item.get("link"), 2000)
    if link and not _GOOGLE_REDIRECT.match(link):
        return link, False
    if link:
        params = parse_qs(urlsplit(link).query)
        for key in ("q", "url"):
            target = (params.get(key) or [""])[0]
            if target.startswith(("http://", "https://")):
                return _clean(target, 2000), False
    match = _BREADCRUMB_SITE.match(str(item.get("displayed_link") or "").strip())
    return (match.group(1).rstrip("/") + "/", True) if match else ("", False)


def _parse_organic(data: dict, max_results: int, *, start: int = 0,
                   page_relative: bool = False) -> list[dict]:
    rows = []
    for index, item in enumerate((data.get("organic_results") or [])[:MAX_ORGANIC]):
        if not isinstance(item, dict):
            continue
        if page_relative:
            url, site_only = _scraperapi_link(item)
        else:
            url, site_only = _clean(item.get("link"), 2000), False
        host = _host_of(url)
        if not host:
            continue
        # SerpAPI's own position is absolute across pages (11-20 on page
        # two); ``rank`` stays the 1-based index among the rows kept here.
        # ScraperAPI numbers each page on its own, and not always from 1,
        # so there the position comes from ``start`` and the row's place.
        position = start + index + 1 if page_relative else item.get("position")
        rows.append({
            "rank": len(rows) + 1,
            "position": position if isinstance(position, int) and position > 0 else None,
            "url": url,
            "domain": host,
            # True when only the result's site is known (see _scraperapi_link).
            "site_only": site_only,
            "breadcrumb": _clean(item.get("displayed_link"), 300),
            "title": _clean(item.get("title")),
            "snippet": _clean(item.get("snippet"), 1000),
            "date": _clean(item.get("date"), 40),
        })
        if len(rows) >= max_results:
            break
    return rows


def _parse_discussions(data: dict) -> list[dict]:
    """Google's forum/Reddit block.

    This is the highest-value block for Brand Research: Google has already
    decided which community threads answer the query, and it hands over the
    comment count, which is the best available proxy for whether a thread is
    worth joining.
    """
    raw = data.get("discussions_and_forums") or data.get("discussions") or []
    rows = []
    for item in raw[:MAX_DISCUSSIONS]:
        if not isinstance(item, dict):
            continue
        url = _clean(item.get("link"), 2000)
        host = _host_of(url)
        if not host:
            continue
        extensions = item.get("extensions") or {}
        comment_count = _parse_comment_count(
            item.get("comment_count")
            or item.get("comments")
            or (extensions.get("comments") if isinstance(extensions, dict) else None)
        )
        rows.append({
            "rank": len(rows) + 1,
            "url": url,
            "domain": host,
            "title": _clean(item.get("title")),
            "snippet": _clean(item.get("snippet") or item.get("answer"), 1000),
            "source_label": _clean(item.get("source") or item.get("displayed_link"), 120),
            "comment_count": comment_count,
            "date": _clean(item.get("date"), 40),
        })
    return rows


def _parse_questions(data: dict) -> list[dict]:
    """People Also Ask. These are literal questions real users type, so they
    double as content opportunities: a question with no good answer ranking
    is a gap the user can fill."""
    rows = []
    raw = data.get("related_questions") or data.get("people_also_ask") or []
    for item in raw[:MAX_QUESTIONS]:
        if not isinstance(item, dict):
            continue
        question = _clean(item.get("question"), 300)
        if not question:
            continue
        url = _clean(item.get("link"), 2000)
        rows.append({
            "question": question,
            "snippet": _clean(item.get("snippet"), 600),
            "url": url,
            "domain": _host_of(url),
        })
    return rows


def _parse_related_searches(data: dict) -> list[str]:
    out = []
    seen = set()
    for item in (data.get("related_searches") or [])[:MAX_RELATED * 2]:
        query = ""
        if isinstance(item, dict):
            query = _clean(item.get("query") or item.get("name"), 200)
        elif isinstance(item, str):
            query = _clean(item, 200)
        fold = query.lower()
        if query and fold not in seen:
            seen.add(fold)
            out.append(query)
        if len(out) >= MAX_RELATED:
            break
    return out


def _parse_ai_overview(data: dict) -> dict:
    """Google's own generated answer, plus what it cited.

    Treated as a sixth AI engine downstream — it is a model recommending
    brands to the user's customers, which is exactly what the engine lane
    tracks. SerpAPI sometimes returns only a `page_token` here (the overview
    needs a second billed request); we deliberately do not chase it, and
    search() reports it as ``ai_overview_deferred``.
    """
    block = data.get("ai_overview")
    if not isinstance(block, dict):
        return {}

    chunks = []
    for part in block.get("text_blocks") or []:
        if not isinstance(part, dict):
            continue
        if part.get("snippet"):
            chunks.append(_clean(part["snippet"], 2000))
        for nested in part.get("list") or []:
            if isinstance(nested, dict) and nested.get("snippet"):
                chunks.append(_clean(nested["snippet"], 2000))

    references = []
    for ref in (block.get("references") or [])[:MAX_AI_REFERENCES]:
        if not isinstance(ref, dict):
            continue
        url = _clean(ref.get("link"), 2000)
        host = _host_of(url)
        if not host:
            continue
        references.append({
            "url": url,
            "domain": host,
            "title": _clean(ref.get("title")),
        })

    text = "\n\n".join(c for c in chunks if c)[:8000]
    if not text and not references:
        return {}
    return {"text": text, "references": references}


def _parse_knowledge_graph(data: dict) -> dict:
    """The right-hand brand panel. Gives the detail panel a canonical site and
    description without spending a Perplexity brand-lookup call per click."""
    block = data.get("knowledge_graph")
    if not isinstance(block, dict):
        return {}
    website = _clean(block.get("website"), 2000)
    out = {
        "title": _clean(block.get("title"), 200),
        "type": _clean(block.get("type"), 120),
        "website": website if _host_of(website) else "",
        "description": _clean(block.get("description"), 1000),
        "thumbnail": _clean(block.get("thumbnail"), 2000),
    }
    return out if any(out.values()) else {}


# -- discovery merge ----------------------------------------------------------


def merge_discovery(perplexity_rows: list[dict], google_rows: list[dict], *, limit: int) -> list[dict]:
    """Union two ranked result lists into one, deduped by normalized URL.

    A URL both indexes surface is a stronger signal than one only a single
    index found, so agreement is the primary sort key and best rank the
    tiebreaker. Each surviving row carries ``discovered_by`` so the UI can
    show where it came from.
    """
    merged: dict[str, dict] = {}
    order: list[str] = []

    def absorb(rows: list[dict], source: str) -> None:
        for row in rows or []:
            url = (row.get("url") or "").strip()
            normalized, host, _ = normalize_url(url)
            if not normalized or not host:
                continue
            existing = merged.get(normalized)
            if existing is None:
                entry = dict(row)
                entry["url"] = url
                entry["domain"] = row.get("domain") or (
                    host[4:] if host.startswith("www.") else host
                )
                entry["discovered_by"] = [source]
                merged[normalized] = entry
                order.append(normalized)
                continue
            if source not in existing["discovered_by"]:
                existing["discovered_by"].append(source)
            # Keep the better (lower) rank, and backfill any field the
            # first index left empty.
            if (row.get("rank") or 999) < (existing.get("rank") or 999):
                existing["rank"] = row["rank"]
            for field in ("title", "snippet", "date", "last_updated"):
                if not existing.get(field) and row.get(field):
                    existing[field] = row[field]

    absorb(perplexity_rows, "perplexity")
    absorb(google_rows, "google")

    rows = [merged[key] for key in order]
    rows.sort(key=lambda r: (-len(r.get("discovered_by") or []), r.get("rank") or 999))
    rows = rows[:limit]
    # Rank is positional in the merged list from here on; downstream code
    # (rank weighting, unique_together on (scan, rank)) requires 1..N dense.
    for index, row in enumerate(rows, start=1):
        row["rank"] = index
    return rows
