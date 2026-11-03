"""Stream events of one model response, turned into part events.

- thinking: a part on the first non-empty thinking delta; its end time
  when the block stops.
- text: one part per text block, its deltas coalesced (60 ms or 2 KB);
  citations land on the part when the block stops.
- tool_use: data tools get a ``tool`` part (preparing; the runner moves
  it on), run_agent an ``agent_call`` part; the research tools, charts
  and links add their own parts when they run.
- server tools: ``web_search`` / ``web_fetch`` parts, completed by their
  result blocks (which arrive whole).
- fallback: a notice part. Any other block type gets no part (its wire
  is still stored).
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from apps.agent.constants import PartType
from apps.agent.runtime.events import now_iso
from apps.agent.services.catalog import agent_name
from apps.agent.tools.registry import label_for, owns_parts

FLUSH_SECONDS = 0.06
FLUSH_CHARS = 2048
MAX_CITATIONS = 20
MAX_SEARCH_RESULTS = 10


class DeltaBuffer:
    """Coalesces streamed text; ``flush`` hands the joined text on."""

    def __init__(self, sink, *, seconds: float = FLUSH_SECONDS, chars: int = FLUSH_CHARS):
        self._sink = sink
        self._seconds = seconds
        self._chars = chars
        self._pieces: list[str] = []
        self._size = 0
        self._last = time.monotonic()

    def add(self, text: str) -> None:
        if not text:
            return
        self._pieces.append(text)
        self._size += len(text)
        if self._size >= self._chars or time.monotonic() - self._last >= self._seconds:
            self.flush()

    def flush(self) -> None:
        if self._pieces:
            text = "".join(self._pieces)
            self._pieces = []
            self._size = 0
            self._sink(text)
        self._last = time.monotonic()


def _as_dict(value) -> dict:
    if value is None:
        return {}
    if hasattr(value, "to_dict"):
        return value.to_dict(mode="json")
    return dict(value) if isinstance(value, dict) else {}


@dataclass
class _Block:
    kind: str
    started: float = field(default_factory=time.monotonic)
    started_at: str = field(default_factory=now_iso)
    part_id: str | None = None
    buffer: DeltaBuffer | None = None
    citations: list = field(default_factory=list)
    tool_id: str | None = None
    name: str | None = None


class StreamTranslator:
    def __init__(self, emitter, request: int, *, server_parts: dict | None = None):
        self.em = emitter
        self.request = request
        self.blocks: dict[int, _Block] = {}
        self.completed: set[int] = set()
        # tool_use id -> the part the translator added for it.
        self.tool_parts: dict[str, str] = {}
        # server tool_use id -> its part. Shared across the run's requests:
        # after pause_turn the result arrives in the next response.
        self.server_parts: dict[str, str] = server_parts if server_parts is not None else {}
        # Text and thinking characters streamed (usage estimate on a stop).
        self.chars = 0
        self.model: str | None = None
        self.start_usage: dict | None = None
        self.message_id: str | None = None

    # ── Dispatch ─────────────────────────────────────────────────────

    def handle(self, event) -> None:
        kind = getattr(event, "type", None)
        if kind == "message_start":
            message = event.message
            self.model = getattr(message, "model", None)
            self.message_id = getattr(message, "id", None)
            self.start_usage = _as_dict(getattr(message, "usage", None))
        elif kind == "content_block_start":
            self._start(event.index, event.content_block)
        elif kind == "content_block_delta":
            self._delta(event.index, event.delta)
        elif kind == "content_block_stop":
            self._stop(event.index, getattr(event, "content_block", None))

    def finish(self) -> None:
        """Flush whatever is buffered (the stream ended or was stopped)."""
        for block in self.blocks.values():
            if block.buffer is not None:
                block.buffer.flush()

    # ── Blocks ───────────────────────────────────────────────────────

    def _start(self, index: int, content_block) -> None:
        data = _as_dict(content_block)
        kind = data.get("type") or getattr(content_block, "type", "")
        block = _Block(kind=kind)
        self.blocks[index] = block
        if kind == "text":
            block.buffer = DeltaBuffer(lambda text, b=block: self._flush_text(b, text))
        elif kind == "thinking":
            block.buffer = DeltaBuffer(lambda text, b=block: self._flush_thinking(b, text))
        elif kind == "tool_use":
            block.tool_id = data.get("id")
            block.name = data.get("name")
            if block.name == "run_agent":
                block.part_id = self.em.add_part(
                    PartType.AGENT_CALL, request=self.request, agent=None, agent_name=None,
                    status="preparing", artifact_id=None, version=None, title=None, kind=None,
                    summary="", error=None,
                )
            elif not owns_parts(block.name):
                block.part_id = self.em.add_part(
                    PartType.TOOL, request=self.request, name=block.name,
                    label=label_for(block.name), status="preparing", summary="", error=None,
                )
            if block.part_id and block.tool_id:
                self.tool_parts[block.tool_id] = block.part_id
        elif kind == "server_tool_use":
            block.tool_id = data.get("id")
            block.name = data.get("name")
            if block.name == "web_search":
                block.part_id = self.em.add_part(
                    PartType.WEB_SEARCH, request=self.request, query="", status="running",
                    results=[], error_code=None,
                )
            elif block.name == "web_fetch":
                block.part_id = self.em.add_part(
                    PartType.WEB_FETCH, request=self.request, url="", status="running", title="",
                    error_code=None,
                )
            if block.part_id and block.tool_id:
                self.server_parts[block.tool_id] = block.part_id
        elif kind in ("web_search_tool_result", "web_fetch_tool_result"):
            self._server_result(kind, data)
        elif kind == "fallback":
            to_model = (data.get("to") or {}).get("model") or "a fallback model"
            self.em.add_part(
                PartType.NOTICE, request=self.request, level="info", code="model_fallback",
                message=f"The first model declined part of this request; {to_model} continued it.",
            )

    def _delta(self, index: int, delta) -> None:
        block = self.blocks.get(index)
        if block is None:
            return
        kind = getattr(delta, "type", "")
        if kind == "text_delta" and block.buffer is not None:
            text = delta.text or ""
            self.chars += len(text)
            block.buffer.add(text)
        elif kind == "thinking_delta" and block.buffer is not None:
            text = delta.thinking or ""
            self.chars += len(text)
            block.buffer.add(text)
        elif kind == "citations_delta":
            citation = _as_dict(getattr(delta, "citation", None))
            url = citation.get("url")
            if url and len(block.citations) < MAX_CITATIONS:
                block.citations.append({
                    "url": url,
                    "title": citation.get("title") or "",
                    "cited_text": citation.get("cited_text") or "",
                })

    def _stop(self, index: int, content_block) -> None:
        self.completed.add(index)
        block = self.blocks.get(index)
        if block is None:
            return
        if block.buffer is not None:
            block.buffer.flush()
        data = _as_dict(content_block)
        if block.kind == "text" and block.part_id and block.citations:
            self.em.update_part(block.part_id, citations=block.citations)
        elif block.kind == "thinking" and block.part_id:
            self.em.update_part(
                block.part_id, ended_at=now_iso(),
                duration_ms=int((time.monotonic() - block.started) * 1000),
            )
        elif block.kind == "tool_use" and block.name == "run_agent" and block.part_id:
            args = data.get("input") if isinstance(data.get("input"), dict) else {}
            agent = args.get("agent") if isinstance(args.get("agent"), str) else None
            self.em.update_part(
                block.part_id, agent=agent, agent_name=agent_name(agent) if agent else None,
                title=args.get("title") if isinstance(args.get("title"), str) else None,
            )
        elif block.kind == "server_tool_use" and block.part_id:
            args = data.get("input") if isinstance(data.get("input"), dict) else {}
            if block.name == "web_search":
                self.em.update_part(block.part_id, query=str(args.get("query") or ""))
            elif block.name == "web_fetch":
                self.em.update_part(block.part_id, url=str(args.get("url") or ""))

    def _flush_text(self, block: _Block, text: str) -> None:
        if block.part_id is None:
            block.part_id = self.em.add_part(PartType.TEXT, request=self.request, text=text,
                                             citations=[])
        else:
            self.em.append_text(block.part_id, text)

    def _flush_thinking(self, block: _Block, text: str) -> None:
        if block.part_id is None:
            if not text.strip():
                return
            block.part_id = self.em.add_part(
                PartType.THINKING, request=self.request, text=text, started_at=block.started_at,
                ended_at=None, duration_ms=None,
            )
        else:
            self.em.append_text(block.part_id, text)

    def _server_result(self, kind: str, data: dict) -> None:
        part_id = self.server_parts.get(data.get("tool_use_id") or "")
        if part_id is None:
            return
        content = data.get("content")
        if isinstance(content, dict) and content.get("error_code"):
            self.em.update_part(part_id, status="error", error_code=content.get("error_code"))
            return
        if kind == "web_search_tool_result":
            results = [
                {"title": r.get("title") or "", "url": r.get("url") or ""}
                for r in (content or []) if isinstance(r, dict) and r.get("url")
            ][:MAX_SEARCH_RESULTS]
            self.em.update_part(part_id, status="done", results=results)
        else:
            body = content if isinstance(content, dict) else {}
            document = body.get("content") if isinstance(body.get("content"), dict) else {}
            patch = {"status": "done", "title": document.get("title") or ""}
            if body.get("url"):
                patch["url"] = body["url"]
            self.em.update_part(part_id, **patch)
