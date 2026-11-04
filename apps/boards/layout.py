"""The analytics board's widgets and layouts (board contract 3 and 6).

A layout is an ordered list of ``{key, size}``, one entry per widget on
the board. The keys are frozen and shared with the frontend
(``src/lib/board/widgets.js`` lists them identically, in catalog order).
The server checks a size only against the global set; each widget's
allowed sizes are the client's, which clamps them on read.
"""

from __future__ import annotations

import json
from datetime import UTC
from typing import TYPE_CHECKING

from core.exceptions import CanseeException

if TYPE_CHECKING:
    from apps.boards.models import BoardLayout

SIZES = ("third", "half", "full")

# Every widget key and its default size, in catalog order (contract 3; Ask AI
# and Prompt ranking first, wave 4 contract 6).
WIDGETS = {
    "prompt-box": "full",
    "ranking": "full",
    "summary": "full",
    "prompts": "full",
    "countries": "half",
    "traffic": "half",
    "visibility": "half",
    "by-engine": "third",
    "who-recommends": "third",
    "share-of-voice": "third",
    "sentiment": "third",
    "fact-check": "third",
    "citations": "half",
    "ai-traffic": "third",
    "ai-crawlers": "third",
    "crawler-access": "third",
    "pages": "half",
    "fan-outs": "half",
    "prompt-research": "half",
    "competitors": "half",
    "products": "half",
}

# Served while nothing is saved, and what Reset board restores: Ask AI and
# Prompt ranking (wave 4 contract 6). Saved layouts are not migrated. Callers
# take copies (default_widgets), never these dicts.
DEFAULT_LAYOUT = (
    {"key": "prompt-box", "size": "full"},
    {"key": "ranking", "size": "full"},
)

# An unknown key is quoted back in its error up to this many characters.
_QUOTE_CHARS = 64


def default_widgets() -> list[dict]:
    return [dict(widget) for widget in DEFAULT_LAYOUT]


def invalid(field: str, message: str) -> CanseeException:
    return CanseeException(
        message, code="invalid", status_code=400, details={"field": field, "message": message},
    )


def _quoted(value) -> str:
    text = value if isinstance(value, str) else json.dumps(value)
    return text if len(text) <= _QUOTE_CHARS else f"{text[:_QUOTE_CHARS]}..."


def read_widgets(raw) -> list[dict]:
    """A stored list as the board shows it: unknown and repeated keys are
    dropped, and a size outside SIZES becomes the key's default size. A
    key a later release retires disappears here without a migration."""
    widgets, seen = [], set()
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        key = item.get("key")
        if not isinstance(key, str) or key not in WIDGETS or key in seen:
            continue
        seen.add(key)
        size = item.get("size")
        widgets.append({"key": key, "size": size if size in SIZES else WIDGETS[key]})
    return widgets


def validate_widgets(raw) -> list[dict]:
    """The widgets of a save, in order; the first problem raises 400
    ``invalid`` with ``details: {field, message}``. An empty list is valid."""
    if not isinstance(raw, list):
        raise invalid("widgets", "Send a list of widgets.")
    widgets, seen = [], set()
    for index, item in enumerate(raw):
        field = f"widgets[{index}]"
        # Exactly {key, size}: extra fields are refused, not dropped.
        if not isinstance(item, dict) or set(item) != {"key", "size"}:
            raise invalid(field, "Each widget is {key, size}.")
        key, size = item["key"], item["size"]
        if not isinstance(key, str) or key not in WIDGETS:
            raise invalid(f"{field}.key", f"Unknown widget: {_quoted(key)}.")
        if key in seen:
            raise invalid(f"{field}.key", "Each widget can be on the board once.")
        if not isinstance(size, str) or size not in SIZES:
            raise invalid(f"{field}.size", "Choose a size of third, half or full.")
        seen.add(key)
        widgets.append({"key": key, "size": size})
    return widgets


def validate_version(raw) -> int:
    """The version a save was made from: a non-negative integer, not a
    boolean (0 while nothing is saved)."""
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
        raise invalid("version", "Send the version you loaded.")
    return raw


def iso(value) -> str | None:
    """ISO 8601 UTC with a Z, to the millisecond."""
    if value is None:
        return None
    text = value.astimezone(UTC).isoformat()
    if value.microsecond:
        text = text[:23] + text[26:]
    return text.replace("+00:00", "Z")


def payload(layout: BoardLayout | None) -> dict:
    """What GET answers (contract 6): the saved layout, or the default
    with ``saved: false`` and version 0 while nothing is saved."""
    if layout is None:
        return {"saved": False, "version": 0, "updated_at": None, "widgets": default_widgets()}
    return {
        "saved": True,
        "version": layout.version,
        "updated_at": iso(layout.updated_at),
        "widgets": read_widgets(layout.widgets),
    }
