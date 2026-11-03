"""Cleaning and checks for generated Markdown articles.

Raw script and iframe HTML is removed and links that are not http(s)
become plain text. The structure checks (an H1, 300 to 5000 words, an
FAQ) only add warnings: the article is still saved.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

MIN_WORDS = 300
MAX_WORDS = 5000

_FENCE = re.compile(r"^\s*```(?:markdown|md)?\s*\n(.*?)\n?```\s*$", re.DOTALL | re.IGNORECASE)
_BLOCK_TAGS = re.compile(
    r"<\s*(script|iframe|object|embed|style)\b[^>]*>.*?<\s*/\s*\1\s*>", re.IGNORECASE | re.DOTALL,
)
_LONE_TAGS = re.compile(r"<\s*/?\s*(script|iframe|object|embed|style)\b[^>]*>", re.IGNORECASE)
_LINK = re.compile(r"(!?)\[([^\]]*)\]\(\s*<?([^)\s>]*)>?(?:\s+\"[^\"]*\")?\s*\)")
_AUTOLINK = re.compile(r"<([a-zA-Z][a-zA-Z0-9+.\-]*:[^>\s]*)>")
_H1 = re.compile(r"^#\s+\S", re.MULTILINE)
_FAQ = re.compile(r"^#{2,3}\s+.*(faq|frequently asked)", re.IGNORECASE | re.MULTILINE)
_HEADING = re.compile(r"^(#{1,3})\s+(.+?)\s*#*\s*$", re.MULTILINE)


@dataclass
class MarkdownResult:
    markdown: str
    warnings: list[dict] = field(default_factory=list)


def _http(url: str) -> bool:
    return url.lower().startswith(("http://", "https://"))


def word_count(markdown: str) -> int:
    text = re.sub(r"[#>*_`\[\]()|-]", " ", markdown)
    return len([w for w in text.split() if any(ch.isalnum() for ch in w)])


def check_markdown(raw: str) -> MarkdownResult:
    warnings: list[dict] = []
    text = (raw or "").strip()
    fenced = _FENCE.match(text)
    if fenced:
        text = fenced.group(1).strip()
        warnings.append({"path": "document", "message": "Removed a code fence around the article."})

    cleaned = _BLOCK_TAGS.sub("", text)
    cleaned = _LONE_TAGS.sub("", cleaned)
    if cleaned != text:
        warnings.append({"path": "html", "message": "Removed raw script, iframe or style HTML."})
    text = cleaned

    dropped = 0

    def replace_link(match):
        nonlocal dropped
        bang, label, url = match.group(1), match.group(2), match.group(3)
        if _http(url):
            return match.group(0)
        dropped += 1
        return "" if bang else label

    def replace_autolink(match):
        nonlocal dropped
        url = match.group(1)
        if _http(url):
            return match.group(0)
        dropped += 1
        return ""

    text = _LINK.sub(replace_link, text)
    text = _AUTOLINK.sub(replace_autolink, text)
    if dropped:
        warnings.append({"path": "links",
                         "message": f"Removed {dropped} link(s) that were not http or https."})

    if not _H1.search(text):
        warnings.append({"path": "structure", "message": "The article has no H1."})
    words = word_count(text)
    if words < MIN_WORDS or words > MAX_WORDS:
        warnings.append({"path": "length",
                         "message": f"The article has {words} words; aim for 300 to 5000."})
    if not _FAQ.search(text):
        warnings.append({"path": "structure", "message": "The article has no FAQ section."})
    return MarkdownResult(markdown=text, warnings=warnings)


def summarize_markdown(markdown: str) -> str:
    """A deterministic one-line summary: the H1, section and FAQ counts, words."""
    headings = _HEADING.findall(markdown or "")
    h1 = next((t for level, t in headings if level == "#"), "")
    h2 = sum(1 for level, _ in headings if level == "##")
    faq_q = 0
    in_faq = False
    for level, title in headings:
        if level == "##":
            in_faq = bool(re.search(r"faq|frequently asked", title, re.IGNORECASE))
        elif level == "###" and in_faq:
            faq_q += 1
    parts = [f"H1: {h1}"[:200] if h1 else "No H1", f"{h2} sections"]
    if faq_q:
        parts.append(f"{faq_q} FAQ questions")
    parts.append(f"{word_count(markdown or '')} words")
    return "; ".join(parts)
