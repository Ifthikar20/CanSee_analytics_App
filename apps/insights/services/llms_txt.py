"""llms.txt and llms-full.txt of a site (contract 5d).

llms.txt (llmstxt.org) is an optional Markdown file that points AI tools to
a site's most useful pages; its one required part is a first line
``# Name``. ``fetch`` reads it with the SSRF-safe fetcher and reports
``found`` (with the title, the number of ``##`` sections and of linked
list items), ``invalid`` (no title line), ``missing`` (4xx) or ``failed``,
plus whether ``/llms-full.txt`` exists. A site that answers every address
with its HTML page (a page shown for a missing file) has no llms.txt, so
an HTML response counts as missing. The text itself is never shown or
sent to a model.
"""

from __future__ import annotations

import re

from apps.agent.services.browse import USER_AGENT
from core.validators.safe_http import FetchError, safe_get

MAX_BYTES = 200_000
MAX_TITLE = 120
FETCH_TIMEOUT = (5, 10)
FETCH_DEADLINE = 10
ALLOWED_PORTS = (80, 443, 8080, 8443)
HTML_TYPES = ("text/html", "application/xhtml+xml")

FOUND, INVALID, MISSING, FAILED = "found", "invalid", "missing", "failed"

_LIST_ITEM = re.compile(r"^\s*(?:[-*+]|\d{1,3}[.)])\s+")
_HTML_START = re.compile(r"^\s*(<!--.*?-->\s*)*<(!doctype\s+html|html)[\s>]", re.I | re.S)


def _get(url: str, max_bytes: int):
    return safe_get(
        url,
        timeout=FETCH_TIMEOUT,
        max_bytes=max_bytes,
        truncate=True,
        allowed_content_types=(),
        allowed_ports=ALLOWED_PORTS,
        deadline=FETCH_DEADLINE,
        headers={"User-Agent": USER_AGENT},
    )


def _is_html(resp, text: str = "") -> bool:
    return resp.content_type in HTML_TYPES or bool(_HTML_START.match(text[:2048]))


def has_link(line: str) -> bool:
    """Whether ``line`` holds a Markdown link ``[text](url)``: text without
    ``]``, a url whose first character is neither ``)`` nor a space, then
    ``)``. One pass over the line, because the file is a stranger's: the
    regular expression this replaces backtracked for minutes on a single
    unclosed link."""
    last_paren = line.rfind(")")
    opened = False
    for i, char in enumerate(line):
        if char == "[":
            opened = True
        elif char == "]":
            first = line[i + 2:i + 3]
            if (opened and line[i + 1:i + 2] == "(" and first and first != ")"
                    and not first.isspace() and last_paren > i + 2):
                return True
            opened = False
    return False


def parse(text: str) -> dict:
    """``{valid, title, sections, links}`` of an llms.txt body."""
    if text.startswith("﻿"):
        text = text[1:]
    lines = text.splitlines()
    first = next((line for line in lines if line.strip()), "")
    if not first.startswith("# "):
        return {"valid": False, "title": None, "sections": None, "links": None}
    return {
        "valid": True,
        "title": first[2:].strip()[:MAX_TITLE],
        "sections": sum(1 for line in lines if line.startswith("## ")),
        "links": sum(1 for line in lines if _LIST_ITEM.match(line) and has_link(line)),
    }


def _full(origin: str) -> str:
    """found / missing / failed for ``/llms-full.txt`` (one byte is read)."""
    try:
        resp = _get(f"{origin}/llms-full.txt", 1)
    except FetchError:
        return FAILED
    if 200 <= resp.status_code < 300:
        return MISSING if _is_html(resp) else FOUND
    return MISSING if 400 <= resp.status_code < 500 else FAILED


def fetch(origin: str) -> dict:
    """The ``llms_txt`` block of an audit (contract 5d) for ``origin``."""
    origin = origin.rstrip("/")
    url = f"{origin}/llms.txt"
    out = {"state": FAILED, "url": url, "status_code": None, "title": None,
           "sections": None, "links": None}
    try:
        resp = _get(url, MAX_BYTES)
    except FetchError:
        out["full"] = _full(origin)
        return out
    out["status_code"] = resp.status_code
    if 200 <= resp.status_code < 300:
        text = resp.content.decode("utf-8", errors="replace")
        if _is_html(resp, text):
            out["state"] = MISSING
        else:
            parsed = parse(text)
            out["state"] = FOUND if parsed["valid"] else INVALID
            out.update({k: parsed[k] for k in ("title", "sections", "links")})
    elif 400 <= resp.status_code < 500:
        out["state"] = MISSING
    out["full"] = _full(origin)
    return out
