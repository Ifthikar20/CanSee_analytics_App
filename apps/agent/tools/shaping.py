"""Shaping tool results: compact JSON, capped text, third-party text as data."""
from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal
from urllib.parse import urlsplit
from uuid import UUID


def _default(value):
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, UUID):
        return str(value)
    return str(value)


def compact(value) -> str:
    """Deterministic, compact JSON for a tool_result."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      default=_default)


def clip(text, limit: int) -> str:
    text = str(text or "")
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def untrusted(source: str, text, limit: int) -> dict:
    """Third-party text, marked so the model treats it as data."""
    return {"untrusted": True, "source": source, "text": clip(text, limit)}


def host_of(url: str) -> str:
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def display_url(url: str) -> str:
    """Google-style breadcrumb of a result URL: host › path › segments."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    host = (parts.hostname or "").lower()
    segments = [s for s in (parts.path or "").split("/") if s][:3]
    return " › ".join([f"{parts.scheme}://{host}" if parts.scheme else host, *segments])
