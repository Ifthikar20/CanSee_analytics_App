"""The run loop: one turn of the orchestrator, from queued to terminal.

A manual loop over streamed requests (design section 6):

- every request replays the chat's pinned snapshot and the rendered
  history (runtime.wire) plus what this turn appended so far;
- ``pause_turn``: the paused content is appended and the request resent
  (at most MAX_PAUSES times);
- ``max_tokens`` with a tool call: the response is discarded
  (``turn.reset``) and the request retried once with twice the
  max_tokens; text only: the reply ends ``truncated``;
- ``refusal``: the response is discarded, a notice added, the reply ends
  ``refused`` and no tool of that response runs;
- after a server-side fallback, only content after the last fallback
  block is echoed back with its earlier text (wire.fallback_filter);
- tools run one after another and all results go back in one message;
- invalid streamed tool JSON and mid-stream overload are retried a few
  times with ``turn.reset`` (and ``run.retrying`` after a backoff);
- a stop keeps the safe part of the response and answers open tool
  calls with "Stopped before this ran."; every ending stores a wire the
  next turn can follow (wire.close_turn: a paused server tool call that
  never got its result is dropped).

A re-run turn (trigger ``rerun``) makes no model request: it runs one
version of a research swarm, adds the results table and a short summary
of what changed, and stores that summary as the turn's wire.

Usage is recorded per request and the gate (llm.gate) is checked before
every request. Postgres holds the result: wire at each boundary, parts
at checkpoints, status with a compare-and-set at the end. A run always
ends with one terminal event, also when the database fails under it.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from decimal import Decimal

from django.db import connections, transaction
from django.utils import timezone

from apps.agent.conf import agent_conf
from apps.agent.constants import (
    ACTIVE_RUN_STATUSES,
    ACTIVE_VERSION_STATUSES,
    LIVE_SWARM_STATUSES,
    EventType,
    MessageStatus,
    PartType,
    RunStatus,
    RunTrigger,
    VersionStatus,
)
from apps.agent.llm.client import get_client
from apps.agent.llm.errors import INTERNAL, classify
from apps.agent.llm.gate import CancelRequested, GateStop, before_request, budget_stop
from apps.agent.llm.request import MAX_OUTPUT_TOKENS, build_turn_request
from apps.agent.llm.usage import UsageTotals, estimate_output_tokens, record_request_usage
from apps.agent.runtime import wire
from apps.agent.runtime.emitter import RunEmitter
from apps.agent.runtime.events import get_event_store, now_iso
from apps.agent.runtime.registry import get_registry
from apps.agent.runtime.request_stream import stream_request
from apps.agent.runtime.translate import StreamTranslator
from apps.agent.tools import registry as tool_registry
from apps.agent.tools.context import ToolContext, ToolResult
from core.resilience import CircuitBreaker

logger = logging.getLogger("apps")

MAX_PAUSES = 5
MAX_JSON_RETRIES = 2
MAX_STREAM_RETRIES = 2
# Seconds before each in-run retry of an overloaded or broken stream.
RETRY_DELAYS = (2.0, 6.0)
BREAKER = CircuitBreaker("llm:claude-agent", failure_threshold=5, recovery_timeout=60)

RUN_TIMEOUT = GateStop("run_timeout", "This reply took too long and was stopped.",
                       retryable=True)

_TERMINAL_PART_STATUS = {"done", "error", "cancelled", "unavailable", "partial",
                         "interrupted"}


@dataclass
class Ending:
    message_status: str
    stop_reason: str = ""
    error: dict | None = None


def run_turn(run_id) -> None:
    """Execute a queued run to its end. A run another worker took, or one
    stopped while queued, is left alone. A run that cannot even start
    (the database failed under it) is failed, so it never stays active."""
    from apps.agent.models import AgentRun

    registry = get_registry()
    try:
        now = timezone.now()
        claimed = AgentRun.objects.filter(id=run_id, status=RunStatus.QUEUED).update(
            status=RunStatus.RUNNING, executor_id=registry.executor_id, started_at=now,
            heartbeat_at=now,
        )
        if not claimed:
            return
        run = (AgentRun.objects
               .select_related("chat", "chat__snapshot", "website", "user", "assistant_message",
                               "user_message")
               .get(id=run_id))
        TurnRunner(run, registry.register(run.id)).execute()
    except Exception:
        logger.exception("agent run %s could not start", run_id)
        _reset_connections()
        fail_unstarted(run_id)
    finally:
        registry.unregister(run_id)


def _reset_connections() -> None:
    """Drop this thread's database connections, so the next query opens a
    new one (Django reconnects only between requests, never inside a run
    thread). A connection inside a transaction (an inline run) is kept."""
    for conn in connections.all(initialized_only=True):
        if not conn.in_atomic_block:
            conn.close()


def fail_unstarted(run_id) -> None:
    """Fail a run whose runner never got going (it could not start, or not
    be handed to a thread), and end its event stream. The research version
    a re-run would have run fails with it, so it never stays queued."""
    from apps.agent.models import AgentMessage, AgentRun, AgentSwarmVersion

    message_id = None
    try:
        message_id = (AgentRun.objects.filter(id=run_id)
                      .values_list("assistant_message_id", flat=True).first())
        now = timezone.now()
        with transaction.atomic():
            failed = AgentRun.objects.filter(id=run_id, status__in=ACTIVE_RUN_STATUSES).update(
                status=RunStatus.FAILED, error_code=INTERNAL.code,
                error_message=INTERNAL.message, finished_at=now,
            )
            if failed and message_id:
                AgentMessage.objects.filter(id=message_id).update(
                    status=MessageStatus.FAILED, error_code=INTERNAL.code,
                    error_message=INTERNAL.message, finished_at=now,
                )
            if failed:
                AgentSwarmVersion.objects.filter(
                    run_id=run_id, status__in=ACTIVE_VERSION_STATUSES,
                ).update(status=VersionStatus.ERROR, error_code=INTERNAL.code,
                         error_message=INTERNAL.message, finished_at=now)
    except Exception:
        logger.exception("agent run %s could not be marked failed", run_id)
    store = get_event_store()
    if store.has(run_id):
        store.append(str(run_id), {
            "type": EventType.RUN_FAILED,
            "run_id": str(run_id),
            "message_id": str(message_id) if message_id else None,
            "ts": now_iso(),
            "error": INTERNAL.as_payload(),
        })


class TurnRunner:
    def __init__(self, run, active):
        self.run = run
        self.chat = run.chat
        self.message = run.assistant_message
        self.user_message = run.user_message
        self.snapshot = self.chat.snapshot
        self.conf = agent_conf()
        self.active = active
        self.emitter = RunEmitter(run, self.message)
        self.started = time.monotonic()
        self.totals = UsageTotals()
        # add_usage may come from a research agent's thread that outlived
        # its task (its usage arrived after the research ended).
        self._usage_lock = threading.RLock()
        self.history = wire.history_of(wire.path_to(self.user_message))
        self.turn_wire: list[dict] = wire.loads(self.message.wire)
        self.request_index = 0
        self.server_parts: dict[str, str] = {}
        self.max_tokens = int((self.snapshot.config or {}).get("max_tokens")
                              or self.conf.max_tokens)
        self.model_used = (self.snapshot.config or {}).get("model") or self.conf.model
        self._client = None
        # The outcome of the terminal compare-and-set, once _finish made it.
        self._claimed: bool | None = None

    # ── Entry ────────────────────────────────────────────────────────

    def execute(self) -> None:
        from apps.agent.models import AgentMessage

        ending = Ending(MessageStatus.FAILED, error=INTERNAL.as_payload())
        try:
            now = timezone.now()
            AgentMessage.objects.filter(id=self.message.id).update(
                started_at=now, status=MessageStatus.STREAMING,
            )
            self.emitter.emit(EventType.RUN_STARTED, chat_id=str(self.chat.id),
                              user_message_id=str(self.user_message.id), model=self.model_used)
            self._announce_fallback_title()
            self._start_title()
            try:
                ending = self._rerun() if self.run.trigger == RunTrigger.RERUN else self._loop()
            except CancelRequested:
                ending = Ending(MessageStatus.CANCELLED)
            except GateStop as stop:
                ending = Ending(MessageStatus.FAILED, error=stop.as_payload())
            except Exception as exc:
                logger.exception("agent run %s failed", self.run.id)
                ending = Ending(MessageStatus.FAILED, error=classify(exc).as_payload())
            ending = self._overdue_ending(ending)
            self._inline_title()
            self._finish(ending)
        except Exception:
            # The database failed under the run (a dropped connection, a
            # Postgres restart): end it on a new connection.
            logger.exception("agent run %s could not be finished", self.run.id)
            self._finish_after_error(ending)

    def _overdue_ending(self, ending: Ending) -> Ending:
        """A run the heartbeat stopped for running past its deadline
        (registry.ActiveRun.timed_out) fails with run_timeout, not as a
        user's Stop."""
        if (ending.message_status == MessageStatus.CANCELLED
                and getattr(self.active, "timed_out", False)):
            return Ending(MessageStatus.FAILED, error=RUN_TIMEOUT.as_payload())
        return ending

    def _finish_after_error(self, ending: Ending) -> None:
        """Finish once more on new connections and, when even that fails,
        still end the event stream, so no reader waits on a run that no
        thread runs any more (the sweeper settles the row later)."""
        _reset_connections()
        try:
            self._finish(ending)
        except Exception:
            logger.exception("agent run %s could not be saved", self.run.id)
        if not self.emitter.ended:
            self.emitter.emit(EventType.RUN_FAILED, error=INTERNAL.as_payload())

    # ── Loop ─────────────────────────────────────────────────────────

    def _loop(self) -> Ending:
        pauses = json_retries = stream_retries = 0
        widened = False
        while True:
            self._gate()
            self.request_index += 1
            req_n = self.request_index
            request = build_turn_request(
                self.snapshot, wire.render(self.history + self.turn_wire),
                max_tokens=self.max_tokens,
            )
            translator = StreamTranslator(self.emitter, req_n, server_parts=self.server_parts)
            outcome = stream_request(self._get_client(), request, translator=translator,
                                     active=self.active, on_event=self.emitter.checkpoint)
            self._account(outcome, translator, request, req_n)

            if outcome.cancelled:
                self._keep_partial(outcome)
                raise CancelRequested()
            if outcome.invalid_json:
                self.emitter.reset_request(req_n)
                json_retries += 1
                if json_retries > MAX_JSON_RETRIES:
                    return Ending(MessageStatus.FAILED, error={
                        "code": "invalid_tool_input", "retryable": True,
                        "message": "The model sent tool input that could not be read.",
                    })
                continue
            if outcome.error is not None:
                err = classify(outcome.error)
                if err.retry_in_run:
                    BREAKER.record_failure()
                self.emitter.reset_request(req_n)
                if err.retry_in_run and stream_retries < MAX_STREAM_RETRIES:
                    delay = RETRY_DELAYS[min(stream_retries, len(RETRY_DELAYS) - 1)]
                    stream_retries += 1
                    self.emitter.emit(EventType.RUN_RETRYING, attempt=stream_retries,
                                      reason=err.code, delay_ms=int(delay * 1000))
                    if self.active.cancel_event.wait(delay):
                        raise CancelRequested()
                    continue
                return Ending(MessageStatus.FAILED, error=err.as_payload())

            final = outcome.final
            BREAKER.record_success()
            json_retries = stream_retries = 0
            self.model_used = getattr(final, "model", None) or self.model_used
            blocks = [wire.block_to_wire(b) for b in final.content]
            kept, dropped = wire.fallback_filter(blocks)
            self._skip_dropped(translator, dropped)
            stop = final.stop_reason or ""
            tool_uses = [b for b in kept if b.get("type") == "tool_use"]

            if stop == "refusal":
                self.emitter.reset_request(req_n)
                self.emitter.add_part(
                    PartType.NOTICE, request=req_n, level="warning", code="refusal",
                    message="The model declined to continue with this request.",
                )
                return Ending(MessageStatus.REFUSED, stop_reason=stop)
            if stop == "max_tokens" and tool_uses:
                self.emitter.reset_request(req_n)
                if widened or self.max_tokens >= MAX_OUTPUT_TOKENS:
                    return Ending(MessageStatus.FAILED, stop_reason=stop, error={
                        "code": "max_tokens", "retryable": True,
                        "message": "The reply needed more room than one response allows.",
                    })
                widened = True
                self.max_tokens = min(self.max_tokens * 2, MAX_OUTPUT_TOKENS)
                continue

            if kept:
                self.turn_wire.append(wire.assistant_message(kept))
                self._save_wire()
            if stop == "pause_turn":
                pauses += 1
                if pauses > MAX_PAUSES:
                    self._close_turn()
                    return Ending(MessageStatus.FAILED, stop_reason=stop, error={
                        "code": "pause_limit", "retryable": True,
                        "message": "The web research took too many steps to finish.",
                    })
                continue
            if stop in ("max_tokens", "model_context_window_exceeded"):
                return Ending(MessageStatus.TRUNCATED, stop_reason=stop)
            if stop == "tool_use" and tool_uses:
                self._run_tools(tool_uses, req_n, translator)
                continue
            return Ending(MessageStatus.COMPLETE, stop_reason=stop)

    def _rerun(self) -> Ending:
        """A re-run turn: one version of a research swarm, its table and a
        summary of the changes; no model request."""
        from apps.agent.models import AgentSwarmVersion
        from apps.agent.runtime.swarm.executor import execute_version
        from apps.agent.services.swarm_tables import finalize_version

        self._gate()
        version = AgentSwarmVersion.objects.select_related("swarm").get(run=self.run)
        self.request_index = 1
        ctx = self._tool_context(1, f"rerun:{version.id}", None)
        outcome = execute_version(ctx, version.swarm, version)
        finalized = finalize_version(ctx, version.swarm, version, outcome)
        self.emitter.add_part(PartType.TEXT, request=1, text=finalized.summary_text, citations=[])
        self.turn_wire = [wire.assistant_message([wire.text_block(finalized.wire_text)])]
        self._save_wire()
        return Ending(MessageStatus.CANCELLED if outcome.cancelled else MessageStatus.COMPLETE)

    def _gate(self) -> None:
        with self._usage_lock:
            requests, cost_usd = self.totals.requests, self.totals.cost_usd
        before_request(self.run, started_monotonic=self.started, requests=requests,
                       cost_usd=cost_usd, cancelled=self.active.cancelled)
        if not BREAKER.allow():
            raise GateStop("upstream_unavailable",
                           "The AI service is unavailable right now. Please try again shortly.",
                           retryable=True)

    def _budget_stop(self, extra_usd: Decimal | None = None) -> GateStop | None:
        """The run's deadline and cost ceiling, for tools between requests;
        ``extra_usd`` is spend a tool has seen but not added yet."""
        with self._usage_lock:
            cost_usd = self.totals.cost_usd
        return budget_stop(started_monotonic=self.started,
                           cost_usd=cost_usd + Decimal(str(extra_usd or 0)))

    def _get_client(self):
        if self._client is None:
            self._client = get_client()
        return self._client

    # ── Usage ────────────────────────────────────────────────────────

    def _account(self, outcome, translator, request: dict, req_n: int) -> None:
        if outcome.final is not None:
            usage, estimated = outcome.final.usage, False
            stop = outcome.final.stop_reason or ""
        elif outcome.partial_usage is not None:
            usage = dict(outcome.partial_usage)
            usage["output_tokens"] = max(int(usage.get("output_tokens") or 0),
                                         estimate_output_tokens(translator.chars))
            estimated, stop = True, ""
        else:
            return
        totals = record_request_usage(
            self.run, usage, model=request["model"], role="orchestrator", step=str(req_n),
            request_id=outcome.request_id or "", stop_reason=stop, estimated=estimated,
        )
        self.add_usage(totals)

    def add_usage(self, totals: UsageTotals) -> None:
        """Add usage to the run: the orchestrator's requests, sub-agents',
        research analyses and the AI engine answers its tools asked for.
        Safe from another thread."""
        from apps.agent.models import AgentRun

        with self._usage_lock:
            self.totals.add(totals)
            t = self.totals
            AgentRun.objects.filter(id=self.run.id).update(
                requests=t.requests, input_tokens=t.input_tokens, output_tokens=t.output_tokens,
                cache_read_tokens=t.cache_read_tokens, cache_write_tokens=t.cache_write_tokens,
                web_search_requests=t.web_search_requests, cost_usd=t.cost_usd,
                usage_estimated=t.estimated,
            )
            self.emitter.emit_before_end(EventType.RUN_USAGE, **self._usage_payload())

    def _usage_payload(self) -> dict:
        t = self.totals
        return {
            "requests": t.requests,
            "input_tokens": t.input_tokens,
            "output_tokens": t.output_tokens,
            "cache_read_tokens": t.cache_read_tokens,
            "cache_write_tokens": t.cache_write_tokens,
            "web_search_requests": t.web_search_requests,
            "cost_usd": float(round(t.cost_usd, 6)),
        }

    # ── Wire ─────────────────────────────────────────────────────────

    def _save_wire(self) -> None:
        from apps.agent.models import AgentMessage

        AgentMessage.objects.filter(id=self.message.id).update(wire=wire.dumps(self.turn_wire))
        self.emitter.checkpoint(force=True)

    def _keep_partial(self, outcome) -> None:
        """Keep the safe part of a stopped response. When it continued a
        paused one, the two are cut together: a paused server tool call
        whose result had not arrived yet is dropped."""
        kept = wire.safe_prefix(outcome.partial_blocks, outcome.completed)
        if kept:
            self.turn_wire.append(wire.assistant_message(kept))
        closed = wire.close_turn(self.turn_wire)
        if kept or closed != self.turn_wire:
            self.turn_wire = closed
            self._save_wire()

    def _close_turn(self) -> None:
        closed = wire.close_turn(self.turn_wire)
        if closed != self.turn_wire:
            self.turn_wire = closed
            self._save_wire()

    def _skip_dropped(self, translator, dropped_ids: list[str]) -> None:
        for tid in dropped_ids:
            part_id = translator.tool_parts.get(tid)
            if part_id:
                self.emitter.update_part(part_id, status="error",
                                         error="Not run: the request moved to a fallback model.")

    # ── Tools ────────────────────────────────────────────────────────

    def _tool_context(self, req_n: int, tool_use_id: str, part_id: str | None) -> ToolContext:
        history_text = wire.outside_text(self.history + self.turn_wire)
        return ToolContext(
            run=self.run, chat=self.chat, website=self.run.website, user=self.run.user,
            emitter=self.emitter, active=self.active, request=req_n, tool_use_id=tool_use_id,
            part_id=part_id, history_text=history_text, add_usage=self.add_usage,
            check_gate=self._gate, budget_stop=self._budget_stop,
            inline=self.conf.run_mode == "inline",
            # The research agents end before the run's own deadline.
            extra={"run_started": self.started},
        )

    def _run_tools(self, tool_uses: list[dict], req_n: int, translator) -> None:
        results: list[dict] = []
        for i, block in enumerate(tool_uses):
            tid, name = block.get("id"), block.get("name")
            if self.active.cancelled:
                self._stop_tools(tool_uses[i:], translator, results)
                raise CancelRequested()
            part_id = translator.tool_parts.get(tid)
            generic = part_id is not None and not tool_registry.owns_parts(name)
            if generic:
                self.emitter.update_part(part_id, status="running")
            ctx = self._tool_context(req_n, tid, part_id)
            try:
                result = tool_registry.execute(ctx, name, block.get("input"))
            except (CancelRequested, GateStop) as stop:
                # A tool stopped part way (the research agents) answers its
                # own call with what it finished; the others get the
                # stopped result.
                partial = getattr(stop, "tool_result", None)
                if isinstance(partial, ToolResult):
                    results.append(wire.tool_result(tid, partial.content,
                                                    is_error=partial.is_error))
                self._stop_tools(tool_uses[i:], translator, results)
                raise
            if generic:
                if result.is_error:
                    self.emitter.update_part(part_id, status="error", error=result.summary,
                                             summary="")
                else:
                    self.emitter.update_part(part_id, status="done", summary=result.summary)
            elif part_id is not None:
                current = self.emitter.part(part_id) or {}
                if current.get("type") == PartType.SWARM:
                    # A research whose handler failed before it closed its part.
                    if current.get("status") in LIVE_SWARM_STATUSES:
                        self._close_swarm(current, stopped=False, error=result.summary)
                elif current.get("status") not in _TERMINAL_PART_STATUS:
                    self.emitter.update_part(
                        part_id, status="error" if result.is_error else "done",
                        error=result.summary if result.is_error else None,
                    )
            results.append(wire.tool_result(tid, result.content, is_error=result.is_error))
            self.emitter.checkpoint(force=True)
        self.turn_wire.append({"role": "user", "content": results})
        self._save_wire()

    def _stop_tools(self, remaining: list[dict], translator, results: list[dict]) -> None:
        answered = {r["tool_use_id"] for r in results}
        for block in remaining:
            tid = block.get("id")
            if tid in answered:
                continue
            results.append(wire.tool_result(tid, wire.STOPPED_RESULT, is_error=True))
        self.turn_wire.append({"role": "user", "content": results})
        self._save_wire()

    # ── Title ────────────────────────────────────────────────────────

    def _wants_title(self) -> bool:
        from apps.agent.constants import TitleSource

        return (self.run.trigger == RunTrigger.SEND and self.user_message.parent_id is None
                and self.chat.title_source != TitleSource.USER)

    def _announce_fallback_title(self) -> None:
        """First turn: send the title the chat has now (the first message cut
        to length, stored when it was sent), so the chat list shows it even
        when the model's title fails or comes after the reply ended."""
        from apps.agent.constants import TitleSource

        if (self.user_message.parent_id is None and self.chat.title
                and self.chat.title_source == TitleSource.AUTO):
            self.emitter.emit(EventType.CHAT_UPDATED, chat_id=str(self.chat.id),
                              title=self.chat.title)

    def _start_title(self) -> None:
        """Thread mode: ask for the title next to the turn. The reply never
        waits for it; a title that comes after the reply ended is stored
        without an event (the emitter takes none after the terminal one)."""
        if not self._wants_title() or self.conf.run_mode == "inline":
            return
        from apps.agent.runtime.title import generate_title

        def work():
            try:
                generate_title(self.run, emitter=self.emitter)
            except Exception:
                logger.warning("agent: title generation failed", exc_info=True)
            finally:
                connections.close_all()

        threading.Thread(target=work, name="agent-title", daemon=True).start()

    def _inline_title(self) -> None:
        """Inline mode: the title request runs here, before the reply ends."""
        if not self._wants_title() or self.conf.run_mode != "inline":
            return
        from apps.agent.runtime.title import generate_title

        try:
            generate_title(self.run, emitter=self.emitter)
        except Exception:
            logger.warning("agent: title generation failed", exc_info=True)

    # ── End ──────────────────────────────────────────────────────────

    def _close_swarm(self, part: dict, *, stopped: bool, error: str | None = None) -> None:
        """A swarm part still open: its unfinished agents get task.finished
        (stopped, or not finished because the run failed), then the part is
        cancelled (or failed). The codes are the contract's task error codes
        (swarm contract 5c)."""
        from apps.agent.runtime.swarm.reporter import close_open_tasks

        close_open_tasks(self.emitter, part, status="stopped", error={
            "code": "stopped" if stopped else "internal_error",
            "message": "Stopped" if stopped else "Not finished",
        })
        patch = {"status": "cancelled" if stopped else "error", "ended_at": now_iso()}
        if error and not stopped:
            patch["error"] = error
        self.emitter.update_part(part["id"], **patch)

    def _close_open_parts(self, ending: Ending) -> None:
        stopped = ending.message_status == MessageStatus.CANCELLED
        for part in self.emitter.parts():
            status = part.get("status")
            kind = part.get("type")
            if kind == PartType.THINKING and not part.get("ended_at"):
                self.emitter.update_part(part["id"], ended_at=now_iso())
                continue
            if kind == PartType.SWARM:
                if status in LIVE_SWARM_STATUSES:
                    self._close_swarm(part, stopped=stopped)
                continue
            if status is None or status in _TERMINAL_PART_STATUS:
                continue
            if kind == PartType.AGENT_CALL:
                self.emitter.update_part(part["id"], status="cancelled" if stopped else "error",
                                         error="Stopped" if stopped else "Not finished")
            elif kind in (PartType.SERP, PartType.PAGE_INSPECT):
                self.emitter.update_part(part["id"], status="error", error={
                    "code": "stopped" if stopped else "not_finished",
                    "message": "Stopped" if stopped else "Not finished",
                })
            elif kind in (PartType.WEB_SEARCH, PartType.WEB_FETCH):
                self.emitter.update_part(part["id"], status="error",
                                         error_code="stopped" if stopped else "not_finished")
            else:
                self.emitter.update_part(part["id"], status="error",
                                         error="Stopped" if stopped else "Not finished")

    def _finish(self, ending: Ending) -> None:
        """Store the end of the run and emit its terminal event. Safe to
        call again after it raised: the compare-and-set and the terminal
        event happen once."""
        from apps.agent.models import AgentChat, AgentMessage, AgentRun

        self.turn_wire = wire.close_turn(self.turn_wire)
        self._close_open_parts(ending)
        parts, parts_seq, research = self.emitter.snapshot()
        now = timezone.now()
        status = ending.message_status
        run_status = {
            MessageStatus.CANCELLED: RunStatus.CANCELLED,
            MessageStatus.FAILED: RunStatus.FAILED,
        }.get(status, RunStatus.COMPLETED)
        error = ending.error or {}
        with self._usage_lock:
            usage = {**self._usage_payload(), "estimated": self.totals.estimated}
            cost_usd = self.totals.cost_usd.quantize(Decimal("0.000001"))
        message_fields = {
            "wire": wire.dumps(self.turn_wire),
            "parts": parts,
            "parts_seq": parts_seq,
            "research": research,
            "model": self.model_used or "",
            "usage": usage,
            "cost_usd": cost_usd,
        }
        # The run's terminal status and its message's are written in one
        # transaction: when the message write fails, the run stays running
        # and the retry (_finish_after_error) or the sweeper ends both, so
        # a finished run never leaves its reply "streaming".
        with transaction.atomic():
            claimed = self._claimed
            if claimed is None:
                claimed = bool(
                    AgentRun.objects.filter(id=self.run.id, status=RunStatus.RUNNING).update(
                        status=run_status, stop_reason=ending.stop_reason,
                        error_code=error.get("code", ""), error_message=error.get("message", ""),
                        finished_at=now, last_seq=parts_seq,
                    )
                )
            self._end_versions(run_status, error)
            if claimed:
                message_fields.update(
                    status=status, stop_reason=ending.stop_reason,
                    error_code=error.get("code", ""), error_message=error.get("message", ""),
                    finished_at=now,
                )
            AgentMessage.objects.filter(id=self.message.id).update(**message_fields)
        self._claimed = claimed
        if not claimed:
            # The row was ended elsewhere (the sweeper); tell local readers.
            if not self.emitter.ended:
                self.emitter.emit(EventType.RUN_INTERRUPTED, reason="lost")
            return
        AgentChat.objects.filter(id=self.chat.id).update(last_message_at=now)
        if not self.emitter.ended:
            self._emit_terminal(run_status, ending, parts_seq)
        AgentRun.objects.filter(id=self.run.id).update(last_seq=self.emitter.last_seq)

    def _end_versions(self, run_status: str, error: dict) -> None:
        """A research version of this run that has not ended ends with it:
        cancelled when the run was stopped, else failed."""
        from apps.agent.models import AgentSwarmVersion

        cancelled = run_status == RunStatus.CANCELLED
        fields = {
            "status": VersionStatus.CANCELLED if cancelled else VersionStatus.ERROR,
            "finished_at": timezone.now(),
        }
        if not cancelled:
            fields.update(error_code=error.get("code") or INTERNAL.code,
                          error_message=error.get("message") or INTERNAL.message)
        AgentSwarmVersion.objects.filter(
            run_id=self.run.id, status__in=ACTIVE_VERSION_STATUSES,
        ).update(**fields)

    def _emit_terminal(self, run_status: str, ending: Ending, parts_seq: int) -> None:
        error = ending.error or {}
        if run_status == RunStatus.CANCELLED:
            self.emitter.emit(EventType.RUN_CANCELLED)
        elif run_status == RunStatus.FAILED:
            self.emitter.emit(EventType.RUN_FAILED, error={
                "code": error.get("code", "internal_error"),
                "message": error.get("message", "Something went wrong."),
                "retryable": bool(error.get("retryable")),
            })
        else:
            self.emitter.emit(EventType.RUN_COMPLETED, status=ending.message_status,
                              stop_reason=ending.stop_reason, parts_seq=parts_seq,
                              duration_ms=int((time.monotonic() - self.started) * 1000))
