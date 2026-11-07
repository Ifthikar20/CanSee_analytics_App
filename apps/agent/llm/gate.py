"""Checks before every model request of a run (orchestrator and sub-agents).

In order: the kill switch, a stop request, the run's deadline, the
user's monthly AI allowance, the per-run cost ceiling and the per-run
request cap. A failed check ends the run; nothing is sent.
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


def before_request(run, *, started_monotonic: float, requests: int, cost_usd: Decimal,
                   cancelled: bool) -> None:
    """Raise CancelRequested or GateStop when the next request must not go out."""
    conf = agent_conf()
    if cancelled:
        raise CancelRequested()
    if not conf.enabled:
        raise GateStop("agent_disabled", "The agent workspace was turned off.")
    if time.monotonic() - started_monotonic > conf.run_max_seconds:
        raise GateStop(
            "run_timeout", "This reply took too long and was stopped.", retryable=True,
        )
    if allowance_denial(run.user):
        raise GateStop(
            "allowance_exceeded", "You have used your monthly AI allowance.",
        )
    if cost_usd >= Decimal(str(conf.run_max_cost_usd)):
        raise GateStop(
            "run_cost_limit",
            f"This reply reached the ${conf.run_max_cost_usd:.2f} cost limit for one reply.",
        )
    if requests >= conf.max_requests_per_run:
        raise GateStop(
            "request_limit",
            f"This reply reached the limit of {conf.max_requests_per_run} model requests.",
        )
