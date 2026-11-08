"""Sources for the writing agents and the context build (design/p2_backend.md
12b): pages read with the SSRF-safe browse fetcher, their text, the room
each gets in a request, and the check that keeps a quote honest.

A quote counts only when it is found word for word in the source it
names, case and whitespace ignored (design/p2_contract.md 1d). Page text
reaches a model as escaped data in its own tag, so it cannot close the
tag or pose as instructions.
"""
from __future__ import annotations

import html
import time
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from django.utils import timezone

from apps.agent.tools.context import ToolError

# A page the browse parser could not take yet (its parse slots are busy)
# is asked for once more after this pause.
BUSY_RETRY_SECONDS = 2.0


def fold(text) -> str:
    """Casefolded, whitespace collapsed: the form quotes are compared in."""
    return " ".join(str(text or "").casefold().split())


def found(haystack, needle) -> bool:
    """True when ``needle`` (non-empty) appears in ``haystack`` word for
    word, case and whitespace ignored. A quote copied from an escaped
    data block (``&amp;``) counts as its unescaped text."""
    return verbatim(haystack, needle) is not None


def verbatim(haystack, needle) -> str | None:
    """The form of ``needle`` found in ``haystack`` (as written, or
    unescaped), stripped; None when neither is there."""
    needle = str(needle or "").strip()
    if not fold(needle):
        return None
    text = fold(haystack)
    if fold(needle) in text:
        return needle
    plain = html.unescape(needle)
    return plain if fold(plain) and fold(plain) in text else None


def reader_text(result: dict | None, *, limit: int | None = None) -> str:
    """Headings, paragraphs, quotes, code, list items and table rows of a
    BrowseResult's reader view, one per line."""
    reader = (result or {}).get("reader") or {}
    chunks = []
    for block in reader.get("blocks") or []:
        kind = block.get("type")
        if kind in ("heading", "paragraph", "quote", "code"):
            chunks.append(block.get("text") or "")
        elif kind == "list":
            chunks.extend(block.get("items") or [])
        elif kind == "table":
            chunks.extend(" | ".join(row) for row in (block.get("rows") or []))
    text = "\n".join(c for c in chunks if c)
    return text[:limit] if limit is not None else text


@dataclass(frozen=True)
class SourcePage:
    id: str
    url: str
    final_url: str
    title: str
    text: str
    # "ok" (loaded with a 2xx status) or "failed"
    state: str
    status_code: int | None
    words: int
    links: list = field(default_factory=list)
    seo: dict | None = None
    # The browse error ({code, message}) when the fetch itself failed.
    error: dict | None = None
    read_at: str = ""

    @property
    def ok(self) -> bool:
        return self.state == "ok"

    @property
    def reason(self) -> str:
        """Why the page is not usable, in the user's words."""
        if self.error:
            return self.error.get("message") or "The page could not be loaded."
        if self.status_code is not None and not 200 <= self.status_code < 300:
            return f"The page answered with HTTP {self.status_code}."
        return ""


def _browse_quota():
    from core.quota import DailyQuota

    return DailyQuota(namespace="agent_browse", setting_name="AGENT_BROWSE_DAILY_LIMIT",
                      default_limit=300)


def read_page(url: str, *, id: str, per_page_chars: int, with_links: bool = False) -> SourcePage:
    """One page through browse.fetch_page; never raises for a bad page."""
    from apps.agent.services import browse

    result = browse.fetch_page(url, with_links=True) if with_links else browse.fetch_page(url)
    if (result.get("error") or {}).get("code") == "busy":
        time.sleep(BUSY_RETRY_SECONDS)
        result = browse.fetch_page(url, with_links=True) if with_links else browse.fetch_page(url)
    error = result.get("error")
    status = result.get("status_code")
    full = "" if error else reader_text(result)
    ok = error is None and status is not None and 200 <= int(status) < 300
    links = result.get("links")
    if links is None:
        links = [{"url": link.get("href"), "text": link.get("text") or ""}
                 for link in ((result.get("reader") or {}).get("links") or [])]
    return SourcePage(
        id=id,
        url=url,
        final_url=result.get("final_url") or url,
        title=(result.get("title") or "") if not error else "",
        text=full[:per_page_chars],
        state="ok" if ok else "failed",
        status_code=status,
        words=len(full.split()),
        links=list(links or []),
        seo=result.get("seo"),
        error=error,
        read_at=result.get("captured_at") or timezone.now().isoformat(),
    )


def read_pages(urls, *, prefix: str, per_page_chars: int, quota_user=None,
               with_links: bool = False, start: int = 1) -> list[SourcePage]:
    """``urls`` read in order as ids ``{prefix}{start}``, ``{prefix}{start+1}``...

    With ``quota_user`` each fetch first takes one page of the user's daily
    browse limit (AGENT_BROWSE_DAILY_LIMIT); when it is used up, reading
    stops with a ToolError."""
    quota = _browse_quota() if quota_user is not None else None
    pages = []
    for n, url in enumerate(urls, start=start):
        if quota is not None and not quota.consume(str(quota_user.id)):
            raise ToolError(
                f"You have opened {quota.limit(quota_user.id)} pages today, the daily limit.",
                summary="Daily page limit reached",
            )
        pages.append(read_page(url, id=f"{prefix}{n}", per_page_chars=per_page_chars,
                               with_links=with_links))
    return pages


def water_caps(sizes, total: int) -> list[int]:
    """How many characters each of ``sizes`` keeps within ``total``: short
    ones whole, long ones an equal share of what is left."""
    sizes = [max(0, int(s)) for s in sizes]
    caps = [0] * len(sizes)
    left = max(0, int(total))
    order = sorted(range(len(sizes)), key=lambda i: sizes[i])
    for n, i in enumerate(order):
        share = left // (len(order) - n)
        caps[i] = min(sizes[i], share)
        left -= caps[i]
    return caps


def water_fill(texts, total: int) -> list[str]:
    texts = [str(t or "") for t in texts]
    return [t[:cap] for t, cap in zip(texts, water_caps([len(t) for t in texts], total),
                                      strict=True)]


def escape(text) -> str:
    return html.escape(str(text or ""), quote=False)


def tag(name: str, text, **attrs) -> dict:
    """A data block ``<name a="...">text</name>``: text escaped so it cannot
    close its tag, attribute values escaped with quotes."""
    head = "".join(f' {key}="{html.escape(str(value), quote=True)}"'
                   for key, value in attrs.items() if value is not None)
    return {"type": "text", "text": f"<{name}{head}>\n{escape(text)}\n</{name}>"}


def host(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def is_http(url: str) -> bool:
    try:
        parts = urlsplit(url or "")
    except ValueError:
        return False
    return parts.scheme.lower() in ("http", "https") and bool(parts.hostname)


def apex_of(url_or_domain: str) -> str:
    from apps.citations.services.url_normalizer import extract_apex_domain

    value = (url_or_domain or "").strip()
    if "://" not in value:
        value = f"https://{value}"
    return extract_apex_domain(host(value))


def same_site(url: str, website) -> bool:
    """An http(s) URL on the website's registrable domain (subdomains too)."""
    if not is_http(url):
        return False
    site = apex_of(website.url or "")
    return bool(site) and apex_of(url) == site


def path_label(url: str) -> str:
    """The path of a page as people read it: ``/pricing``, ``/`` for home."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    path = parts.path or "/"
    return f"{path}?{parts.query}" if parts.query else path
