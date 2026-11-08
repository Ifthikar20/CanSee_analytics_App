"""Find a quoted passage in a text and return its exact position.

Quotes come back from a language model, so they rarely match byte for byte:
whitespace differs, Markdown emphasis markers are left out, curly quotes
become straight ones. ``locate`` matches on a normalized form of both
strings and maps the match back to offsets in the ORIGINAL text, so the UI
can highlight exactly the characters that are there.
"""
from __future__ import annotations

import html

# Characters dropped before comparing: Markdown emphasis and inline code.
_DROP = frozenset("*`")
_TRANSLATE = {
    "‘": "'", "’": "'", "‚": "'", "‛": "'",
    "“": '"', "”": '"', "„": '"', "‟": '"',
    "–": "-", "—": "-", "−": "-",
    " ": " ", " ": " ", " ": " ",
    "…": "...",
}


def _normalize(text: str) -> tuple[str, list[int]]:
    """(normalized text, index map): ``index_map[i]`` is the position in
    ``text`` of normalized character ``i``. Whitespace runs collapse to one
    space, case is folded, dropped characters leave no trace."""
    out: list[str] = []
    index_map: list[int] = []
    last_space = True  # also strips leading whitespace
    for pos, ch in enumerate(text or ""):
        if ch in _DROP:
            continue
        ch = _TRANSLATE.get(ch, ch)
        if ch.isspace():
            if last_space:
                continue
            out.append(" ")
            index_map.append(pos)
            last_space = True
            continue
        for piece in ch.lower():
            out.append(piece)
            index_map.append(pos)
        last_space = False
    while out and out[-1] == " ":
        out.pop()
        index_map.pop()
    return "".join(out), index_map


def _needle(quote: str) -> str:
    return _normalize(quote)[0]


def locate(quote: str, text: str, *, start: int = 0) -> tuple[int, int] | None:
    """``(start, end)`` of ``quote`` in ``text`` (case, whitespace and
    Markdown emphasis aside), or None. ``text[start:end]`` is the matched
    passage as it appears in ``text``. Searching begins at offset ``start``.
    A quote copied from HTML-escaped input (``&amp;``) is matched unescaped.
    """
    if not quote or not text:
        return None
    hay, index_map = _normalize(text)
    if not hay:
        return None
    # First normalized position at or after ``start`` in the original text.
    begin = 0
    while begin < len(index_map) and index_map[begin] < start:
        begin += 1
    for candidate in (_needle(quote), _needle(html.unescape(quote))):
        if len(candidate) < 3:
            continue
        at = hay.find(candidate, begin)
        if at < 0:
            continue
        first = index_map[at]
        last = index_map[at + len(candidate) - 1]
        return first, last + 1
    return None


def block_texts(blocks) -> list[tuple[int, int | None, str]]:
    """``(block index, list item index or None, text)`` for every piece of
    readable text in browse reader blocks, in page order. Tables are read
    cell by cell as rows joined with " | " (their item index is the row)."""
    out: list[tuple[int, int | None, str]] = []
    for b_index, block in enumerate(blocks or []):
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "list":
            for i_index, item in enumerate(block.get("items") or []):
                if isinstance(item, str) and item:
                    out.append((b_index, i_index, item))
        elif kind == "table":
            for r_index, row in enumerate(block.get("rows") or []):
                if isinstance(row, list):
                    out.append((b_index, r_index, " | ".join(str(c) for c in row)))
        else:
            text = block.get("text")
            if isinstance(text, str) and text:
                out.append((b_index, None, text))
    return out


def locate_in_blocks(quote: str, blocks, *, prefer_block: int | None = None) -> dict | None:
    """Anchor of ``quote`` in reader blocks: ``{"block", "item", "start",
    "end", "text"}`` where ``text`` is the exact matched passage, or None.

    A quote must sit inside one block (or one list item). ``prefer_block``
    is tried first, so a model that names the block it quoted wins when
    the same sentence appears twice on a page."""
    pieces = block_texts(blocks)
    if prefer_block is not None:
        pieces = sorted(pieces, key=lambda p: p[0] != prefer_block)
    for b_index, i_index, text in pieces:
        if pieces and block_kind(blocks, b_index) == "table":
            continue
        span = locate(quote, text)
        if span is not None:
            return {
                "block": b_index,
                "item": i_index,
                "start": span[0],
                "end": span[1],
                "text": text[span[0]:span[1]],
            }
    return None


def block_kind(blocks, index: int) -> str:
    try:
        return str(blocks[index].get("type") or "")
    except (IndexError, AttributeError, TypeError):
        return ""
