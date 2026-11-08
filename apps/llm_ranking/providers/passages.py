"""Cited passages: which span of an answer each web source grounds.

Engines that search the web report, next to their answer, which source
backs which part of it:

- Claude: every text block carries ``citations`` with the page ``url``,
  ``title`` and ``cited_text`` (the passage it quoted); the block itself is
  the span of the answer it grounds.
- OpenAI / xAI (Responses API): ``url_citation`` annotations with
  ``start_index``/``end_index`` into the output text, plus ``url``/``title``.
- Perplexity: ``[n]`` markers in the answer refer to ``search_results[n-1]``
  (``url``, ``title``, ``snippet``).

Gemini is left out on purpose: its grounding terms exclude the links from
what may be stored (core.utils.retention.grounded_links_may_be_stored).

Every helper here returns the same list shape, ready for
``LLMRankingResult.citation_passages``:
``[{"url", "title", "cited_text", "answer_start", "answer_end"}]`` with
offsets into the final (stripped) answer text. All readers tolerate typed
SDK objects and plain dicts, and never raise on odd input.
"""
from __future__ import annotations

import re

MAX_PASSAGES = 40
MAX_CITED_TEXT = 1000
MAX_TITLE = 300
MAX_URL = 2000

_MARKER = re.compile(r"\[(\d{1,2})\]")
# A sentence ends at . ! ? (with any [n] markers right after it, where
# Perplexity puts them) before whitespace or the end, or at a line break; a
# list item is its own sentence.
_BOUNDARY = re.compile(r"[.!?](?:\[\d{1,2}\])*(?=\s|$)|\n")


def _get(item, key, default=None):
    if isinstance(item, dict):
        return item.get(key, default)
    return getattr(item, key, default)


def _str(value, limit: int) -> str:
    return value[:limit] if isinstance(value, str) else ""


def clean_passages(items, text: str) -> list[dict]:
    """Valid passages only: a URL, in-bounds offsets with start < end, at
    most MAX_PASSAGES, strings clipped, duplicates (same url and span)
    dropped. Anything else in the list is ignored."""
    out: list[dict] = []
    seen: set[tuple] = set()
    length = len(text or "")
    for item in items or []:
        if not isinstance(item, dict):
            continue
        url = _str(item.get("url"), MAX_URL).strip()
        start, end = item.get("answer_start"), item.get("answer_end")
        if not url or not isinstance(start, int) or not isinstance(end, int):
            continue
        if isinstance(start, bool) or isinstance(end, bool):
            continue
        if not 0 <= start < end <= length:
            continue
        key = (url, start, end)
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "url": url,
            "title": _str(item.get("title"), MAX_TITLE).strip(),
            "cited_text": " ".join(_str(item.get("cited_text"), MAX_CITED_TEXT).split()),
            "answer_start": start,
            "answer_end": end,
        })
        if len(out) >= MAX_PASSAGES:
            break
    return out


def _shift(passages: list[dict], raw: str) -> tuple[str, list[dict]]:
    """Strip ``raw`` the way providers store the answer and move offsets
    with it, clipping spans to the stripped text."""
    lead = len(raw) - len(raw.lstrip())
    text = raw.strip()
    moved = []
    for p in passages:
        start = max(p["answer_start"] - lead, 0)
        end = min(p["answer_end"] - lead, len(text))
        if start < end:
            moved.append({**p, "answer_start": start, "answer_end": end})
    return text, moved


def claude_passages(content) -> tuple[str, list[dict]]:
    """(answer text, passages) from a Claude message's content blocks.

    The text blocks are concatenated verbatim, as the provider stores the
    answer; each block's citations ground that block's span."""
    raw = ""
    found: list[dict] = []
    for block in content or []:
        if _get(block, "type", "") != "text":
            continue
        piece = _get(block, "text", "") or ""
        if not isinstance(piece, str) or not piece:
            continue
        start = len(raw)
        raw += piece
        # Spans exclude the whitespace a block starts or ends with.
        lead = len(piece) - len(piece.lstrip())
        trail = len(piece) - len(piece.rstrip())
        span = (start + lead, len(raw) - trail)
        if span[0] >= span[1]:
            continue
        for cite in _get(block, "citations", None) or []:
            found.append({
                "url": _get(cite, "url"),
                "title": _get(cite, "title"),
                "cited_text": _get(cite, "cited_text"),
                "answer_start": span[0],
                "answer_end": span[1],
            })
    text, moved = _shift(found, raw)
    return text, clean_passages(moved, text)


def responses_passages(resp) -> list[dict]:
    """Passages from a Responses API response (OpenAI, xAI).

    ``output_text`` is the concatenation of every ``output_text`` content
    part, so each part's annotation indices are shifted by the length of
    the parts before it."""
    raw = ""
    found: list[dict] = []
    for item in _get(resp, "output", None) or []:
        if _get(item, "type") != "message":
            continue
        for part in _get(item, "content", None) or []:
            if _get(part, "type") != "output_text":
                continue
            piece = _get(part, "text", "") or ""
            if not isinstance(piece, str):
                continue
            base = len(raw)
            raw += piece
            for ann in _get(part, "annotations", None) or []:
                if _get(ann, "type") != "url_citation":
                    continue
                start, end = _get(ann, "start_index"), _get(ann, "end_index")
                if not isinstance(start, int) or not isinstance(end, int):
                    continue
                found.append({
                    "url": _get(ann, "url"),
                    "title": _get(ann, "title"),
                    "cited_text": "",
                    "answer_start": base + start,
                    "answer_end": base + end,
                })
    text, moved = _shift(found, raw)
    # The stored answer is resp.output_text stripped; when the SDK's
    # output_text differs from the joined parts, offsets cannot be trusted.
    output_text = _get(resp, "output_text", None)
    if isinstance(output_text, str) and output_text.strip() != text:
        return []
    return clean_passages(moved, text)


def marker_passages(text: str, sources) -> list[dict]:
    """Passages from ``[n]`` markers (Perplexity): the sentence holding a
    marker is grounded by ``sources[n-1]`` (dicts with url/title/snippet)."""
    text = text or ""
    if not text or not sources:
        return []
    spans = []
    start = 0
    for m in _BOUNDARY.finditer(text):
        end = m.start() if m.group(0) == "\n" else m.end()
        spans.append((start, end))
        start = m.end()
    spans.append((start, len(text)))

    found: list[dict] = []
    for s_start, s_end in spans:
        sentence = text[s_start:s_end]
        if not sentence.strip():
            continue
        numbers = {int(n) for n in _MARKER.findall(sentence)}
        if not numbers:
            continue
        # The grounded span is the sentence without its trailing markers.
        body_end = s_end
        while True:
            trimmed = text[s_start:body_end].rstrip()
            m = re.search(r"(\[\d{1,2}\])+$", trimmed)
            if not m:
                body_end = s_start + len(trimmed)
                break
            body_end = s_start + m.start()
        lead = len(sentence) - len(sentence.lstrip())
        a_start = s_start + lead
        if a_start >= body_end:
            continue
        for n in sorted(numbers):
            if not 1 <= n <= len(sources):
                continue
            src = sources[n - 1]
            if not isinstance(src, dict):
                continue
            found.append({
                "url": src.get("url"),
                "title": src.get("title"),
                "cited_text": src.get("snippet"),
                "answer_start": a_start,
                "answer_end": body_end,
            })
    return clean_passages(found, text)
