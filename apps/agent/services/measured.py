"""What Cansee measured, as the writing agents read it (design/p2_backend.md
12c). Every function returns empty data when the insights app is off or a
read fails, and logs; none raises. A source with no data adds nothing,
never a zero (contract 12e.6).

The insights services are imported inside the functions, so the agent app
loads without them.
"""
from __future__ import annotations

import logging

from django.apps import apps as django_apps
from django.conf import settings

from apps.agent.subagents import sourcing

logger = logging.getLogger("apps")

MAX_SIGNALS = 20
FANOUT_QUERIES = 10
THEMES = 10
CONTRADICTED = 5
PROMPTS = 10


def _enabled() -> bool:
    return (bool(getattr(settings, "INSIGHTS_ENABLED", False))
            and django_apps.is_installed("apps.insights"))


def engine_labels() -> dict[str, str]:
    """Public engine key -> its label ("chatgpt" -> "ChatGPT")."""
    from apps.citations.services.url_analytics import model_key
    from apps.llm_ranking.services.answer_window import ENGINE_ORDER
    from apps.prompt_library.services.prompt_stats import MODEL_LABELS

    return {model_key(p): MODEL_LABELS.get(p, p) for p in ENGINE_ORDER}


def and_list(items) -> str:
    items = [str(i) for i in items if i]
    if len(items) <= 2:
        return " and ".join(items)
    return f"{', '.join(items[:-1])} and {items[-1]}"


def _date(value) -> str:
    """The date part of an ISO timestamp ("2026-11-04")."""
    return str(value or "")[:10]


def _cited_prompts(website, url) -> list[str]:
    """Tracked prompts whose answers in the last 30 days cite this page."""
    from django.utils import timezone

    from apps.citations.models import Citation
    from apps.insights.services import pages
    from apps.llm_ranking.services import answer_window
    from apps.prompt_library.models import BrandPrompt

    host = pages.site_host(website)
    key = pages.page_key(url, host)
    if key is None:
        return []
    window = answer_window.Window("30d", "day", timezone.now().date())
    brand_prompts = list(BrandPrompt.objects.filter(website=website, is_archived=False)
                         .select_related("prompt").order_by("created_at"))
    prompt_of = {result.id: str(bp.prompt_id) for bp, result in answer_window.tracked_answers(
        website, brand_prompts, since=window.start_at)}
    ids = list(prompt_of)
    found: list[str] = []
    for start in range(0, len(ids), 1000):
        rows = Citation.objects.filter(result_id__in=ids[start:start + 1000]).values_list(
            "result_id", "url")
        for result_id, cited in rows:
            if pages.page_key(cited, host) == key and prompt_of[result_id] not in found:
                found.append(prompt_of[result_id])
    return found


def page_signals(website, url) -> list[dict]:
    """``[{"id": "s{n}", "kind", "text"}]`` for one page of the site: its
    Google positions, citations, the searches engines ran for the prompts
    it ranks or is cited for, AI visits, crawler requests and the latest
    audit; at most MAX_SIGNALS lines, each naming its date or period."""
    if not _enabled():
        return []
    try:
        return _page_signals(website, url)
    except Exception:
        logger.warning("agent: the page signals of a page could not be read", exc_info=True)
        return []


def _page_signals(website, url) -> list[dict]:
    from apps.insights.services import fanouts, pages, site_audit

    labels = engine_labels()
    lines: list[tuple[str, str]] = []
    row = pages.page_row(website, url, period="30d")
    prompt_ids: list[str] = []
    fanout_lines: list[tuple[str, str]] = []
    later: list[tuple[str, str]] = []
    if row:
        google = row.get("google") or {}
        for prompt in google.get("prompts") or []:
            if prompt.get("position") is None:
                continue
            prompt_ids.append(str(prompt["prompt_id"]))
            text = f'Google position {prompt["position"]} for "{prompt["text"]}"'
            if prompt.get("checked_at"):
                text += f" (checked {_date(prompt['checked_at'])})"
            lines.append(("google", text))
        citations = row.get("citations") or {}
        if citations.get("citations"):
            engines = ", ".join(labels.get(e, e) for e in citations.get("engines") or [])
            if citations.get("cited_in_text"):
                lines.append(("citations",
                              f"Cited {citations['citations']} times in {citations['answers']} AI "
                              f"answers ({engines}) in the last 30 days; "
                              f"{citations['cited_in_text']} in the answer text"))
            else:
                lines.append(("retrieved_not_cited",
                              f"Retrieved by AI engines {citations['citations']} times in the "
                              "last 30 days but never cited in the answer text"))
        visits = row.get("ai_visits") or {}
        if visits.get("sessions"):
            source = ((row.get("sources") or {}).get("ai_visits") or {}).get("source_label") or ""
            later.append(("ai_visits", f"{visits['sessions']} visits from AI assistants in the "
                                       f"last 30 days ({source})"))
        crawler = row.get("crawler_requests") or {}
        if crawler.get("requests"):
            later.append(("crawler", f"{crawler['requests']} AI crawler requests in the last 7 "
                                     "days (Cloudflare)"))
        audit = row.get("audit") or {}
        if audit.get("passed") is not None:
            names = {key: label for key, label, _rule in site_audit.SITE_AUDIT_CHECKS}
            failed = audit.get("failed") or []
            text = (f"Audit {_date(audit.get('audited_at'))}: {audit['passed']} of "
                    f"{audit['total']} checks pass; failing: "
                    f"{', '.join(names.get(k, k) for k in failed) or 'none'}")
            if "text_without_js" in failed and audit.get("words") is not None:
                text += f"; {audit['words']} words readable without JavaScript"
            later.append(("audit", text))
    for prompt_id in _cited_prompts(website, url):
        if prompt_id not in prompt_ids:
            prompt_ids.append(prompt_id)
    for query in fanouts.queries_for_prompts(website, prompt_ids, period="30d",
                                             limit=FANOUT_QUERIES):
        engines = and_list(labels.get(e, e) for e in query.get("engines") or [])
        seen = _date(query["last_seen_at"])
        fanout_lines.append(("fanout", f'{engines} searched "{query["query"]}" while answering '
                                       f'"{query["prompt_text"]}" ({seen})'))
    ordered = [*lines, *fanout_lines, *later][:MAX_SIGNALS]
    return [{"id": f"s{n}", "kind": kind, "text": text}
            for n, (kind, text) in enumerate(ordered, start=1)]


def competitor_benchmark(website, competitor: dict) -> dict | None:
    """The 30-day comparison with one tracked competitor (contract 4a), cut
    to what a comparison page needs to choose its questions; None when
    nothing was measured."""
    if not _enabled():
        return None
    try:
        from apps.insights.services import competitors

        payload = competitors.build(website, period="30d", interval="week", engine=None)
    except Exception:
        logger.warning("agent: the competitor benchmark could not be read", exc_info=True)
        return None
    try:
        return _benchmark(payload, competitor)
    except Exception:
        logger.warning("agent: the competitor benchmark had an unexpected shape", exc_info=True)
        return None


def _benchmark(payload: dict, competitor: dict) -> dict | None:
    apex = sourcing.apex_of(competitor.get("domain") or "")
    key = next((b["key"] for b in payload.get("brands") or []
                if not b.get("is_you") and sourcing.apex_of(b.get("domain") or "") == apex), None)
    if key is None:
        return None

    def pair(block: dict | None) -> dict | None:
        if not block:
            return None
        return {"you": block.get("you"), "competitor": block.get(key)}

    ai = payload.get("ai") or {}
    brands = ai.get("brands") or {}
    citations = payload.get("citations") or {}
    google = payload.get("google") or {}
    site = payload.get("site") or {}
    prompts = []
    for row in (payload.get("by_prompt") or [])[:PROMPTS]:
        prompts.append({
            "prompt": row.get("text"),
            "answers": row.get("answers"),
            "named_pct": pair(row.get("ai")),
            "google": pair(row.get("google")) if row.get("google_state") == "ok" else None,
        })
    head = next((h for h in payload.get("head_to_head") or [] if h.get("key") == key), None)
    return {
        "period": "last 30 days",
        "answers": ai.get("answers"),
        "named": {"you": _drop_series(brands.get("you")),
                  "competitor": _drop_series(brands.get(key))},
        "head_to_head": {k: v for k, v in (head or {}).items() if k != "key"} or None,
        "citations": pair(citations.get("brands")) if citations.get("has_data") else None,
        "google": pair(google.get("brands")) if google.get("brands") else None,
        "site_audit": pair(site),
        "prompts": prompts,
    }


def _drop_series(block):
    return {k: v for k, v in block.items() if k != "series"} if isinstance(block, dict) else None


def correction_claim(website, claim_key: str) -> dict | None:
    """One Fact check claim group of the last 90 days (contract 3d, plus
    ``cited_urls`` of third-party pages), or None."""
    if not _enabled():
        return None
    try:
        from apps.insights.services import fact_check

        return fact_check.claim_group(website, claim_key, period="90d")
    except Exception:
        logger.warning("agent: the Fact check claim could not be read", exc_info=True)
        return None


def perception(website) -> dict:
    """How AI engines describe the brand over the last 90 days: the top
    themes with their counts and one quote, and up to five claims the
    brand's evidence contradicts."""
    empty = {"period": "last 90 days", "themes": [], "contradicted": []}
    if not _enabled():
        return empty
    try:
        from apps.insights.services import fact_check, sentiment

        themes = sentiment.build(website, period="90d", interval="week", engine=None,
                                 prompt_id=None)
        claims = fact_check.build(website, period="90d", interval="week", engine=None,
                                  prompt_id=None, verdict="contradicted", topic=None)
    except Exception:
        logger.warning("agent: how the engines describe the brand could not be read",
                       exc_info=True)
        return empty
    coverage = themes.get("coverage") or {}
    return {
        "period": "last 90 days",
        "named_answers": coverage.get("named_answers"),
        "analyzed_answers": coverage.get("analyzed"),
        "themes": [
            {"label": t.get("label"), "tone": t.get("tone"), "answers": t.get("answers"),
             "engines": t.get("engines") or [],
             "quote": ((t.get("quotes") or [{}])[0] or {}).get("quote") or ""}
            for t in (themes.get("themes") or [])[:THEMES]
        ],
        "contradicted": [
            {"claim": c.get("text"), "answers": c.get("answers"), "engines": c.get("engines") or [],
             "evidence": c.get("evidence"), "note": c.get("note") or ""}
            for c in (claims.get("claims") or []) if c.get("verdict") == "contradicted"
        ][:CONTRADICTED],
    }


def site_pages(website, *, limit: int = 60, home=None) -> list[dict]:
    """``[{"url", "title"}]``: the home page and its links (``home``, a
    SourcePage read with its links), the latest audit's pages, the Pages
    rows and the Offerings pages; the site's own pages only, de-duplicated,
    at most ``limit``. Without the insights app, the audit and Pages parts
    are left out."""
    from apps.agent.services.context import offerings_for_writers, url_key

    found: dict[str, dict] = {}

    def add(url, title="") -> None:
        if not url or not sourcing.same_site(url, website) or len(found) >= limit:
            return
        key = url_key(url)
        if key in found:
            if title and not found[key]["title"]:
                found[key]["title"] = title
            return
        found[key] = {"url": url, "title": (title or "")[:200]}

    if home is not None and home.ok:
        add(home.final_url or home.url, home.title)
        for link in home.links:
            if isinstance(link, dict):
                add(link.get("url") or "", link.get("text") or "")
    if _enabled():
        try:
            from apps.insights.services import pages, site_audit

            for row in site_audit.latest_own_pages(website):
                if row.get("status_code") and 200 <= int(row["status_code"]) < 300:
                    add(row.get("final_url") or row.get("url"), row.get("title") or "")
            for row in pages.build(website, period="30d", filter="all").get("pages") or []:
                add(row.get("url"))
        except Exception:
            logger.warning("agent: the site's known pages could not be read", exc_info=True)
    for offering in offerings_for_writers(website):
        add(offering["url"], offering["name"])
    return list(found.values())
