"""The project's own pages, joined across every source (contract 6).

A page is a URL on the project's host (``www.`` ignored; path without
query, fragment or trailing slash). Five sources say something about it:
Google (your URL and your page-one rows on each tracked prompt's latest
check), the window's AI citations on your site, AI visits (wave 1 landing
pages of AI referrals), AI crawler requests (wave 1 Cloudflare paths, the
top 50 of the last 7 days) and the latest crawler audit. A signal is null
when its source cannot be read (the source's state says why); a source
that was read and has nothing for the page gives a measured zero, except
crawler requests, where only the top paths are known (``not_in_top``).
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import timedelta
from urllib.parse import urlsplit

from django.utils import timezone

from apps.citations.services import serp_google
from apps.citations.services.url_analytics import model_key
from apps.insights.services import site_audit
from apps.insights.services.runs import iso
from apps.llm_ranking.services import answer_window
from apps.search_gap.services.google_rank import is_same_site, site_apex
from core.exceptions import CanseeException

GOOGLE_DAYS = 120
MAX_ROWS = 200
CITATION_CHUNK = 1000
FILTERS = (
    "all", "cited", "retrieved_not_cited", "ranking", "ai_visits", "crawled", "failing_checks",
)
OK = "ok"


# -- URLs ----------------------------------------------------------------------


def _bare_host(host: str) -> str:
    host = (host or "").strip().lower().rstrip(".")
    return host[4:] if host.startswith("www.") else host


def _split(url: str, *, bare_host_ok: bool = False):
    url = (url or "").strip()
    if bare_host_ok and url and "://" not in url:
        # A project URL saved without its scheme ("notion.so").
        url = f"https://{url}"
    try:
        parts = urlsplit(url)
        _ = parts.port  # raises ValueError for a bad port
    except ValueError:
        return None
    return parts if parts.scheme.lower() in ("http", "https") and parts.hostname else None


def site_host(website) -> str:
    """The project's host without ``www.`` ("" when its URL is unusable)."""
    parts = _split(website.url, bare_host_ok=True)
    return _bare_host(parts.hostname) if parts else ""


def site_origin(website) -> str:
    """``scheme://host[:port]`` of the project's URL ("" when unusable)."""
    parts = _split(website.url, bare_host_ok=True)
    return f"{parts.scheme.lower()}://{parts.netloc.lower()}" if parts else ""


def clean_path(path: str) -> str:
    """A path as pages key it: no query, fragment or trailing slash; ``/``
    for the root."""
    path = (path or "").split("#", 1)[0].split("?", 1)[0].strip() or "/"
    if not path.startswith("/"):
        path = f"/{path}"
    return path.rstrip("/") or "/"


def page_key(url: str, host: str) -> str | None:
    """``host + path`` of a URL on ``host`` (``www.`` ignored), else None."""
    parts = _split(url)
    if parts is None or not host or _bare_host(parts.hostname) != host:
        return None
    return host + clean_path(parts.path)


# -- signals -------------------------------------------------------------------


@dataclass
class PageSignals:
    """What the sources say about one page of the project's site."""

    key: str
    path: str
    url: str
    # prompt id -> {"prompt_id", "text", "position", "checked_at"} (best position)
    google: dict = field(default_factory=dict)
    citations: int = 0
    cited_in_text: int = 0
    answers: set = field(default_factory=set)
    engines: set = field(default_factory=set)
    sessions: int = 0
    by_assistant: Counter = field(default_factory=Counter)
    requests: int = 0
    audit: object | None = None

    @property
    def best_position(self) -> int | None:
        positions = [p["position"] for p in self.google.values() if p["position"]]
        return min(positions) if positions else None

    def signal_count(self) -> int:
        """Sources with a positive value for this page."""
        return sum((
            self.best_position is not None,
            self.citations > 0,
            self.sessions > 0,
            self.requests > 0,
            self.audit is not None,
        ))


@dataclass
class Collected:
    """Every source read once: their states (contract ``sources``), which
    of them are on, and the pages they name."""

    window: answer_window.Window
    sources: dict
    on: dict
    pages: dict


class _Pages(dict):
    def __init__(self, host: str, origin: str):
        super().__init__()
        self.host, self.origin = host, origin

    def at(self, key: str) -> PageSignals:
        page = self.get(key)
        if page is None:
            path = key[len(self.host):] or "/"
            page = self[key] = PageSignals(key=key, path=path, url=f"{self.origin}{path}")
        return page

    def at_url(self, url: str) -> PageSignals | None:
        key = page_key(url, self.host)
        return self.at(key) if key else None

    def at_path(self, path: str) -> PageSignals:
        return self.at(self.host + clean_path(path))


def _brand_prompts(website) -> list:
    from apps.prompt_library.models import BrandPrompt

    return list(
        BrandPrompt.objects.filter(website=website, is_archived=False)
        .select_related("prompt").order_by("created_at")
    )


def _google(website, brand_prompts, pages: _Pages, apex: str) -> dict:
    from apps.search_gap.models import GoogleRankCheck

    if not serp_google.is_configured():
        return {"state": "not_configured", "checked_prompts": None}
    by_id = {bp.id: bp for bp in brand_prompts}
    latest: dict = {}
    checks = (
        GoogleRankCheck.objects
        .filter(website=website, brand_prompt_id__in=list(by_id), error="",
                checked_at__gte=timezone.now() - timedelta(days=GOOGLE_DAYS))
        .only("brand_prompt_id", "position", "url", "top_results", "checked_at")
        .order_by("-checked_at")
    )
    for check in checks:
        latest.setdefault(check.brand_prompt_id, check)
    if not latest:
        return {"state": "no_checks", "checked_prompts": 0}

    for bp_id, check in latest.items():
        bp = by_id[bp_id]
        rows = [{"url": check.url, "position": check.position}] if check.position else []
        rows += [
            r for r in check.top_results or []
            if isinstance(r, dict) and is_same_site(r.get("domain", ""), apex)
        ]
        for row in rows:
            position = row.get("position")
            page = pages.at_url(row.get("url") or "")
            if page is None or not isinstance(position, int) or position <= 0:
                continue
            seen = page.google.get(bp.prompt_id)
            if seen is None or position < seen["position"]:
                page.google[bp.prompt_id] = {
                    "prompt_id": str(bp.prompt_id), "text": bp.prompt.text,
                    "position": position, "checked_at": iso(check.checked_at),
                }
    return {"state": OK, "checked_prompts": len(latest)}


def _citations(website, brand_prompts, window, pages: _Pages, apex: str) -> dict:
    from apps.citations.models import Citation

    provider_of = {
        result.id: result.provider
        for _bp, result in answer_window.tracked_answers(
            website, brand_prompts, since=window.start_at,
        )
    }
    ids = list(provider_of)
    with_sources: set = set()
    for offset in range(0, len(ids), CITATION_CHUNK):
        rows = (
            Citation.objects.filter(result_id__in=ids[offset:offset + CITATION_CHUNK])
            .values("result_id", "url", "domain", "is_target", "reference_count")
        )
        for row in rows:
            with_sources.add(row["result_id"])
            if not (row["is_target"] or is_same_site(row["domain"], apex)):
                continue
            page = pages.at_url(row["url"])
            if page is None:
                continue
            page.citations += 1
            page.cited_in_text += int((row["reference_count"] or 0) > 0)
            page.answers.add(row["result_id"])
            page.engines.add(provider_of[row["result_id"]])
    return {"state": OK if with_sources else "no_data", "answers_with_sources": len(with_sources)}


def _ai_visits(website, period: str, pages: _Pages) -> dict:
    from apps.web_analytics.services import ai_traffic

    try:
        visits = ai_traffic.ai_landing_pages(website, period=period)
    except Exception:
        visits = {"source": None, "source_label": None, "available": False,
                  "reason": "source_error", "pages": []}
    state = OK if visits.get("available") else visits.get("reason") or "source_error"
    if state == OK:
        for row in visits.get("pages") or []:
            page = pages.at_path(row.get("path") or "/")
            page.sessions += int(row.get("sessions") or 0)
            page.by_assistant.update(row.get("by_assistant") or {})
    return {"state": state, "source": visits.get("source"),
            "source_label": visits.get("source_label")}


def _crawlers(website, pages: _Pages) -> dict:
    from apps.web_analytics.services import ai_crawlers, ai_traffic

    try:
        report = ai_traffic.crawler_paths(website)
    except Exception:
        report = ai_crawlers.unavailable("source_error")
    state = OK if report.get("available") else report.get("reason") or "source_error"
    if state == OK:
        for row in report.get("paths") or []:
            pages.at_path(row.get("path") or "/").requests += int(row.get("requests") or 0)
    return {"state": state, "window_days": report.get("window_days", ai_crawlers.WINDOW_DAYS)}


def _audit(website, pages: _Pages) -> dict:
    audit = site_audit.latest_own_audit(website)
    if audit is None:
        return {"state": "not_run", "audited_at": None}
    for row in audit.pages.all():
        page = pages.at_url(row.url)
        if page is not None and page.audit is None:
            page.audit = row
    return {"state": OK, "audited_at": iso(audit.audited_at)}


def collect(website, *, period: str = "30d") -> Collected:
    """Every source of contract 6 read once for the period."""
    period, interval, _engine, _prompt = answer_window.parse_filters(period=period)
    window = answer_window.Window(period, interval, timezone.now().date())
    pages = _Pages(site_host(website), site_origin(website))
    if not pages.host:
        sources = {
            "google": {"state": "not_configured", "checked_prompts": None},
            "citations": {"state": "no_data", "answers_with_sources": 0},
            "ai_visits": {"state": "no_source", "source": None, "source_label": None},
            "crawlers": {"state": "no_cloudflare", "window_days": 7},
            "audit": {"state": "not_run", "audited_at": None},
        }
        return Collected(window, sources, dict.fromkeys(sources, False), pages)
    apex = site_apex(website.url)
    brand_prompts = _brand_prompts(website)
    sources = {
        "google": _google(website, brand_prompts, pages, apex),
        "citations": _citations(website, brand_prompts, window, pages, apex),
        "ai_visits": _ai_visits(website, period, pages),
        "crawlers": _crawlers(website, pages),
        "audit": _audit(website, pages),
    }
    on = {name: block["state"] == OK for name, block in sources.items()}
    return Collected(window, sources, on, pages)


def known_urls(website, *, period: str = "30d") -> dict:
    """``{key: PageSignals}`` of every page a source names (key =
    ``host_without_www + path``)."""
    return collect(website, period=period).pages


# -- rows ----------------------------------------------------------------------


def _audit_block(row) -> dict:
    checks = row.checks or {}
    return {
        "passed": row.passed,
        "total": row.total,
        "failed": [key for key, _l, _r in site_audit.SITE_AUDIT_CHECKS if checks.get(key) is False],
        "audited_at": iso(row.audit.audited_at),
    }


def _row(page: PageSignals, on: dict, *, detailed: bool = False) -> dict:
    google = None
    if on["google"]:
        prompts = sorted(page.google.values(), key=lambda p: (p["position"], p["text"]))
        if not detailed:
            prompts = [{k: p[k] for k in ("prompt_id", "text", "position")} for p in prompts]
        google = {"best_position": page.best_position, "prompts": prompts}
    citations = None
    if on["citations"]:
        citations = {
            "citations": page.citations,
            "cited_in_text": page.cited_in_text,
            "answers": len(page.answers),
            "engines": [model_key(p) for p in answer_window.ENGINE_ORDER if p in page.engines],
        }
    visits = None
    if on["ai_visits"]:
        visits = {
            "sessions": page.sessions,
            "by_assistant": dict(sorted(page.by_assistant.items(), key=lambda kv: (-kv[1], kv[0]))),
        }
    crawler = None
    if on["crawlers"]:
        crawler = (
            {"requests": page.requests, "state": OK} if page.requests > 0
            else {"requests": None, "state": "not_in_top"}
        )
    audit = None
    if on["audit"]:
        if page.audit is None:
            audit = {"passed": None, "total": None, "state": "not_audited"}
        else:
            audit = _audit_block(page.audit)
            if detailed:
                audit["words"] = page.audit.words
    signals = sum((
        bool(google and google["best_position"] is not None),
        bool(citations and citations["citations"] > 0),
        bool(visits and visits["sessions"] > 0),
        bool(crawler and crawler["requests"]),
        bool(audit and audit["passed"] is not None),
    ))
    return {
        "url": page.url,
        "path": page.path,
        "signals": signals,
        "google": google,
        "citations": citations,
        "ai_visits": visits,
        "crawler_requests": crawler,
        "audit": audit,
    }


def _matches(row: dict, filter_: str) -> bool:
    if filter_ == "all":
        return True
    citations, audit = row["citations"], row["audit"]
    if filter_ == "cited":
        return bool(citations and citations["cited_in_text"] > 0)
    if filter_ == "retrieved_not_cited":
        return bool(citations and citations["citations"] > 0 and citations["cited_in_text"] == 0)
    if filter_ == "ranking":
        return bool(row["google"] and row["google"]["best_position"] is not None)
    if filter_ == "ai_visits":
        return bool(row["ai_visits"] and row["ai_visits"]["sessions"] > 0)
    if filter_ == "crawled":
        return bool(row["crawler_requests"] and row["crawler_requests"]["requests"])
    # failing_checks
    return bool(audit and audit["passed"] is not None and audit["passed"] < audit["total"])


def _totals(rows: list[dict], on: dict) -> dict:
    def count(source: str, filter_: str):
        return sum(1 for row in rows if _matches(row, filter_)) if on[source] else None

    return {
        "pages": len(rows),
        "ranking": count("google", "ranking"),
        "cited": count("citations", "cited"),
        "retrieved_not_cited": count("citations", "retrieved_not_cited"),
        "ai_visits": count("ai_visits", "ai_visits"),
        "crawled": count("crawlers", "crawled"),
        "audited": (
            sum(1 for row in rows if row["audit"] and row["audit"]["passed"] is not None)
            if on["audit"] else None
        ),
    }


def _window(window) -> dict:
    return {
        "period": window.period,
        "interval": window.interval,
        "start": window.start.isoformat(),
        "end": window.end.isoformat(),
        "buckets": [day.isoformat() for day in window.buckets],
    }


def _invalid_filter() -> CanseeException:
    message = "Choose a filter of " + ", ".join(FILTERS[:-1]) + f" or {FILTERS[-1]}."
    return CanseeException(
        message, code="invalid", status_code=400, details={"field": "filter", "message": message},
    )


def build(website, *, period: str = "30d", filter: str = "all") -> dict:  # noqa: A002 - contract name
    """The Pages payload (contract 6)."""
    filter_ = filter or "all"
    if filter_ not in FILTERS:
        raise _invalid_filter()
    collected = collect(website, period=period)
    rows = [_row(page, collected.on) for page in collected.pages.values()]
    shown = [row for row in rows if _matches(row, filter_)]
    shown.sort(key=lambda row: (
        -row["signals"], -((row["citations"] or {}).get("citations") or 0), row["path"],
    ))
    return {
        "window": _window(collected.window),
        "filter": filter_,
        "sources": collected.sources,
        "totals": _totals(rows, collected.on),
        "pages": shown[:MAX_ROWS],
    }


def page_row(website, url: str, *, period: str = "30d") -> dict | None:
    """One page's contract 6 row plus ``sources`` (the source states), or
    None when the URL is not on the project's site or no source names it.
    For the writers' signal lines it also carries each Google prompt's
    ``checked_at`` and the audit's ``words`` (null when the page did not
    load)."""
    key = page_key(url, site_host(website))
    if key is None:
        return None
    collected = collect(website, period=period)
    page = collected.pages.get(key)
    if page is None:
        return None
    return {**_row(page, collected.on, detailed=True), "sources": collected.sources}
