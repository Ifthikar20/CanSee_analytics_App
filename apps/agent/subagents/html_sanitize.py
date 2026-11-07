"""Server-side cleaning of generated landing pages.

The page is previewed in a sandboxed iframe with no script rights, but
it is also downloadable and shared with the project's members, so the
stored HTML itself must run nothing and load nothing: no scripts (except
JSON-LD structured data), frames, plugins, base, meta refresh, form
actions, pings, event handlers, javascript: or data: URLs (data: images
are fine) and no external resources except Google Fonts.

html.parser reads <style> and <script> as raw text everywhere, but a
browser parses them as markup inside <svg> and <math>, and ends a
CDATA section outside them at the first ">". So the text of every kept
style and JSON-LD script is escaped where it could open a tag, and
comments, CDATA sections, processing instructions and declarations are
removed. URLs are checked as a browser reads them (tabs and newlines
dropped, backslashes as slashes) and CSS after its escapes are decoded.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from bs4 import BeautifulSoup, CData, Comment, Declaration, Doctype, ProcessingInstruction

MAX_BYTES = 400 * 1024
FONT_HOSTS = ("fonts.googleapis.com", "fonts.gstatic.com")
_DROP_TAGS = ("iframe", "frame", "frameset", "object", "embed", "applet", "base", "portal")
_URL_ATTRS = ("href", "src", "action", "formaction", "poster", "background", "cite", "data",
              "xlink:href", "srcset", "imagesrcset", "ping", "longdesc", "manifest", "codebase",
              "lowsrc")
_RESOURCE_ATTRS = ("src", "srcset", "imagesrcset", "poster", "background", "data", "lowsrc",
                   "codebase")
# Schemes a browser fetches over the network.
_NETWORK_SCHEMES = ("http", "https", "ftp", "ws", "wss")
_CSS_URL = re.compile(r"url\(\s*(['\"]?)(.*?)\1\s*(?:\)|\Z)", re.IGNORECASE | re.DOTALL)
_CSS_IMPORT = re.compile(
    r"""@import\s+(?:url\(\s*(?P<q1>['"]?)(?P<u1>.*?)(?P=q1)\s*\)|(?P<q2>['"])(?P<u2>.*?)(?P=q2))"""
    r"""[^;]*;?""",
    re.IGNORECASE | re.DOTALL,
)
# CSS functions that load an image from a string argument (url() aside).
_CSS_IMAGE_FUNCTION = re.compile(
    r"(?<![\w-])(?:-webkit-)?(?:image-set|cross-fade|image|src)\(", re.IGNORECASE,
)
_CSS_STRING = re.compile(r"""(['"])(.*?)\1""", re.DOTALL)
_CSS_ESCAPE = re.compile(r"\\(?:([0-9a-fA-F]{1,6})[ \t\n\r\f]?|(.))", re.DOTALL)
_CSS_EXPRESSION = re.compile(r"expression\s*\(|behavior\s*:|-moz-binding", re.IGNORECASE)
# A "<" that would open a tag, comment or declaration where text is markup.
_MARKUP_OPEN = re.compile(r"<(?=[A-Za-z!/?])")
_FENCE = re.compile(r"^\s*```[a-zA-Z]*\s*\n(.*?)\n?```\s*$", re.DOTALL)


@dataclass
class SanitizeResult:
    html: str
    warnings: list[dict] = field(default_factory=list)
    error: str | None = None


def _browser_url(url: str) -> str:
    """``url`` as a browser reads it: tabs and newlines removed, outer
    spaces and control characters stripped, backslashes as slashes."""
    value = re.sub(r"[\t\n\r]", "", url or "")
    return value.strip("".join(chr(c) for c in range(0x21))).replace("\\", "/")


def _host(url: str) -> str:
    try:
        return (urlsplit(_browser_url(url)).hostname or "").lower()
    except ValueError:
        return ""


def _is_font_url(url: str) -> bool:
    return _host(url) in FONT_HOSTS


def _scheme(url: str) -> str:
    cleaned = re.sub(r"[\x00-\x20]", "", url or "").lower()
    match = re.match(r"^([a-z][a-z0-9+.\-]*):", cleaned)
    return match.group(1) if match else ""


def _is_external(url: str) -> bool:
    value = _browser_url(url).lower()
    return value.startswith("//") or _scheme(value) in _NETWORK_SCHEMES


def _css_unescape(css: str) -> str:
    """CSS with its escapes decoded, as a browser's tokenizer reads it."""
    def replace(match):
        if match.group(1):
            code = int(match.group(1), 16)
            return chr(code) if 0 < code <= 0x10FFFF and not 0xD800 <= code <= 0xDFFF else "�"
        return "" if match.group(2) == "\n" else match.group(2)

    return _CSS_ESCAPE.sub(replace, css)


def _css_target_ok(target: str) -> bool:
    target = target.strip()
    return (target.lower().startswith("data:image/") or target.startswith("#")
            or _is_font_url(target))


def _function_end(css: str, start: int) -> int:
    """The index after the ")" closing the function whose arguments start
    at ``start``; the end of ``css`` when it is never closed."""
    depth, quote, i = 1, "", start
    while i < len(css):
        ch = css[i]
        if quote:
            if ch == quote:
                quote = ""
        elif ch in "\"'":
            quote = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return len(css)


def _clean_css_text(css: str, warn) -> str:
    def replace_import(match):
        if _is_font_url(match.group("u1") or match.group("u2") or ""):
            return match.group(0)
        warn("style", "Removed a CSS @import that is not Google Fonts.")
        return ""

    def replace_url(match):
        if _css_target_ok(match.group(2)):
            return match.group(0)
        warn("style", "Removed a CSS url() that loads an external or unsafe resource.")
        return "none"

    css = _CSS_IMPORT.sub(replace_import, css)
    out, pos = [], 0
    for match in _CSS_IMAGE_FUNCTION.finditer(css):
        if match.start() < pos:
            continue
        end = _function_end(css, match.end())
        call = css[match.start():end]
        if all(_css_target_ok(s.group(2)) for s in _CSS_STRING.finditer(call)):
            continue
        warn("style", "Removed a CSS image that loads an external or unsafe resource.")
        out.append(css[pos:match.start()] + "none")
        pos = end
    css = "".join(out) + css[pos:]
    css = _CSS_URL.sub(replace_url, css)
    if _CSS_EXPRESSION.search(css):
        warn("style", "Removed CSS that can run code.")
        css = _CSS_EXPRESSION.sub("", css)
    return css


def _clean_css(css: str, warn) -> str:
    """``css`` without external loads and code. The checks read the CSS with
    its escapes decoded; CSS whose escapes hide nothing is kept as written."""
    decoded = _css_unescape(css)
    cleaned = _clean_css_text(decoded, warn)
    return css if cleaned == decoded else cleaned


def _escape_markup(text: str) -> str:
    """Raw text with every "<" that could open a tag as the CSS escape \\3c."""
    return _MARKUP_OPEN.sub(r"\\3c ", text)


def _escape_json(text: str) -> str:
    """JSON with <, > and & as \\u escapes: the same data, no markup."""
    return text.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def sanitize_html(raw: str) -> SanitizeResult:
    warnings: list[dict] = []
    seen: set[tuple[str, str]] = set()

    def warn(path: str, message: str) -> None:
        if (path, message) not in seen:
            seen.add((path, message))
            warnings.append({"path": path, "message": message})

    text = (raw or "").strip()
    fenced = _FENCE.match(text)
    if fenced:
        text = fenced.group(1).strip()
        warn("document", "Removed a Markdown code fence around the page.")
    if "</html>" not in text.lower():
        return SanitizeResult(html="", warnings=warnings,
                              error="The page is incomplete: it does not end with </html>.")
    if not text.lower().startswith("<!doctype html"):
        warn("document", "The page does not start with <!DOCTYPE html>.")

    soup = BeautifulSoup(text, "html.parser")
    # Comments, CDATA sections, processing instructions and declarations
    # (a browser ends some of them at a different place than html.parser).
    hidden = (Comment, CData, Declaration, ProcessingInstruction)
    for node in soup.find_all(string=lambda s: isinstance(s, hidden)):
        if not isinstance(node, Comment):
            warn("document", "Removed a CDATA section, declaration or processing instruction.")
        node.extract()

    for tag in list(soup.find_all(True)):
        if tag.decomposed:
            continue
        name = tag.name.lower()
        if name == "script":
            if (tag.get("type") or "").strip().lower() == "application/ld+json":
                body = tag.string or ""
                try:
                    json.loads(body)
                except ValueError:
                    warn("script", "Removed structured data that is not valid JSON.")
                    tag.decompose()
                    continue
                tag.attrs = {"type": "application/ld+json"}
                if _escape_json(body) != body:
                    tag.string = _escape_json(body)
                continue
            warn("script", "Removed a script.")
            tag.decompose()
            continue
        if name in _DROP_TAGS:
            warn(name, f"Removed a <{name}> element.")
            tag.decompose()
            continue
        if name == "meta" and (tag.get("http-equiv") or "").strip().lower() in ("refresh", "set-cookie"):
            warn("meta", "Removed a meta refresh.")
            tag.decompose()
            continue
        if name == "link":
            href = tag.get("href") or ""
            rel = " ".join(tag.get("rel") or []).lower() if isinstance(tag.get("rel"), list) \
                else str(tag.get("rel") or "").lower()
            if _is_external(href) and not _is_font_url(href):
                warn("link", "Removed a link to an external resource that is not Google Fonts.")
                tag.decompose()
                continue
            if "import" in rel or _scheme(href) in ("javascript", "data", "vbscript"):
                warn("link", "Removed an unsafe link element.")
                tag.decompose()
                continue
        if name == "style":
            css = tag.string or tag.get_text()
            cleaned = _clean_css(css, warn)
            if _escape_markup(cleaned) != cleaned:
                warn("style", "Escaped markup inside a style element.")
                cleaned = _escape_markup(cleaned)
            if cleaned != css:
                tag.string = cleaned

        for attr in list(tag.attrs):
            lower = attr.lower()
            value = tag.attrs[attr]
            if lower.startswith("on"):
                del tag.attrs[attr]
                warn(f"{name}[{lower}]", "Removed an event handler attribute.")
                continue
            if lower in ("action", "formaction"):
                del tag.attrs[attr]
                warn("form", "Removed a form action.")
                continue
            if lower == "ping":
                del tag.attrs[attr]
                warn(f"{name}[ping]", "Removed a ping URL.")
                continue
            if lower == "style" and isinstance(value, str):
                cleaned = _clean_css(value, warn)
                if cleaned != value:
                    tag.attrs[attr] = cleaned
                continue
            if lower not in _URL_ATTRS:
                continue
            values = value if isinstance(value, list) else [value]
            joined = " ".join(str(v) for v in values)
            scheme = _scheme(joined)
            if scheme in ("javascript", "vbscript"):
                del tag.attrs[attr]
                warn(f"{name}[{lower}]", "Removed a javascript: URL.")
                continue
            if scheme == "data" and not (
                lower in _RESOURCE_ATTRS and joined.strip().lower().startswith("data:image/")
            ):
                del tag.attrs[attr]
                warn(f"{name}[{lower}]", "Removed a data: URL that is not an image.")
                continue
            if lower in _RESOURCE_ATTRS or (lower in ("href", "xlink:href") and name in (
                "image", "use", "feimage",
            )):
                if lower in ("srcset", "imagesrcset"):
                    if any(_is_external(c.strip().split(" ")[0]) for c in joined.split(",")):
                        del tag.attrs[attr]
                        warn(f"{name}[{lower}]", "Removed external images.")
                    continue
                if _is_external(joined):
                    del tag.attrs[attr]
                    warn(f"{name}[{lower}]", "Removed an external resource.")

    html = str(soup)
    if not any(isinstance(node, Doctype) for node in soup.contents):
        html = "<!DOCTYPE html>\n" + html
    if len(html.encode("utf-8")) > MAX_BYTES:
        return SanitizeResult(html="", warnings=warnings,
                              error="The page is larger than 400 KB.")
    return SanitizeResult(html=html, warnings=warnings)


def summarize_html(html: str) -> str:
    """A deterministic one-line summary: headings and section counts."""
    soup = BeautifulSoup(html or "", "html.parser")
    h1 = soup.find("h1")
    sections = len(soup.find_all("section")) or len(soup.find_all("h2"))
    faq = sum(1 for s in soup.find_all("script", type="application/ld+json")
              if "FAQPage" in (s.string or ""))
    title = h1.get_text(" ", strip=True) if h1 else (soup.title.string if soup.title else "")
    parts = [f"H1: {title}"[:200] if title else "No H1"]
    parts.append(f"{sections} sections")
    parts.append("FAQ structured data" if faq else "no FAQ structured data")
    return "; ".join(parts)
