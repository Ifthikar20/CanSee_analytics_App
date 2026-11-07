"""What one agent request cost, from its ``usage`` block.

A request bills per sampling iteration when the response carries
``usage.iterations`` (server-side fallback hops, server tool loops):
each entry is priced at its own model's rates and the top-level counts,
which add those entries up, are not priced again. Without iterations the
top-level counts are the request. Cache writes are priced per TTL from
``usage.cache_creation``; tokens the split does not cover (it arrives
only with message_start) are priced at the configured TTL. Server web
searches are counted once per request.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal

from core.ai_tracking import estimate_cost


@dataclass(frozen=True)
class UsageRow:
    """One priced unit: a whole request, or one of its iterations."""

    model: str
    kind: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read: int = 0
    cache_write_5m: int = 0
    cache_write_1h: int = 0
    web_search_requests: int = 0
    web_fetch_requests: int = 0

    @property
    def cache_write(self) -> int:
        return self.cache_write_5m + self.cache_write_1h

    def cost(self) -> Decimal:
        return Decimal(str(estimate_cost(
            self.model, self.input_tokens, self.output_tokens,
            cache_write_5m=self.cache_write_5m,
            cache_write_1h=self.cache_write_1h,
            cache_read=self.cache_read,
            web_search_requests=self.web_search_requests,
        )))


def as_dict(usage) -> dict:
    if usage is None:
        return {}
    if hasattr(usage, "to_dict"):
        return usage.to_dict(mode="json")
    return dict(usage)


def _int(value) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _cache_writes(entry: dict, default_ttl: str) -> tuple[int, int]:
    total = _int(entry.get("cache_creation_input_tokens"))
    split = entry.get("cache_creation") or {}
    w5 = _int(split.get("ephemeral_5m_input_tokens"))
    w1 = _int(split.get("ephemeral_1h_input_tokens"))
    rest = max(0, total - w5 - w1)
    if default_ttl == "5m":
        w5 += rest
    else:
        w1 += rest
    return w5, w1


def usage_rows(model: str, usage, *, default_ttl: str = "1h") -> list[UsageRow]:
    """The priced rows of one request (see module doc)."""
    u = as_dict(usage)
    server = u.get("server_tool_use") or {}
    searches = _int(server.get("web_search_requests"))
    fetches = _int(server.get("web_fetch_requests"))
    iterations = [it for it in (u.get("iterations") or []) if isinstance(it, dict)]
    if not iterations:
        w5, w1 = _cache_writes(u, default_ttl)
        return [UsageRow(
            model=model, kind="request",
            input_tokens=_int(u.get("input_tokens")),
            output_tokens=_int(u.get("output_tokens")),
            cache_read=_int(u.get("cache_read_input_tokens")),
            cache_write_5m=w5, cache_write_1h=w1,
            web_search_requests=searches, web_fetch_requests=fetches,
        )]
    rows = []
    for it in iterations:
        w5, w1 = _cache_writes(it, default_ttl)
        rows.append(UsageRow(
            model=it.get("model") or model,
            kind=str(it.get("type") or "message"),
            input_tokens=_int(it.get("input_tokens")),
            output_tokens=_int(it.get("output_tokens")),
            cache_read=_int(it.get("cache_read_input_tokens")),
            cache_write_5m=w5, cache_write_1h=w1,
        ))
    rows[-1] = replace(rows[-1], web_search_requests=searches, web_fetch_requests=fetches)
    return rows


def cost_for(model: str, usage, *, default_ttl: str = "1h") -> Decimal:
    """USD cost of one request."""
    return sum((row.cost() for row in usage_rows(model, usage, default_ttl=default_ttl)),
               Decimal("0"))
