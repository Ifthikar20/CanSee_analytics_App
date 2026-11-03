"""page_inspect: one page read, yours or a competitor's, and its on-page
SEO facts (design section 8c).

Your page is your best-ranking result in the prompt's Google pages, else
your homepage. Competitor page k is the k-th other site in those pages,
the competitors you named first, then tracked competitors, then by
position, one page per site. The fetch is inspect_page's (same address
rule, daily allowance and capture), and a URL is fetched once per
execution: a second task that wants it waits for the first and reuses
what it read.
"""
from __future__ import annotations

import logging
import threading

from apps.agent.constants import WorkerType
from apps.agent.runtime.events import now_iso
from apps.agent.runtime.swarm.context import (
    PageSlot,
    TaskJob,
    TaskStopped,
    WorkerOutput,
    failed,
    plural,
    skipped,
)
from apps.agent.tools.shaping import clip, host_of

logger = logging.getLogger("apps")

TARGET_CHARS = 60
TEXT_CHARS = 6000
EXCERPT_CHARS = 600
MAX_H1 = 5
MAX_H2 = 8
MAX_SCHEMA_TYPES = 12
HEADING_CHARS = 160
TITLE_CHARS = 200
DESCRIPTION_CHARS = 300
WAIT_SLICE_SECONDS = 0.25

NO_OTHER_SITE = "Google listed no other site for this prompt."
GOOGLE_UNREAD = "The Google results for this prompt could not be read."
BLOCKED = ("This address did not come from Google results in this chat or from the project's "
           "sites, so it was not read.")
BROWSE_LIMIT = "You used today's page inspection allowance."
NOT_READ = "The page could not be loaded."
# Fetch error codes a task reports as they are (contract 5c). Every other
# reason the page could not be read (too_large, not_html, too_complex,
# busy) is a fetch_failed whose message names that reason.
TASK_FETCH_CODES = ("fetch_failed", "timeout", "blocked_url")


def _fetch_error(error: dict) -> dict:
    code = error.get("code") or "fetch_failed"
    return {"code": code if code in TASK_FETCH_CODES else "fetch_failed",
            "message": error.get("message") or NOT_READ}


def page_target(url: str) -> str:
    """Host and path, as the row shows a chosen page."""
    from urllib.parse import urlsplit

    try:
        path = urlsplit(url).path or ""
    except ValueError:
        path = ""
    return clip(f"{host_of(url) or url}{path if path != '/' else ''}", TARGET_CHARS)


def _homepage(website) -> str:
    url = (website.url or "").strip()
    return url if "://" in url else f"https://{url}"


def _own_page(job: TaskJob) -> dict | WorkerOutput:
    swarm_run = job.run
    key = job.inputs["prompt_key"]
    captures = swarm_run.serp_captures(key)
    yours = [r for c in captures for r in c.get("results") or []
             if r.get("is_yours") and r.get("url")]
    if yours:
        best = min(yours, key=lambda r: r.get("position") or 999)
        return {
            "url": best["url"],
            "position": best.get("position"),
            "title": best.get("title") or "",
            "competitor_name": None,
            "chosen_because": f"Ranks #{best.get('position')} on Google for this prompt",
        }
    if not swarm_run.plan.get("serp_configured"):
        because = "Your homepage: Google results are not available"
    elif not captures:
        because = "Your homepage: the Google results could not be read"
    else:
        because = "Your homepage: you are not in the Google pages searched"
    return {"url": _homepage(swarm_run.website), "position": None, "title": "",
            "competitor_name": None, "chosen_because": because}


def _competitor_page(job: TaskJob) -> dict | WorkerOutput:
    from apps.citations.services.url_normalizer import extract_apex_domain

    swarm_run = job.run
    key = job.inputs["prompt_key"]
    slot = int(job.inputs.get("slot") or 1)
    prefer = [str(d).lower() for d in job.inputs.get("prefer") or []]
    captures = swarm_run.serp_captures(key)
    if not captures:
        if swarm_run.prompt_specs(key, WorkerType.SERP_PAGE):
            return skipped("dependency_failed", GOOGLE_UNREAD)
        return skipped("nothing_to_analyze", NO_OTHER_SITE)

    def apex_of(row: dict) -> str:
        return extract_apex_domain(row.get("domain") or host_of(row.get("url") or ""))

    rows = [r for c in captures for r in c.get("results") or []
            if not r.get("is_yours") and r.get("url")]
    rows.sort(key=lambda r: (apex_of(r) not in prefer, not r.get("is_competitor"),
                             r.get("position") or 999))
    picked, seen = [], set()
    for row in rows:
        apex = apex_of(row)
        if apex and apex not in seen:
            seen.add(apex)
            picked.append(row)
    if len(picked) < slot:
        message = NO_OTHER_SITE if not picked else (
            f"Google listed only {plural(len(picked), 'other site')} for this prompt.")
        return skipped("nothing_to_analyze", message)
    row = picked[slot - 1]
    because = f"Ranks #{row.get('position')} on Google for this prompt"
    if apex_of(row) in prefer:
        because += "; a competitor you named"
    elif row.get("is_competitor"):
        because += "; a tracked competitor"
    return {
        "url": row["url"],
        "position": row.get("position"),
        "title": row.get("title") or "",
        "competitor_name": row.get("competitor_name"),
        "chosen_because": because,
    }


def _seo_detail(seo: dict) -> dict:
    def text(value, limit):
        return clip(value, limit) if isinstance(value, str) and value else None

    return {
        "title": text(seo.get("title"), TITLE_CHARS),
        "title_length": seo.get("title_length"),
        "meta_description": text(seo.get("meta_description"), DESCRIPTION_CHARS),
        "meta_description_length": seo.get("meta_description_length"),
        "h1": [clip(h, HEADING_CHARS) for h in (seo.get("h1") or [])[:MAX_H1]],
        "h2": [clip(h, HEADING_CHARS) for h in (seo.get("h2") or [])[:MAX_H2]],
        "word_count": seo.get("word_count"),
        "structured_data": list(seo.get("structured_data") or [])[:MAX_SCHEMA_TYPES],
        "canonical": text(seo.get("canonical"), TITLE_CHARS),
        "robots": seo.get("robots") or "",
        "links": seo.get("links") or {"internal": None, "external": None},
        "images": seo.get("images") or {"count": None, "missing_alt": None},
    }


def _output(job: TaskJob, choice: dict, slot: PageSlot, *, reused_from: str | None
            ) -> WorkerOutput:
    capture = slot.capture or {}
    seo = capture.get("seo") or {}
    url = capture.get("url") or choice["url"]
    final_url = capture.get("final_url") or url
    title = seo.get("title") or choice.get("title") or host_of(final_url)
    words = seo.get("word_count") or 0
    schema_types = len(seo.get("structured_data") or [])
    detail = {
        "role": job.inputs.get("role"),
        "url": url,
        "final_url": final_url,
        "status_code": capture.get("status_code"),
        "domain": host_of(final_url),
        "competitor_name": choice.get("competitor_name"),
        "position": choice.get("position"),
        "chosen_because": choice["chosen_because"],
        "title": clip(title, TITLE_CHARS),
        "seo": _seo_detail(seo),
        "excerpt": clip(slot.text, EXCERPT_CHARS),
        "reused_from": reused_from,
    }
    with job.run.lock:
        job.run.page_texts[job.task_id] = {"seo": seo, "text": slot.text}
    line = (f"{clip(title, 50)} · {plural(words, 'word')} · "
            f"{plural(schema_types, 'schema type')}")
    return WorkerOutput(status="done", result=line, detail=detail, pane=slot.pane,
                        target=page_target(final_url))


def _claim(job: TaskJob, url: str) -> tuple[PageSlot, bool]:
    """The URL's slot in this execution, and whether this task fetches it."""
    swarm_run = job.run
    with swarm_run.lock:
        slot = swarm_run.pages.get(url)
        if slot is not None:
            return slot, False
        slot = PageSlot(owner=job.task_id, owner_label=job.spec.get("label") or job.task_id)
        swarm_run.pages[url] = slot
        return slot, True


def _wait(job: TaskJob, ready: threading.Event) -> None:
    """Wait for another task's fetch, as long as this task may run."""
    while not ready.wait(WAIT_SLICE_SECONDS):
        job.check()
        if job.remaining() <= 0:
            raise TaskStopped()


def _fetch(job: TaskJob, url: str, slot: PageSlot) -> None:
    """Fetch ``url`` into ``slot`` (also when this task stopped meanwhile:
    a task waiting on the slot can still use the page)."""
    from apps.agent.services.browse import fetch_page
    from apps.agent.tools.research import _browse_quota, _capture_id, _reader_text

    swarm_run = job.run
    try:
        if not _browse_quota().consume(str(swarm_run.ctx.user.id)):
            slot.error = {"code": "browse_limit", "message": BROWSE_LIMIT}
            return
        try:
            result = fetch_page(url) or {}
        except Exception:
            logger.warning("agent swarm: fetch_page raised for task %s", job.task_id,
                           exc_info=True)
            result = {"error": {"code": "fetch_failed", "message": NOT_READ}}
        job.release_primary()
        capture_id = _capture_id()
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
        if capture["error"]:
            slot.error = _fetch_error(capture["error"])
            return
        slot.text = _reader_text(capture["reader"])[:TEXT_CHARS]
        slot.capture = {k: v for k, v in capture.items() if k != "reader"}
        slot.pane = {"kind": "page", "tab_id": f"page:{capture_id}"}
    finally:
        slot.ready.set()


def run(job: TaskJob) -> WorkerOutput:
    from apps.agent.tools.research import _may_fetch

    choice = _own_page(job) if job.inputs.get("role") == "own" else _competitor_page(job)
    if isinstance(choice, WorkerOutput):
        return choice
    url = choice["url"]
    target = page_target(url)
    job.reporter.step(f"Reading {host_of(url) or url}", target=target)
    if not _may_fetch(job.run.tool_ctx(), url):
        return skipped("blocked_url", BLOCKED, target=target)

    slot, owner = _claim(job, url)
    if owner:
        _fetch(job, url, slot)
        job.check()
        if slot.capture is not None:
            job.reporter.step("Reading SEO facts")
    else:
        job.release_primary()
        job.reporter.step(f"Same page as {slot.owner_label}")
        _wait(job, slot.ready)
        job.check()
    # The task that finishes with the page first puts it in the research
    # store (the owner, unless it stopped after its fetch).
    if slot.capture is not None and not slot.recorded:
        slot.recorded = job.reporter.record_capture("page", slot.capture["capture_id"],
                                                    slot.capture)
    if slot.capture is None:
        error = slot.error or {"code": "fetch_failed", "message": NOT_READ}
        if error["code"] == "browse_limit":
            return skipped("browse_limit", error["message"], target=target)
        return failed(error["code"], error["message"], target=target)
    return _output(job, choice, slot, reused_from=None if owner else slot.owner)
