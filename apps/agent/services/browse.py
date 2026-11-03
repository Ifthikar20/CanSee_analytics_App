"""One web page for the browser pane: SEO facts, a reader view, and
whether the page may be shown live in a frame.

The fetch goes through core.validators.safe_http with the browse limits:
public addresses only (checked again on the connected socket and on every
redirect), ports 80/443/8080/8443, HTML only, 2 MB, 12 s in total.
Failures come back inside the result as ``error`` so the pane can say
what happened; nothing here raises for a bad page.

Pages are hostile input. Every pass over the tree is iterative and linear
in its size, so deep nesting cannot exhaust the stack or the CPU, and
page text is untrusted data for whoever reads the result. A parse tree
takes far more memory than its bytes (2 MB of tiny elements is about
300 MB of objects), so a page with more than MAX_TAGS tags is refused,
at most PARSE_SLOTS pages are parsed at a time in this process, and each
tree is taken apart as soon as it has been read.
"""
from __future__ import annotations

import json
import re
import threading
import time
from urllib.parse import urldefrag, urljoin, urlsplit

from bs4 import BeautifulSoup, NavigableString, Tag
from bs4.dammit import UnicodeDammit
from bs4.element import CData, Comment, Declaration, Doctype, ProcessingInstruction
from django.utils import timezone

from apps.citations.services.url_normalizer import extract_apex_domain
from core.validators.safe_http import FetchError, safe_get

DEADLINE_SECONDS = 12
MAX_BYTES = 2_000_000
ALLOWED_PORTS = (80, 443, 8080, 8443)
HTML_TYPES = ("text/html", "application/xhtml+xml")
CONNECT_TIMEOUT = 5
USER_AGENT = "Mozilla/5.0 (compatible; CanseeAgent/1.0; +https://cansee.ai/bot)"

# Start and end tags, comments and declarations: each becomes a tree node
# or splits the text into another one. Real pages stay far below this; at
# the limit a tree holds about 70 MB.
MAX_TAGS = 80_000
PARSE_SLOTS = 2
PARSE_WAIT_SECONDS = 10
# Nodes read for one heading's text in the SEO facts: headings nested in
# each other would otherwise each re-read the rest of the page.
MAX_HEADING_NODES = 5_000

MAX_READER_BLOCKS = 400
MAX_READER_LINKS = 50
# fetch_page(with_links=True) and fetch_page(with_headers=True).
MAX_DOCUMENT_LINKS = 300
MAX_LINK_TEXT = 120
MAX_HEADER_VALUE = 300
MAX_H1 = 20
MAX_H2 = 20
MAX_HREFLANG = 50
MAX_STRUCTURED_TYPES = 20
MAX_LIST_ITEMS = 50
MAX_TABLE_ROWS = 50
MAX_TABLE_COLS = 12
MAX_TEXT = 2000
MAX_HEADING = 300
MAX_CELL = 300
MAX_CODE = 5000
MAX_JSON_LD_CHARS = 200_000

ERROR_MESSAGES = {
    "blocked_url": (
        "This address can't be opened here: only public web pages (http or https, "
        "ports 80, 443, 8080 or 8443) can be shown."
    ),
    "timeout": f"The page took longer than {DEADLINE_SECONDS} seconds to load.",
    "too_large": f"The page is larger than {MAX_BYTES // 1_000_000} MB.",
    "too_complex": f"The page has more than {MAX_TAGS:,} HTML tags, too many to read here.",
    "busy": "Too many pages are being read right now. Try again in a moment.",
    "not_html": "This address is not a web page.",
    "fetch_failed": "The page could not be loaded.",
}

# Never part of the readable content. (<button> stays: accordion FAQs put
# their questions in buttons.)
_DROP = frozenset({
    "script", "style", "noscript", "template", "head", "iframe", "object", "embed",
    "svg", "canvas", "select", "option", "input", "textarea", "nav", "aside",
})
# Page chrome, unless it sits inside the article or main content.
_CHROME = frozenset({"header", "footer"})
_CONTENT = frozenset({"article", "main"})
_NO_WORDS = frozenset({"script", "style", "noscript", "template", "svg"})
_HEADINGS = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}
_INLINE = frozenset({
    "a", "abbr", "b", "bdi", "bdo", "big", "cite", "code", "data", "del", "dfn", "em",
    "font", "i", "ins", "kbd", "label", "mark", "q", "s", "samp", "small", "span",
    "strike", "strong", "sub", "sup", "time", "tt", "u", "var", "wbr",
})
# A table holding any of these is page layout, read as content blocks.
_LAYOUT_MARKERS = ["table", "p", "h1", "h2", "h3", "ul", "ol", "div"]
_NOT_TEXT = (Comment, CData, Declaration, Doctype, ProcessingInstruction)
_SPACE = re.compile(r"\s+")
_CHARSET = re.compile(r"charset\s*=\s*[\"']?([A-Za-z0-9._:\-]+)", re.I)
_HTML_START = re.compile(rb"^\s*(<!--.*?-->\s*)*<(!doctype\s+html|html)[\s>]", re.I | re.S)
_TAG = re.compile(r"<[A-Za-z!?/]")

_parse_slots = threading.BoundedSemaphore(PARSE_SLOTS)


def fetch_page(url: str, *, with_headers: bool = False, with_links: bool = False) -> dict:
    """BrowseResult (contract section 4) for ``url``.

    ``error`` is null on success; otherwise ``{code, message}`` with code
    blocked_url, fetch_failed, timeout, too_large, too_complex, busy or
    not_html, and ``seo`` and ``reader`` are null. An HTTP error status is
    not a fetch error: the page that came back is parsed and
    ``status_code`` says what it was.

    ``with_headers`` adds ``headers``: ``{"x-robots-tag", "content-type"}``
    as the response sent them ("" when absent or when nothing came back).
    ``with_links`` adds ``links``: every ``<a href>`` of the whole document,
    navigation, header and footer included (the reader view leaves those
    out), as ``[{"url", "text"}]`` ([] when the page was not parsed).
    """
    url = (url or "").strip()
    flags = {"with_headers": with_headers, "with_links": with_links}
    started = time.monotonic()
    try:
        resp = safe_get(
            url,
            timeout=(CONNECT_TIMEOUT, DEADLINE_SECONDS),
            max_bytes=MAX_BYTES,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.1",
                "Accept-Language": "en-US,en;q=0.9",
            },
            allowed_content_types=HTML_TYPES,
            allowed_ports=ALLOWED_PORTS,
            deadline=DEADLINE_SECONDS,
        )
    except FetchError as exc:
        code = "not_html" if exc.code == "content_type" else exc.code
        return _extras(_failed(url, code), **flags)
    fetch_ms = int((time.monotonic() - started) * 1000)

    final_url = resp.final_url
    headers = resp.headers
    if resp.content_type not in HTML_TYPES and not _looks_like_html(resp.content):
        return _extras(_failed(url, "not_html", final_url=final_url, status_code=resp.status_code),
                       headers=headers, **flags)

    if not _parse_slots.acquire(timeout=PARSE_WAIT_SECONDS):
        return _extras(_failed(url, "busy", final_url=final_url, status_code=resp.status_code),
                       headers=headers, **flags)
    links = None
    try:
        # Decoded here the way BeautifulSoup decodes bytes, so the tags are
        # counted in whatever encoding the page uses before a tree is built.
        charset = _charset(resp.headers.get("Content-Type"))
        markup = UnicodeDammit(
            resp.content, known_definite_encodings=[charset] if charset else [], is_html=True,
        ).unicode_markup
        if markup is None:
            return _extras(
                _failed(url, "not_html", final_url=final_url, status_code=resp.status_code),
                headers=headers, **flags,
            )
        if _too_many_tags(markup):
            return _extras(
                _failed(url, "too_complex", final_url=final_url, status_code=resp.status_code),
                headers=headers, **flags,
            )
        soup = BeautifulSoup(markup, "html.parser")
        try:
            seo = seo_facts(soup, final_url)
            reader = reader_view(soup, final_url, title=seo["title"] or "")
            if with_links:
                links = document_links(soup, final_url)
        finally:
            # The tree is full of reference cycles; taking it apart frees it
            # now instead of at the next garbage collection.
            soup.decompose()
    finally:
        _parse_slots.release()
    seo["bytes"] = len(resp.content)
    seo["fetch_ms"] = fetch_ms
    return _extras({
        "url": url,
        "final_url": final_url,
        "status_code": resp.status_code,
        "frameable": is_frameable(final_url, resp.headers),
        "title": seo["title"] or reader["title"] or urlsplit(final_url).hostname or final_url,
        "captured_at": _now(),
        "seo": seo,
        "reader": reader,
        "error": None,
    }, headers=headers, links=links, **flags)


def _extras(result: dict, *, with_headers: bool, with_links: bool, headers=None,
            links=None) -> dict:
    """``result`` with the ``headers`` and ``links`` fetch_page was asked for."""
    if with_headers:
        lowered = {str(k).lower(): str(v) for k, v in (headers or {}).items()}
        result["headers"] = {
            name: _strip_nul(lowered.get(name, ""))[:MAX_HEADER_VALUE]
            for name in ("x-robots-tag", "content-type")
        }
    if with_links:
        result["links"] = links or []
    return result


def document_links(soup: BeautifulSoup, base_url: str) -> list[dict]:
    """Every ``<a href>`` of the document as ``[{"url", "text"}]``: absolute
    http(s), fragment removed, de-duplicated in document order, at most
    MAX_DOCUMENT_LINKS. Navigation, header and footer count too. Leaves
    ``soup`` unchanged."""
    seen: set[str] = set()
    links: list[dict] = []
    for a in soup.find_all("a", href=True):
        href = _absolute(a["href"], base_url)
        if not href:
            continue
        href = urldefrag(href)[0]
        if href in seen:
            continue
        seen.add(href)
        text = (
            _clean(_visible_text(a, limit=MAX_LINK_TEXT, max_nodes=MAX_HEADING_NODES))
            or _clean(a.get("aria-label"))
            or _clean(a.get("title"))
        )
        links.append({"url": href, "text": text[:MAX_LINK_TEXT]})
        if len(links) >= MAX_DOCUMENT_LINKS:
            break
    return links


def is_frameable(url: str, headers: dict) -> bool:
    """Whether a browser would render ``url`` inside the pane's iframe.

    A CSP frame-ancestors directive decides when present (it overrides
    X-Frame-Options); the page is frameable only if every such directive
    allows any https origin. Otherwise X-Frame-Options DENY or SAMEORIGIN
    blocks it. Plain http is never shown live.
    """
    if urlsplit(url).scheme.lower() != "https":
        return False
    lowered = {str(k).lower(): str(v) for k, v in (headers or {}).items()}
    ancestors = []
    for policy in lowered.get("content-security-policy", "").split(","):
        for directive in policy.split(";"):
            tokens = directive.strip().lower().split()
            if tokens and tokens[0] == "frame-ancestors":
                ancestors.append(set(tokens[1:]))
    if ancestors:
        return all(sources & {"*", "https:", "https://*"} for sources in ancestors)
    xfo = lowered.get("x-frame-options", "").lower()
    return "deny" not in xfo and "sameorigin" not in xfo


def seo_facts(soup: BeautifulSoup, base_url: str) -> dict:
    """SeoFacts (contract section 4) minus ``bytes`` and ``fetch_ms``.

    Counts cover the whole document, chrome included, the way a crawler
    sees it. Leaves ``soup`` unchanged.
    """
    title = _page_title(soup)
    description = _meta(soup, "description")
    html = soup.find("html")
    lang = _clean(html.get("lang")) if isinstance(html, Tag) else ""

    host_apex = extract_apex_domain(urlsplit(base_url).hostname or "")
    apex_of: dict[str, str] = {}
    internal = external = 0
    for a in soup.find_all("a", href=True):
        href = _absolute(a["href"], base_url)
        if not href:
            continue
        host = urlsplit(href).hostname or ""
        if host not in apex_of:
            apex_of[host] = extract_apex_domain(host)
        if apex_of[host] == host_apex:
            internal += 1
        else:
            external += 1

    images = soup.find_all("img")
    return {
        "title": title or None,
        "title_length": len(title),
        "meta_description": description or None,
        "meta_description_length": len(description),
        "canonical": _link_href(soup, "canonical", base_url),
        "robots": _meta(soup, "robots") or None,
        "lang": lang or None,
        "h1": _texts(soup, "h1", MAX_H1),
        "h2": _texts(soup, "h2", MAX_H2),
        "word_count": _word_count(soup),
        "links": {"internal": internal, "external": external},
        "images": {
            "count": len(images),
            "missing_alt": sum(1 for img in images if not _clean(img.get("alt"))),
        },
        "structured_data": _structured_data_types(soup),
        "open_graph": {
            "title": _meta(soup, "og:title") or None,
            "description": _meta(soup, "og:description") or None,
            "image": _absolute(_meta(soup, "og:image"), base_url),
        },
        "hreflang": _hreflang(soup, base_url),
    }


def reader_view(soup: BeautifulSoup, base_url: str, *, title: str = "") -> dict:
    """Reader (contract section 4): the main content as typed blocks.

    The content root is <main> (or role=main), else a lone <article>, else
    <body>. Navigation, asides, form controls, scripts, hidden elements and
    page-level header/footer are left out. Leaves ``soup`` unchanged.
    """
    root = _content_root(soup)
    in_content = root.name in _CONTENT or root.get("role") == "main"
    builder = _ReaderBuilder()
    builder.walk(root, in_content)
    h1 = root.find("h1")
    return {
        "title": title or (_clean(_visible_text(h1, limit=MAX_HEADING))[:MAX_HEADING]
                           if h1 else "") or None,
        "blocks": builder.blocks,
        "links": _reader_links(root, base_url, in_content),
    }


# ── Reader ────────────────────────────────────────────────────────────────


class _ReaderBuilder:
    def __init__(self):
        self.blocks: list[dict] = []
        self._inline: list[str] = []

    @property
    def full(self) -> bool:
        return len(self.blocks) >= MAX_READER_BLOCKS

    def add(self, block: dict) -> None:
        if not self.full:
            self.blocks.append(block)

    def flush(self) -> None:
        text = _clean("".join(self._inline))
        self._inline = []
        if text:
            self.add({"type": "paragraph", "text": text[:MAX_TEXT]})

    def walk(self, root: Tag, in_content: bool) -> None:
        stack = [(iter(root.children), in_content)]
        while stack and not self.full:
            children, inside = stack[-1]
            child = next(children, None)
            if child is None:
                stack.pop()
                self.flush()
                continue
            if isinstance(child, _NOT_TEXT):
                continue
            if isinstance(child, NavigableString):
                self._inline.append(str(child))
                continue
            if not isinstance(child, Tag) or _skipped(child, inside):
                continue
            if self._element(child):
                # A block container (div, section, li, td, ...): its loose
                # text becomes paragraphs around its block children.
                self.flush()
                stack.append((iter(child.children), inside or child.name in _CONTENT))
        self.flush()

    def _element(self, el: Tag) -> bool:
        """Add ``el``'s block; True when it is a container to descend into."""
        name = el.name
        if name in _INLINE:
            self._inline.append(_visible_text(el, limit=MAX_TEXT))
        elif name == "br":
            self._inline.append(" ")
        elif name == "img":
            pass
        elif name in _HEADINGS:
            self.flush()
            text = _clean(_visible_text(el, limit=MAX_HEADING))
            if text:
                self.add({"type": "heading", "level": _HEADINGS[name], "text": text[:MAX_HEADING]})
        elif name in ("p", "blockquote"):
            self.flush()
            text = _clean(_visible_text(el, limit=MAX_TEXT))
            if text:
                kind = "paragraph" if name == "p" else "quote"
                self.add({"type": kind, "text": text[:MAX_TEXT]})
        elif name in ("ul", "ol"):
            items = [li for li in el.find_all("li", recursive=False) if not _skipped(li, True)]
            if not items:
                return True
            self.flush()
            texts = [_clean(_visible_text(li, limit=MAX_TEXT))[:MAX_TEXT]
                     for li in items[:MAX_LIST_ITEMS]]
            texts = [t for t in texts if t]
            if texts:
                self.add({"type": "list", "ordered": name == "ol", "items": texts})
        elif name == "pre":
            self.flush()
            text = _strip_nul(_visible_text(el, keep_space=True, limit=MAX_CODE)).strip("\n")
            if text.strip():
                self.add({"type": "code", "text": text[:MAX_CODE]})
        elif name == "table" and el.find(_LAYOUT_MARKERS) is None:
            self.flush()
            rows = _table_rows(el)
            if rows:
                self.add({"type": "table", "rows": rows})
        else:
            return True
        return False


def _content_root(soup: BeautifulSoup) -> Tag:
    body = soup.find("body")
    scope = body if isinstance(body, Tag) else soup
    main = scope.find("main") or scope.find(attrs={"role": "main"})
    if isinstance(main, Tag):
        return main
    articles = scope.find_all("article", limit=2)
    if len(articles) == 1:
        return articles[0]
    return scope


def _skipped(el: Tag, in_content: bool) -> bool:
    if el.name in _DROP:
        return True
    if el.name in _CHROME and not in_content:
        return True
    if el.has_attr("hidden") or str(el.get("aria-hidden", "")).lower() == "true":
        return True
    style = str(el.get("style", "")).replace(" ", "").lower()
    return "display:none" in style or "visibility:hidden" in style


def _visible_text(el: Tag, *, keep_space: bool = False, limit: int | None = None,
                  max_nodes: int | None = None) -> str:
    """Text of ``el`` without its dropped or hidden descendants. Inline
    markup joins as written; block elements are separated by a space.

    With ``limit`` the walk stops once the text holds that many characters
    besides whitespace, so the text cleaned and cut to ``limit`` is the
    same as if the whole element had been read. ``max_nodes`` stops it
    after that many nodes."""
    parts: list[str] = []
    found = 0
    nodes = 0
    stack = [(iter(el.children), False)]
    while stack:
        if (limit is not None and found >= limit) or (max_nodes is not None and nodes >= max_nodes):
            break
        children, is_block = stack[-1]
        child = next(children, None)
        if child is None:
            stack.pop()
            if is_block and not keep_space:
                parts.append(" ")
            continue
        nodes += 1
        if isinstance(child, _NOT_TEXT):
            continue
        if isinstance(child, NavigableString):
            text = str(child)
            parts.append(text)
            if limit is not None:
                found += len(_SPACE.sub("", _strip_nul(text)))
            continue
        if not isinstance(child, Tag) or _skipped(child, True):
            continue
        if child.name == "br":
            parts.append("\n" if keep_space else " ")
            continue
        block = child.name not in _INLINE and not keep_space
        if block:
            parts.append(" ")
        stack.append((iter(child.children), block))
    return "".join(parts)


def _table_rows(table: Tag) -> list[list[str]]:
    rows = []
    for tr in table.find_all("tr"):
        cells = [
            _clean(_visible_text(cell, limit=MAX_CELL))[:MAX_CELL]
            for cell in tr.find_all(["th", "td"], recursive=False)[:MAX_TABLE_COLS]
        ]
        if any(cells):
            rows.append(cells)
        if len(rows) >= MAX_TABLE_ROWS:
            break
    return rows


def _reader_links(root: Tag, base_url: str, in_content: bool) -> list[dict]:
    """Links in the content (not in skipped parts), deduplicated, page-local
    anchors left out."""
    page = urldefrag(base_url)[0]
    seen: set[str] = set()
    links: list[dict] = []
    stack = [(iter(root.children), in_content)]
    while stack and len(links) < MAX_READER_LINKS:
        children, inside = stack[-1]
        child = next(children, None)
        if child is None:
            stack.pop()
            continue
        if not isinstance(child, Tag) or _skipped(child, inside):
            continue
        if child.name == "a" and child.has_attr("href"):
            href = _absolute(child["href"], base_url)
            if href and urldefrag(href)[0] != page and href not in seen:
                seen.add(href)
                text = (
                    _clean(_visible_text(child, limit=MAX_HEADING))
                    or _clean(child.get("aria-label"))
                    or _clean(child.get("title"))
                )
                links.append({"text": (text or href)[:MAX_HEADING], "href": href})
            continue
        stack.append((iter(child.children), inside or child.name in _CONTENT))
    return links


# ── SEO helpers ───────────────────────────────────────────────────────────


def _page_title(soup: BeautifulSoup) -> str:
    for el in soup.find_all("title", limit=20):
        if el.find_parent("svg") is None:
            return _clean(el.get_text(" "))
    return ""


def _meta(soup: BeautifulSoup, key: str) -> str:
    """Content of <meta name=key> or <meta property=key> (case-insensitive)."""
    key = key.lower()
    for el in soup.find_all("meta"):
        for attr in ("name", "property"):
            if str(el.get(attr, "")).strip().lower() == key:
                return _clean(el.get("content"))
    return ""


def _rels(el: Tag) -> set[str]:
    rel = el.get("rel") or []
    if isinstance(rel, str):
        rel = rel.split()
    return {str(r).lower() for r in rel}


def _link_href(soup: BeautifulSoup, rel: str, base_url: str) -> str | None:
    for el in soup.find_all("link", href=True):
        if rel in _rels(el):
            return _absolute(el["href"], base_url)
    return None


def _hreflang(soup: BeautifulSoup, base_url: str) -> list[dict]:
    out = []
    for el in soup.find_all("link", href=True, hreflang=True):
        if "alternate" not in _rels(el):
            continue
        href = _absolute(el["href"], base_url)
        lang = _clean(el.get("hreflang"))
        if href and lang:
            out.append({"lang": lang, "url": href})
        if len(out) >= MAX_HREFLANG:
            break
    return out


def _structured_data_types(soup: BeautifulSoup) -> list[str]:
    """JSON-LD @type values, top level and @graph, in document order."""
    types: list[str] = []

    def add(node) -> None:
        if not isinstance(node, dict):
            return
        raw = node.get("@type")
        for value in raw if isinstance(raw, list) else [raw]:
            if isinstance(value, str) and value.strip():
                name = value.strip().rsplit("/", 1)[-1]
                if name not in types:
                    types.append(name)

    for el in soup.find_all("script"):
        if str(el.get("type", "")).split(";")[0].strip().lower() != "application/ld+json":
            continue
        text = el.string or ""
        if not text.strip() or len(text) > MAX_JSON_LD_CHARS:
            continue
        try:
            doc = json.loads(text)
        except (ValueError, RecursionError):
            continue
        for node in doc if isinstance(doc, list) else [doc]:
            add(node)
            graph = node.get("@graph") if isinstance(node, dict) else None
            for item in graph if isinstance(graph, list) else []:
                add(item)
    return types[:MAX_STRUCTURED_TYPES]


def _word_count(soup: BeautifulSoup) -> int:
    """Words in the body text (scripts and styles excluded), counting only
    tokens with a letter or digit, so stray punctuation is not a word."""
    body = soup.find("body")
    scope = body if isinstance(body, Tag) else soup
    count = 0
    stack = [iter(scope.children)]
    while stack:
        child = next(stack[-1], None)
        if child is None:
            stack.pop()
        elif isinstance(child, _NOT_TEXT):
            continue
        elif isinstance(child, NavigableString):
            count += sum(1 for token in str(child).split() if any(c.isalnum() for c in token))
        elif isinstance(child, Tag) and child.name not in _NO_WORDS:
            stack.append(iter(child.children))
    return count


def _texts(soup: BeautifulSoup, name: str, limit: int) -> list[str]:
    out = []
    for el in soup.find_all(name, limit=limit * 3):
        text = _clean(_visible_text(el, limit=MAX_HEADING, max_nodes=MAX_HEADING_NODES))
        if text:
            out.append(text[:MAX_HEADING])
        if len(out) >= limit:
            break
    return out


# ── Shared helpers ────────────────────────────────────────────────────────


def _absolute(href, base_url: str) -> str | None:
    """``href`` resolved against the page, http(s) only, else None."""
    href = _strip_nul(str(href or "")).strip()
    if not href:
        return None
    try:
        resolved = urljoin(base_url, href)
        scheme = urlsplit(resolved).scheme.lower()
    except ValueError:
        return None
    return resolved if scheme in ("http", "https") else None


def _clean(value) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        value = " ".join(str(v) for v in value)
    return _SPACE.sub(" ", _strip_nul(str(value))).strip()


def _strip_nul(text: str) -> str:
    # Postgres JSONB rejects \u0000, and results end up in JSON columns.
    return text.replace("\x00", "")


def _charset(content_type: str | None) -> str | None:
    match = _CHARSET.search(content_type or "")
    return match.group(1) if match else None


def _looks_like_html(body: bytes) -> bool:
    return bool(_HTML_START.match(body[:2048].lstrip(b"\xef\xbb\xbf")))


def _too_many_tags(markup: str) -> bool:
    if markup.count("<") <= MAX_TAGS:
        return False
    return any(n >= MAX_TAGS for n, _ in enumerate(_TAG.finditer(markup)))


def _now() -> str:
    return timezone.now().isoformat().replace("+00:00", "Z")


def _failed(url: str, code: str, *, final_url: str | None = None, status_code=None) -> dict:
    return {
        "url": url,
        "final_url": final_url or url,
        "status_code": status_code,
        "frameable": False,
        "title": None,
        "captured_at": _now(),
        "seo": None,
        "reader": None,
        "error": {"code": code, "message": ERROR_MESSAGES.get(code, ERROR_MESSAGES["fetch_failed"])},
    }
