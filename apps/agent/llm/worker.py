"""The research agents' analysis requests (Claude Haiku 5.5, design 10).

One non-streaming request per analysis through the shared client, with
structured JSON output and thinking off (accepted at effort low, medium
or high). Never sent: temperature, top_p or top_k (a 400 on this model),
a thinking budget, an assistant prefill (a 400), fallbacks (the model has
no server-side fallback), tools or cache_control (the prompts are short
and run as one burst). The user turn is the only data channel and always
the last message.

Before a request goes out: Stop and the task's time limit, the user's
monthly AI allowance, and a slot of the process-wide ``haiku`` limit
(after a wait for that slot, Stop, the time and the allowance again).
Each request is recorded in the usage ledger (module ``agent``, role
``worker``) and its cost added to the run; it does not count toward the
run's request cap.
"""
from __future__ import annotations

import hashlib
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

from pydantic import BaseModel, ValidationError

from apps.agent.conf import agent_conf
from apps.agent.llm.client import get_client
from apps.agent.llm.errors import classify
from apps.agent.llm.pricing import usage_rows
from apps.agent.llm.usage import record_request_usage
from apps.agent.runtime.swarm import limits
from core.ai_tracking import record_usage
from core.llm import allowance_denial

logger = logging.getLogger("apps")

ALLOWANCE_MESSAGE = "The monthly AI allowance is used up."
REFUSED_MESSAGE = "The analysis model declined this input."
INVALID_MESSAGE = "The analysis came back unreadable."
STOPPED_MESSAGE = "Stopped before the analysis ran."
# AITokenUsage.idempotency_key holds at most this many characters; the
# ledger key is "agent:{run_id}:{step}:{row}" (llm.usage).
LEDGER_KEY_CHARS = 120
_LEDGER_ROW_CHARS = 3


def usage_step(run, task_id: str, purpose: str) -> str:
    """The ledger step of one analysis. Unique within the run per swarm,
    version, task and purpose: two researches in one turn share task ids
    (engines.p1.chatgpt), and a shared key would make the ledger drop the
    second request's usage as a replay. A step too long for the ledger's
    key is shortened to a digest."""
    swarm = str(run.swarm.id).replace("-", "")[:8]
    head = f"w{swarm}.{run.version.version}"
    step = f"{head}:{task_id}:{purpose}"
    room = LEDGER_KEY_CHARS - len(f"agent:{run.ctx.run.id}:") - _LEDGER_ROW_CHARS
    if len(step) > room:
        step = f"{head}:{hashlib.sha1(step.encode()).hexdigest()[:24]}"
    return step


@dataclass
class HaikuResult:
    ok: bool
    data: BaseModel | None = None
    # allowance_exceeded | refused | invalid_output | rate_limited |
    # upstream_error | cancelled
    code: str | None = None
    # Public text: never the provider's own error text.
    message: str | None = None
    request_id: str = ""

    @property
    def error(self) -> dict | None:
        return None if self.ok else {"code": self.code, "message": self.message}


def build_worker_request(*, system: str, content: list[dict], schema: dict) -> dict:
    """The request body of one analysis (verified for claude-haiku-5-5)."""
    conf = agent_conf()
    return {
        "model": conf.worker_model,
        "max_tokens": conf.worker_max_tokens,
        "system": [{"type": "text", "text": system}],
        "thinking": {"type": "disabled"},
        "output_config": {
            "effort": conf.worker_effort,
            "format": {"type": "json_schema", "schema": schema},
        },
        "messages": [{"role": "user", "content": content}],
    }


def _failed(code: str, message: str, request_id: str = "") -> HaikuResult:
    return HaikuResult(ok=False, code=code, message=message, request_id=request_id)


def _refusal(run, ends: float) -> HaikuResult | None:
    """Why the analysis must not go out now (Stop, no time left, or the
    monthly allowance used up), or None when it may."""
    if run.cancelled() or time.monotonic() >= ends:
        return _failed("cancelled", STOPPED_MESSAGE)
    if allowance_denial(run.ctx.user):
        return _failed("allowance_exceeded", ALLOWANCE_MESSAGE)
    return None


def ask_haiku(*, run, task_id: str, worker: str, purpose: str, system: str,
              content: list[dict], schema: dict, output_model: type[BaseModel],
              timeout: float) -> HaikuResult:
    """One analysis for task ``task_id`` of ``run`` (a SwarmRun). ``timeout``
    is what is left of the task's time; never raises for an API failure."""
    ends = time.monotonic() + timeout
    refused = _refusal(run, ends)
    if refused is not None:
        return refused
    # content_gap and citations got their slot when they started.
    take_slot = not run.inline and not limits.holds_haiku(worker)
    if take_slot and not limits.acquire("haiku", timeout):
        return _failed("cancelled", STOPPED_MESSAGE)

    request = build_worker_request(system=system, content=content, schema=schema)
    started = time.monotonic()
    try:
        # The wait for a slot can be long (other researches hold them):
        # Stop, the time left and the allowance are checked again, so a
        # stopped task or a spent allowance sends nothing.
        refused = _refusal(run, ends) if take_slot else None
        if refused is not None:
            return refused
        response = get_client().messages.create(
            **request, timeout=max(1.0, ends - time.monotonic()))
    except Exception as exc:
        error = classify(exc)
        logger.warning("agent swarm: analysis request failed (task %s, %s): %s",
                       task_id, purpose, error.code)
        busy = error.code in ("rate_limited", "upstream_overloaded")
        code = "rate_limited" if busy else "upstream_error"
        return _failed(code, error.message)
    finally:
        if take_slot:
            limits.release("haiku")

    request_id = getattr(response, "_request_id", "") or ""
    stop_reason = response.stop_reason or ""
    try:
        totals = record_request_usage(
            run.ctx.run, response.usage, model=request["model"], role="worker",
            step=usage_step(run, task_id, purpose), request_id=request_id,
            stop_reason=stop_reason, duration_ms=int((time.monotonic() - started) * 1000),
            extra={
                "swarm_id": str(run.swarm.id),
                "version": run.version.version,
                "task_id": task_id,
                "worker": worker,
                "purpose": purpose,
            },
        )
        run.add_usage(totals, requests=1)
    except Exception:
        logger.warning("agent swarm: analysis usage was not recorded (task %s)", task_id,
                       exc_info=True)

    if stop_reason == "refusal":
        return _failed("refused", REFUSED_MESSAGE, request_id)
    if stop_reason == "max_tokens":
        return _failed("invalid_output", INVALID_MESSAGE, request_id)
    # Read the answer by block type: a thinking block, if any, is skipped.
    text = "".join(getattr(block, "text", "") or "" for block in response.content
                   if getattr(block, "type", "") == "text")
    try:
        data = output_model.model_validate_json(text)
    except (ValidationError, ValueError):
        return _failed("invalid_output", INVALID_MESSAGE, request_id)
    return HaikuResult(ok=True, data=data, request_id=request_id)


# ── Outside a research run ────────────────────────────────────────────────


def _read(response, output_model: type[BaseModel], request_id: str,
          stop_reason: str) -> HaikuResult:
    """The answer of one standalone request (ask_haiku's reading rules)."""
    if stop_reason == "refusal":
        return _failed("refused", REFUSED_MESSAGE, request_id)
    if stop_reason == "max_tokens":
        return _failed("invalid_output", INVALID_MESSAGE, request_id)
    text = "".join(getattr(block, "text", "") or "" for block in response.content
                   if getattr(block, "type", "") == "text")
    try:
        data = output_model.model_validate_json(text)
    except (ValidationError, ValueError):
        return _failed("invalid_output", INVALID_MESSAGE, request_id)
    return HaikuResult(ok=True, data=data, request_id=request_id)


def _standalone_step(module: str, step: str) -> str:
    """``step`` as the ledger key "{module}:{step}:{row}" can hold it: a
    step too long for the key is shortened to a digest."""
    room = LEDGER_KEY_CHARS - len(f"{module}:") - _LEDGER_ROW_CHARS
    if len(step) > room:
        step = hashlib.sha1(step.encode()).hexdigest()
    return step


def _standalone_refusal(user, should_stop) -> HaikuResult | None:
    if should_stop is not None and should_stop():
        return _failed("cancelled", STOPPED_MESSAGE)
    if allowance_denial(user):
        return _failed("allowance_exceeded", ALLOWANCE_MESSAGE)
    return None


def ask_haiku_standalone(*, user, website, system: str, content: list[dict], schema: dict,
                         output_model: type[BaseModel], timeout: float, audit_id: str, role: str,
                         step: str, module: str = "insights", max_tokens: int | None = None,
                         should_stop: Callable[[], bool] | None = None) -> HaikuResult:
    """One analysis request outside a research run (insights runs, context
    builds): the request and reading rules of ask_haiku, the same process-wide
    analysis slots, and one ledger row per priced unit with ``module``,
    ``metadata.role`` and ``metadata.audit_id`` (the caller's run tag).

    ``step`` is unique per request within the module (the ledger drops a
    second write under the same key as a replay). Stop (``should_stop``)
    and the user's monthly allowance are checked before the request and
    again after the wait for a slot. Never raises for an API failure.
    """
    ends = time.monotonic() + timeout
    refused = _standalone_refusal(user, should_stop)
    if refused is not None:
        return refused
    if not limits.acquire("haiku", timeout):
        return _failed("rate_limited", "The analysis model was busy. Try again in a minute.")

    request = build_worker_request(system=system, content=content, schema=schema)
    if max_tokens is not None:
        request["max_tokens"] = int(max_tokens)
    started = time.monotonic()
    try:
        # The wait for a slot can be long: Stop and the allowance again.
        refused = _standalone_refusal(user, should_stop)
        if refused is not None:
            return refused
        response = get_client().messages.create(
            **request, timeout=max(1.0, ends - time.monotonic()))
    except Exception as exc:
        error = classify(exc)
        logger.warning("insights: analysis request failed (%s, %s): %s", role, step, error.code)
        busy = error.code in ("rate_limited", "upstream_overloaded")
        return _failed("rate_limited" if busy else "upstream_error", error.message)
    finally:
        limits.release("haiku")

    request_id = getattr(response, "_request_id", "") or ""
    stop_reason = response.stop_reason or ""
    key_step = _standalone_step(module, step)
    try:
        rows = usage_rows(request["model"], response.usage, default_ttl=agent_conf().cache_ttl)
        duration_ms = int((time.monotonic() - started) * 1000)
        for i, row in enumerate(rows):
            record_usage(
                module=module,
                model_name=row.model,
                input_tokens=row.input_tokens,
                output_tokens=row.output_tokens,
                user=user,
                website=website,
                provider="anthropic",
                duration_ms=duration_ms if i == 0 else 0,
                metadata={
                    "role": role,
                    "audit_id": audit_id,
                    "request_id": request_id,
                    "stop_reason": stop_reason,
                    "iteration": row.kind,
                },
                idempotency_key=f"{module}:{key_step}:{i}"[:LEDGER_KEY_CHARS],
                cache_write_5m=row.cache_write_5m,
                cache_write_1h=row.cache_write_1h,
                cache_read=row.cache_read,
                web_search_requests=row.web_search_requests,
            )
    except Exception:
        logger.warning("insights: analysis usage was not recorded (%s, %s)", role, step,
                       exc_info=True)
    return _read(response, output_model, request_id, stop_reason)
