"""Checks before every model request of a run (orchestrator and sub-agents).

In order: the kill switch, a stop request, the run's deadline, the
user's monthly AI allowance, the per-run cost ceiling and the per-run
request cap. A failed check ends the run; nothing is sent. The deadline
and the cost ceiling are also checked between the AI engine calls and
research agents a tool runs (``budget_stop``), whose cost counts toward
the run's.
"""
from __future__ import annotations

import time
from decimal import Decimal

from apps.agent.conf import agent_conf
from core.llm import allowance_denial


class GateStop(Exception):
    def __init__(self, code: str, message: str, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable

    def as_payload(self) -> dict:
        return {"code": self.code, "message": self.message, "retryable": self.retryable}


class CancelRequested(Exception):
    """The run was asked to stop; finish it as cancelled."""


def _timeout_stop(conf, started_monotonic: float) -> GateStop | None:
    if time.monotonic() - started_monotonic > conf.run_max_seconds:
        return GateStop(
            "run_timeout", "This reply took too long and was stopped.", retryable=True,
        )
    return None


def _cost_stop(conf, cost_usd: Decimal) -> GateStop | None:
    if cost_usd >= Decimal(str(conf.run_max_cost_usd)):
        return GateStop(
            "run_cost_limit",
            f"This reply reached the ${conf.run_max_cost_usd:.2f} cost limit for one reply.",
        )
    return None


def budget_stop(*, started_monotonic: float, cost_usd: Decimal) -> GateStop | None:
    """The run's deadline and cost ceiling alone, for the work a tool does
    between model requests (asking AI engines, running research agents):
    the GateStop that ends the run, or None while both hold."""
    conf = agent_conf()
    return _timeout_stop(conf, started_monotonic) or _cost_stop(conf, cost_usd)


def before_request(run, *, started_monotonic: float, requests: int, cost_usd: Decimal,
                   cancelled: bool) -> None:
    """Raise CancelRequested or GateStop when the next request must not go out."""
    conf = agent_conf()
    if cancelled:
        raise CancelRequested()
    if not conf.enabled:
        raise GateStop("agent_disabled", "The agent workspace was turned off.")
    stop = _timeout_stop(conf, started_monotonic)
    if stop is not None:
        raise stop
    if allowance_denial(run.user):
        raise GateStop(
            "allowance_exceeded", "You have used your monthly AI allowance.",
        )
    stop = _cost_stop(conf, cost_usd)
    if stop is not None:
        raise stop
    if requests >= conf.max_requests_per_run:
        raise GateStop(
            "request_limit",
            f"This reply reached the limit of {conf.max_requests_per_run} model requests.",
        )
