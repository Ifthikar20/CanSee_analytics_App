"""Google page one for an insights run (research reads, fan-out checks).

Every request takes one page of the project's daily insights quota before
it is sent. ``ctx.billed_page()`` counts each response the provider bills
(HTTP 200, including an empty or unreadable page, and 404), so a run's
Google page count never passes what the user confirmed: a billed response
is never retried, and the one retry after a network error, a 429 or a 5xx
(which are not billed) takes a quota page of its own.
"""

from __future__ import annotations

import logging
import time

from apps.citations.services import serp_google

logger = logging.getLogger("apps")

PAGE_SIZE = 10
RETRY_SECONDS = 3.0
# Provider responses that are billed although they carry an error code.
_BILLED_ERRORS = ("empty_results", "decode", "http_404")
# serp_google error codes in words (wave 1 contract 2h, copied).
SERP_MESSAGES = {
    "network": "Google search could not be reached.",
    "decode": "Google search returned an unreadable response.",
    "empty_query": "The search query was empty.",
    "empty_results": "Google's results page came back empty, so this search could not be read.",
    "serpapi_not_configured": "Google results aren't configured on this server.",
}
SERP_FAILED = "Google search failed."
DAILY_LIMIT = "This project used its Google pages for today."
CANCELLED = "Stopped before it ran."


def billed(payload: dict) -> bool:
    """Whether the provider bills this response."""
    error = str(payload.get("error") or "")
    return not error or error in _BILLED_ERRORS


def _retryable(error: str) -> bool:
    return error in ("network", "http_429") or error.startswith("http_5")


def error_message(error: str) -> tuple[str, str]:
    """(public item code, message) for a page_one error (contract 1e rule 6)."""
    if error == "daily_limit":
        return "daily_limit", DAILY_LIMIT
    if error == "cancelled":
        return "cancelled", CANCELLED
    if error.startswith("http_"):
        return "serp_failed", f"Google search failed (HTTP {error[5:]})."
    return "serp_failed", SERP_MESSAGES.get(error, SERP_FAILED)


def _search(query: str, country: str) -> dict:
    try:
        return serp_google.search(query, max_results=PAGE_SIZE, country=country or None)
    except Exception:
        logger.warning("insights: serp_google.search raised", exc_info=True)
        return {"error": "network"}


def page_one(ctx, query: str, *, country: str = "") -> dict:
    """One serp_google.search payload for ``query`` (page one, 10 results)
    in ``country`` (ISO-2, "" for the provider's default), or
    ``{"error": "cancelled" | "daily_limit"}`` when nothing was sent."""
    if ctx.cancelled():
        return {"error": "cancelled"}
    if not ctx.google_page():
        return {"error": "daily_limit"}
    text = " ".join((query or "").split())
    payload = _search(text, country)
    if billed(payload):
        ctx.billed_page()
        return payload
    if _retryable(str(payload.get("error") or "")):
        time.sleep(RETRY_SECONDS)
        if not ctx.cancelled() and ctx.google_page():
            payload = _search(text, country)
            if billed(payload):
                ctx.billed_page()
    return payload
