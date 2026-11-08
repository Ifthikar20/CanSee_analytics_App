"""Ledger rows for agent requests (core.ai_tracking.record_usage).

One AITokenUsage row per priced unit of a request (llm.pricing): the
request itself, or each of its ``usage.iterations`` entries. The
idempotency key is per run, step and row, so a replayed write collapses
to the original row.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from decimal import Decimal

from apps.agent.conf import agent_conf
from apps.agent.constants import USAGE_MODULE
from apps.agent.llm.pricing import usage_rows
from core.ai_tracking import record_usage


@dataclass
class UsageTotals:
    requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    web_search_requests: int = 0
    cost_usd: Decimal = field(default_factory=lambda: Decimal("0"))
    estimated: bool = False

    def add(self, other: UsageTotals) -> None:
        self.requests += other.requests
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cache_read_tokens += other.cache_read_tokens
        self.cache_write_tokens += other.cache_write_tokens
        self.web_search_requests += other.web_search_requests
        self.cost_usd += other.cost_usd
        self.estimated = self.estimated or other.estimated


def engine_usage(capture) -> UsageTotals:
    """What AI engine answers (a core.ai_tracking.capture_usage capture)
    add to a run: their cost. They are no model request of the run (the
    request cap counts the agent's own) and their tokens are another
    provider's, so only the cost is added; their ledger rows are already
    written by the provider call."""
    return UsageTotals(requests=0, cost_usd=Decimal(str(capture.cost_usd)))


def estimate_output_tokens(chars: int) -> int:
    """A conservative token count for streamed text whose usage never came
    (about 3 characters per token, rounded up)."""
    return max(1, math.ceil(max(0, chars) / 3))


def record_request_usage(
    run,
    usage,
    *,
    model: str,
    role: str,
    step: str,
    request_id: str = "",
    stop_reason: str = "",
    estimated: bool = False,
    duration_ms: int = 0,
    extra: dict | None = None,
) -> UsageTotals:
    """Record one request's usage and return what it adds to the run.
    ``extra`` adds flat keys to each row's metadata (the research agents
    tag their analyses with the swarm, version and task)."""
    rows = usage_rows(model, usage, default_ttl=agent_conf().cache_ttl)
    totals = UsageTotals(requests=1, estimated=estimated)
    for i, row in enumerate(rows):
        cost = row.cost()
        record_usage(
            module=USAGE_MODULE,
            model_name=row.model,
            input_tokens=row.input_tokens,
            output_tokens=row.output_tokens,
            user=run.user,
            website=run.website,
            provider="anthropic",
            duration_ms=duration_ms if i == 0 else 0,
            metadata={
                # The ledger's own keys below win over ``extra``.
                **(extra or {}),
                "role": role,
                "run_id": str(run.id),
                "chat_id": str(run.chat_id),
                "request_id": request_id or "",
                "stop_reason": stop_reason or "",
                "iteration": row.kind,
                "estimated": estimated,
                "web_fetch_requests": row.web_fetch_requests,
            },
            idempotency_key=f"agent:{run.id}:{step}:{i}",
            cache_write_5m=row.cache_write_5m,
            cache_write_1h=row.cache_write_1h,
            cache_read=row.cache_read,
            web_search_requests=row.web_search_requests,
        )
        totals.input_tokens += row.input_tokens
        totals.output_tokens += row.output_tokens
        totals.cache_read_tokens += row.cache_read
        totals.cache_write_tokens += row.cache_write
        totals.web_search_requests += row.web_search_requests
        totals.cost_usd += cost
    return totals
