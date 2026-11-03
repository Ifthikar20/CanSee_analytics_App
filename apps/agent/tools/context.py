"""What a tool handler gets, and what it returns.

Scope always comes from the run row: ``website`` and ``user`` are the
run's, never values from the tool input.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from apps.agent.tools.shaping import compact


class ToolError(Exception):
    """A failure the model should see as an ``is_error`` tool result."""

    def __init__(self, message: str, *, summary: str | None = None):
        super().__init__(message)
        self.message = message
        self.summary = summary or message


@dataclass
class ToolResult:
    content: str
    is_error: bool = False
    # One short line for the tool's step in the chat.
    summary: str = ""

    @classmethod
    def ok(cls, value: Any, summary: str = "") -> ToolResult:
        return cls(content=value if isinstance(value, str) else compact(value), summary=summary)

    @classmethod
    def error(cls, message: str, summary: str = "") -> ToolResult:
        return cls(content=message, is_error=True, summary=summary or message)


@dataclass
class ToolContext:
    run: Any
    chat: Any
    website: Any
    user: Any
    emitter: Any
    active: Any
    request: int
    tool_use_id: str = ""
    # A part the stream translator already added for this call (run_agent).
    part_id: str | None = None
    # What reached the chat from outside the model so far: the user's
    # words, tool results and web results (runtime.wire.outside_text).
    history_text: str = ""
    # Adds a nested request's usage (a sub-agent) to the run.
    add_usage: Callable[[Any], None] | None = None
    # Raises when the next model request must not go out (llm.gate).
    check_gate: Callable[[], None] | None = None
    inline: bool = False
    extra: dict = field(default_factory=dict)

    def cancelled(self) -> bool:
        return bool(self.active is not None and self.active.cancelled)
