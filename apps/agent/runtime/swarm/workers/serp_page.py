"""serp_page: one Google results page for one prompt (design section 8a).

Searched from the prompt's own country with the same provider, quota and
capture shape as google_search. Version 1 shows a page this chat
captured within the hour again instead of searching (SerpAPI answers a
repeated search from its cache for an hour anyway); re-runs always
search. A network error, a 429 or a 5xx is retried once after a pause.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime

from django.utils import timezone
from django.utils.dateparse import parse_datetime

from apps.agent.runtime.swarm.context import TaskJob, WorkerOutput, failed, skipped
from apps.agent.tools.shaping import clip

logger = logging.getLogger("apps")

RETRY_SECONDS = 3.0
TOP_RESULTS = 5
MAX_REFERENCES = 5
MAX_QUESTIONS = 5
TITLE_CHARS = 120
QUESTION_CHARS = 200
REUSED_NOTE = (
    "Google pages this chat captured within the hour were shown again instead of searched again."
)
# empty_results: the provider read an empty first page, which a second read
# usually fixes.
_RETRY_CODES = ("network", "http_429", "empty_results")


def _retryable(code: str) -> bool:
    return code in _RETRY_CODES or code.startswith("http_5")


def age_text(seconds: float) -> str:
    """How long ago, in words: "less than a minute", "12 minutes"."""
    minutes = int(seconds // 60)
    if minutes < 1:
        return "less than a minute"
    return f"{minutes} minute{'' if minutes == 1 else 's'}"


def _age_seconds(captured_at: str | None) -> float:
    captured = parse_datetime(captured_at or "") if captured_at else None
    if not isinstance(captured, datetime):
        return 0.0
    return max(0.0, (timezone.now() - captured).total_seconds())


def _pause(job: TaskJob, seconds: float) -> None:
    """Wait ``seconds``, ending early (TaskStopped) when the task is stopped."""
    end = time.monotonic() + seconds
    while True:
        job.check()
        left = end - time.monotonic()
        if left <= 0:
            return
        time.sleep(min(0.25, left))


def _search(query: str, page: int, gl: str, hl: str) -> dict:
    from apps.agent.tools.research import PAGE_SIZE
    from apps.citations.services import serp_google

    try:
        return serp_google.search(query, max_results=PAGE_SIZE, start=(page - 1) * PAGE_SIZE,
                                  gl=gl, hl=hl or "en")
    except Exception:
        logger.warning("agent swarm: serp_google.search raised", exc_info=True)
        return {"error": "network"}


def _overview(capture: dict) -> dict | None:
    overview = capture.get("ai_overview")
    if not overview:
        return None
    return {
        "shown": bool(overview.get("shown")),
        "content_captured": overview.get("content_captured"),
        "mentions_you": overview.get("mentions_you"),
        "cites_you": overview.get("cites_you"),
        "references": [
            {"domain": r.get("domain") or "", "url": r.get("url") or ""}
            for r in (overview.get("references") or [])[:MAX_REFERENCES]
        ],
    }


def output(capture: dict, *, reused_seconds: float | None) -> WorkerOutput:
    """The task's result line, detail and pane for one capture."""
    results = capture.get("results") or []
    positions = capture.get("brand_positions") or []
    page = capture.get("page") or 1
    best = next((r for r in results if r.get("is_competitor")), None)
    if positions:
        line = f"You're #{min(positions)}"
    else:
        line = f"Not on page {page}"
        if best is not None:
            line += f" · {best.get('competitor_name') or best.get('domain')} #{best['position']}"
    overview = capture.get("ai_overview") or {}
    if page == 1 and overview.get("shown"):
        line += " · AI Overview names you" if overview.get("mentions_you") else " · AI Overview shown"
    if reused_seconds is not None:
        line += f" · captured {age_text(reused_seconds)} ago"
    if positions and min(positions) <= 3:
        tone = "good"
    elif not positions and best is not None:
        tone = "warn"
    else:
        tone = None
    detail = {
        "query": capture.get("query") or "",
        "page": page,
        "location": capture.get("location") or "",
        "provider": capture.get("provider") or "",
        "captured_at": capture.get("captured_at"),
        "reused": reused_seconds is not None,
        "result_count": len(results),
        "brand_positions": list(positions),
        "top": [
            {
                "position": r.get("position"),
                "title": clip(r.get("title"), TITLE_CHARS),
                "url": r.get("url") or "",
                "domain": r.get("domain") or "",
                "is_yours": bool(r.get("is_yours")),
                "is_competitor": bool(r.get("is_competitor")),
                "competitor_name": r.get("competitor_name"),
            }
            for r in results[:TOP_RESULTS]
        ],
        "ai_overview": _overview(capture),
        "people_also_ask": [clip(q, QUESTION_CHARS)
                            for q in (capture.get("people_also_ask") or [])[:MAX_QUESTIONS]],
    }
    return WorkerOutput(status="done", result=line, tone=tone, detail=detail,
                        pane={"kind": "serp", "tab_id": f"serp:{capture['capture_id']}"})


def run(job: TaskJob) -> WorkerOutput:
    from apps.agent.tools.research import (
        _capture_id,
        _recent_captures,
        _serp_error,
        _serp_quota,
        build_serp_capture,
    )
    from apps.citations.services import serp_google
    from apps.search_gap.services.google_rank import locale

    swarm_run = job.run
    key = job.inputs["prompt_key"]
    page = int(job.inputs.get("page") or 1)
    prompt = swarm_run.prompts.get(key) or {}
    query = " ".join((prompt.get("text") or "").split())
    location = (prompt.get("location") or "").lower()

    if swarm_run.version.version == 1:
        earlier = _recent_captures(swarm_run.tool_ctx(), query, location).get(page)
        if earlier is not None:
            seconds = _age_seconds(earlier.get("captured_at"))
            job.reporter.step(f"Using the page captured {age_text(seconds)} ago")
            job.release_primary()
            job.reporter.record_capture("serp", earlier["capture_id"], earlier)
            swarm_run.set_capture(key, page, earlier)
            swarm_run.add_note("google", "info", "reused_captures", REUSED_NOTE)
            return output(earlier, reused_seconds=seconds)

    if not serp_google.is_configured():
        return skipped("not_configured", "Google results are not configured.")
    reliable = serp_google.max_reliable_pages()
    if page > reliable:
        # A plan made before the cap (a re-run) may still hold this page.
        return skipped("not_configured", (
            f"Google results past page {reliable} are not reliable through "
            f"{serp_google.PROVIDER_LABELS.get(serp_google.provider(), 'this provider')}, "
            "so this page was not searched."
        ))
    job.check()
    if not _serp_quota().consume(str(swarm_run.website.id)):
        return skipped("daily_limit", "This project used its daily Google search allowance.")
    gl, hl = locale(location)
    payload = _search(query, page, gl, hl)
    code = str(payload.get("error") or "")
    if code and _retryable(code):
        job.reporter.retry()
        _pause(job, RETRY_SECONDS)
        job.reporter.step(f"Fetching Google page {page}")
        payload = _search(query, page, gl, hl)
        code = str(payload.get("error") or "")
    job.release_primary()
    if code:
        return failed("upstream_error", _serp_error(code)["message"])
    job.check()
    job.reporter.step("Reading the results")
    capture_id = _capture_id()
    capture = build_serp_capture(payload, website=swarm_run.website, query=query, page=page,
                                 location=location, capture_id=capture_id)
    capture["cost_usd"] = float(serp_google.cost_per_search_usd())
    job.reporter.record_capture("serp", capture_id, capture)
    swarm_run.set_capture(key, page, capture)
    return output(capture, reused_seconds=None)
